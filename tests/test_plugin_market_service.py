import asyncio
import shutil
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mohobot.plugin_market.journal import MarketOperation, MarketRecoveryError
from mohobot.plugin_market.service import MarketService, MarketSourceChanged
from mohobot.plugin_market.sources import DEFAULT_INDEX_URL, add_registry_source
from mohobot.file_store import json_write
from mohobot.plugins.installer import InstallError, sha256_file
from mohobot.plugins.sources import load_sources, save_source


@pytest.fixture(autouse=True)
def _offline_registry_dns(monkeypatch):
    monkeypatch.setattr("mohobot.plugin_market.sources.socket.getaddrinfo", lambda *args: [])


def _entry(name: str, archive: Path | None = None) -> dict:
    data = {
        "author": "CarefreeSongs712",
        "name": name,
        "version": "1.0.0",
        "repo": f"https://github.com/CarefreeSongs712/mohobot-plugin-{name}",
        "description": "demo",
        "sha256": "a" * 64,
        "api_version": 1,
    }
    if archive is not None:
        data["sha256"] = sha256_file(archive)
        data["download_url"] = (
            f"https://github.com/CarefreeSongs712/mohobot-plugin-{name}/"
            f"releases/download/v1.0.0/{name}-1.0.0.zip"
        )
    return data


def _payload(name: str) -> dict:
    return {
        "$meta": {"schema_version": 1, "name": "index"},
        "plugins": {f"CarefreeSongs712/{name}": _entry(name)},
    }


def _archive(tmp_path: Path, name: str, version: str = "1.0.0") -> Path:
    source = tmp_path / f"src-{version}" / name
    source.mkdir(parents=True)
    (source / "main.py").write_text(
        "class Plugin:\n"
        "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        encoding="utf-8",
    )
    (source / "metadata.yaml").write_text(
        f"name: {name}\n"
        "author: CarefreeSongs712\n"
        "display_name: 演示\n"
        f"version: {version}\n"
        "description: demo\n"
        f"repo: https://github.com/CarefreeSongs712/mohobot-plugin-{name}\n"
        "license: MIT\n"
        "api_version: 1\n",
        encoding="utf-8",
    )
    archive = tmp_path / f"{name}-{version}.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for path in source.rglob("*"):
            if path.is_file():
                handle.write(path, f"{name}-main/" + path.relative_to(source).as_posix())
    return archive


def test_market_refresh_merges_sources(tmp_path: Path) -> None:
    async def run() -> None:
        await add_registry_source(
            tmp_path / "data",
            id="extra",
            name="额外源",
            url="https://example.com/index.json",
        )
        payloads = {
            "https://v4.gh-proxy.org/https://raw.githubusercontent.com/CarefreeSongs712/mohobot-plugin-index/main/index.json": _payload("official"),
            "https://v4.gh-proxy.org/https://example.com/index.json": _payload("extra"),
        }
        service = MarketService(
            tmp_path / "data",
            tmp_path / "plugins",
            proxy="https://v4.gh-proxy.org/",
            fetcher=lambda url: payloads[url],
        )
        await service.refresh()
        ids = {entry.plugin_id for entry in service.entries()}
        assert ids == {"CarefreeSongs712/official", "CarefreeSongs712/extra"}

    asyncio.run(run())


def test_install_from_entry_records_source(tmp_path: Path) -> None:
    async def run() -> None:
        archive = _archive(tmp_path, "demo")
        payload = {
            "$meta": {"schema_version": 1, "name": "index"},
            "plugins": {"CarefreeSongs712/demo": _entry("demo", archive)},
        }
        service = MarketService(
            tmp_path / "data",
            tmp_path / "plugins",
            fetcher=lambda url: payload,
        )
        await service.refresh()
        entry = service.find("CarefreeSongs712/demo")
        assert entry is not None
        with patch(
            "mohobot.plugin_market.service.download_file",
            lambda url, dest: shutil.copy(archive, dest),
        ):
            name, destination = await service.install_from_entry(entry)
        assert name == "demo"
        assert (destination / "main.py").is_file()
        records = await load_sources(tmp_path / "data")
        assert records["demo"]["install_method"] == "market"
        assert records["demo"]["plugin_id"] == "CarefreeSongs712/demo"

    asyncio.run(run())


def test_update_market_plugin(tmp_path: Path) -> None:
    async def run() -> None:
        archive_v1 = _archive(tmp_path, "demo")
        payload = {
            "$meta": {"schema_version": 1, "name": "index"},
            "plugins": {"CarefreeSongs712/demo": _entry("demo", archive_v1)},
        }
        service = MarketService(
            tmp_path / "data",
            tmp_path / "plugins",
            fetcher=lambda url: payload,
        )
        await service.refresh()
        entry = service.find("CarefreeSongs712/demo")
        assert entry is not None
        with patch(
            "mohobot.plugin_market.service.download_file",
            lambda url, dest: shutil.copy(archive_v1, dest),
        ):
            await service.install_from_entry(entry)

        # #145 used to record the index version even when the ZIP was older.
        # A stale record must not prevent retrying a real upgrade.
        stale = (await load_sources(tmp_path / "data"))["demo"]
        await save_source(tmp_path / "data", "demo", {**stale, "version": "99.0.0"})
        archive_v2 = _archive(tmp_path, "demo", version="2.0.0")
        payload["plugins"]["CarefreeSongs712/demo"] = _entry("demo", archive_v2)
        payload["plugins"]["CarefreeSongs712/demo"]["version"] = "2.0.0"
        await service.refresh()
        seen: list[str] = []

        async def before_replace(name: str) -> None:
            seen.append(name)

        with patch(
            "mohobot.plugin_market.service.download_file",
            lambda url, dest: shutil.copy(archive_v2, dest),
        ):
            name, destination = await service.update(
                "CarefreeSongs712/demo", before_replace=before_replace
            )
        assert name == "demo"
        assert seen == ["demo"]
        assert "2.0.0" in (destination / "metadata.yaml").read_text(encoding="utf-8")
        assert (await load_sources(tmp_path / "data"))["demo"]["version"] == "2.0.0"

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["install", "update", "uninstall"])
def test_market_write_rolls_back_files_and_source_if_commit_fails(tmp_path, monkeypatch, kind):
    async def run():
        data = tmp_path / "data"
        plugins = data / "plugins"
        config_dir = data / "plugins_config"
        recycle_dir = data / "plugin_recycle"
        config_file = config_dir / "demo.json"
        source = _archive(tmp_path, "demo")
        plugin_id = "CarefreeSongs712/demo"
        payload = {"$meta": {"schema_version": 1, "name": "index"}, "plugins": {plugin_id: _entry("demo", source)}}
        service = MarketService(data, plugins, fetcher=lambda url: payload)
        monkeypatch.setattr("mohobot.plugin_market.service.download_file", lambda url, dest: shutil.copy(source, dest))
        await save_source(data, "peer", {"version": "9.0.0"})
        if kind != "install":
            await service.install(plugin_id)
        if kind == "update":
            source = _archive(tmp_path, "demo", "2.0.0")
            payload["plugins"][plugin_id] = {**_entry("demo", source), "version": "2.0.0"}
            await service.refresh()
        config_dir.mkdir()
        config_file.write_text('{"keep": true}')
        original = await load_sources(data)
        live = plugins / "demo"
        old_inode = live.stat().st_ino if live.exists() else None
        decisions = []
        real_write = MarketOperation._write_decision

        def fail_commit(operation, decision):
            decisions.append(decision)
            if decision == "committed":
                raise OSError("fixture cannot mark commit")
            return real_write(operation, decision)

        monkeypatch.setattr(MarketOperation, "_write_decision", fail_commit)
        with pytest.raises(MarketRecoveryError, match="fixture cannot mark commit"):
            if kind == "uninstall":
                await service.uninstall("demo", config_dir=config_dir, recycle_dir=recycle_dir, delete_config=True)
            else:
                await getattr(service, kind)(plugin_id)
        assert decisions == ["committed", "rolled_back"]
        assert (live.stat().st_ino if live.exists() else None) == old_inode
        assert await load_sources(data) == original
        assert config_file.read_text() == '{"keep": true}'
        assert not list(recycle_dir.glob("*"))
        assert not list((plugins / ".market-journal").iterdir())

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["install", "update"])
@pytest.mark.parametrize("case", ["version", "author", "api", "host", "entry", "digest"])
def test_market_checks_actual_archive_before_replacing_target(tmp_path, monkeypatch, operation, case):
    async def run() -> None:
        data = tmp_path / "data"
        service = MarketService(data, data / "plugins", fetcher=lambda url: payload)
        target = service.plugins_dir / "demo"
        entry_id = "CarefreeSongs712/demo"
        record = {"install_method": "market", "registry_id": "official", "plugin_id": entry_id, "version": "1.0.0"}
        want_version = "2.0.0" if operation == "update" else "1.0.0"
        old_metadata = (
            "name: demo\nauthor: CarefreeSongs712\ndisplay_name: Demo\nversion: 1.0.0\n"
            "description: demo\nrepo: https://github.com/CarefreeSongs712/mohobot-plugin-demo\n"
            "license: MIT\napi_version: 1\n"
        )
        if operation == "update":
            target.mkdir(parents=True)
            (target / "metadata.yaml").write_text(old_metadata, encoding="utf-8")
            (target / "main.py").write_text("# old bytes\n", encoding="utf-8")
            await save_source(data, "demo", record)
        original_records = await load_sources(data)

        metadata = old_metadata.replace("version: 1.0.0", f"version: {want_version}")
        if case == "version":
            metadata = metadata.replace(f"version: {want_version}", "version: 0.5.0")
        elif case == "author":
            metadata = metadata.replace("author: CarefreeSongs712", "author: OtherOwner")
        elif case == "api":
            metadata = metadata.replace("api_version: 1", "api_version: 999")
        elif case == "host":
            metadata += 'mohobot_version: ">=999.0.0"\n'
        elif case == "entry":
            metadata += "entry: src/not_there.py\n"
        archive = tmp_path / "candidate.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("demo/metadata.yaml", metadata)
            handle.writestr("demo/main.py", "raise AssertionError('must not execute')\n")
        entry = _entry("demo", archive)
        entry["version"] = want_version
        if case == "digest":
            entry["sha256"] = "0" * 64
        payload = {"$meta": {"schema_version": 1}, "plugins": {entry_id: entry}}
        monkeypatch.setattr("mohobot.plugin_market.service.download_file", lambda url, dest: shutil.copy(archive, dest))
        stopped = []

        async def before_replace(name):
            stopped.append(name)

        expected_error = {
            "version": "版本", "author": "身份", "api": "API", "host": "mohobot",
            "entry": "entry file not found", "digest": "SHA256",
        }[case]
        with pytest.raises(InstallError, match=expected_error):
            if operation == "update":
                await service.update(entry_id, before_replace=before_replace)
            else:
                await service.install(entry_id)
        assert stopped == []
        assert await load_sources(data) == original_records
        if operation == "update":
            assert (target / "metadata.yaml").read_text(encoding="utf-8") == old_metadata
            assert (target / "main.py").read_bytes() == b"# old bytes\n"
        else:
            assert not target.exists()

    asyncio.run(run())


def test_refresh_cannot_publish_after_newer_refresh(tmp_path):
    async def run():
        loop = asyncio.get_running_loop()
        started = loop.create_future()
        release = threading.Event()
        calls = 0

        def fetch(url):
            nonlocal calls
            calls += 1
            if calls == 1:
                loop.call_soon_threadsafe(started.set_result, None)
                assert release.wait(5)
                return _payload("stale")
            return _payload("latest")

        data = tmp_path / "data"
        service = MarketService(data, data / "plugins", fetcher=fetch)
        old = asyncio.create_task(service.refresh())
        try:
            await asyncio.wait_for(started, 5)
            await service.refresh()
        finally:
            release.set()
            await old
        assert [entry.name for entry in service.entries()] == ["latest"]
        assert service.source_status("official")["status"] == "fresh"

        def offline(url):
            raise OSError("offline fixture")

        restarted = MarketService(data, data / "plugins", fetcher=offline)
        await restarted.refresh()
        assert [entry.name for entry in restarted.entries()] == ["latest"]
        assert restarted.source_status("official")["status"] == "cached"
        assert restarted.source_status("official")["last_success_at"] == service.source_status("official")["last_success_at"]

    asyncio.run(run())


def test_source_list_does_not_skip_initial_index_or_proxy_fallback(tmp_path):
    async def run():
        requests = []

        def fetch(url):
            requests.append(url)
            if url != DEFAULT_INDEX_URL:
                raise OSError("proxy fixture unavailable")
            return _payload("official")

        service = MarketService(tmp_path / "data", tmp_path / "plugins", proxy="https://proxy.example.test", fetcher=fetch)
        await service.reload_sources()
        assert requests == []
        assert service.entries() == []
        assert service.source_status("official") == {"status": "unattempted", "last_success_at": None}
        await service.ensure_indexes()
        await service.ensure_indexes()
        assert requests == ["https://proxy.example.test/" + DEFAULT_INDEX_URL, DEFAULT_INDEX_URL]
        assert [entry.name for entry in service.entries()] == ["official"]
        assert service.source_status("official")["status"] == "fresh"

    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["url", "recreate"])
def test_old_source_refresh_and_cache_are_invalidated(tmp_path, mutation):
    async def run():
        loop = asyncio.get_running_loop()
        started = loop.create_future()
        release = threading.Event()
        custom_calls = 0
        url = "https://registry.example.test/index.json"

        def fetch(request_url):
            nonlocal custom_calls
            if request_url == DEFAULT_INDEX_URL:
                return _payload("official")
            custom_calls += 1
            if custom_calls == 1:
                return _payload("cached_old")
            if custom_calls == 2:
                loop.call_soon_threadsafe(started.set_result, None)
                assert release.wait(5)
                return _payload("late_old")
            raise OSError("offline new identity")

        service = MarketService(tmp_path / "data", tmp_path / "plugins", fetcher=fetch)
        await service.add_source(source_id="custom", name="old", url=url)
        await service.refresh()
        assert [entry.name for entry in service.entries("custom")] == ["cached_old"]
        await service.add_source(source_id="custom", name="renamed", url=url)
        assert service.source_status("custom")["status"] == "fresh"
        old = asyncio.create_task(service.refresh())
        try:
            await asyncio.wait_for(started, 5)
            if mutation == "recreate":
                await service.remove_source("custom")
            await service.add_source(source_id="custom", name="new", url=url if mutation == "recreate" else url + "?new=1")
            assert service.entries("custom") == []
            assert service.source_status("custom")["status"] == "unattempted"
            await service.ensure_indexes()
        finally:
            release.set()
            await old
        assert service.entries("custom") == []
        assert service.source_status("custom") == {"status": "failed", "last_success_at": None}

    asyncio.run(run())


def test_corrupt_identity_cache_is_not_reused(tmp_path):
    async def run():
        def offline(url):
            raise OSError("offline fixture")

        service = MarketService(tmp_path, tmp_path / "plugins", fetcher=lambda url: _payload("demo"))
        await service.refresh()
        cache, = (tmp_path / "cache" / "plugin_market").glob("*.json")
        cache.write_text('{"index":', encoding="utf-8")
        restarted = MarketService(tmp_path, tmp_path / "plugins", fetcher=offline)
        await restarted.refresh()
        assert restarted.entries() == []
        assert restarted.source_status("official") == {"status": "failed", "last_success_at": None}

    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["rename", "url", "recreate"])
def test_install_rechecks_frozen_source_after_download(tmp_path, monkeypatch, mutation):
    async def run():
        loop = asyncio.get_running_loop()
        started = loop.create_future()
        release = threading.Event()
        archive = _archive(tmp_path, "demo")
        plugin_id = "CarefreeSongs712/demo"
        url = "https://registry.example.test/index.json"
        payload = {"$meta": {"schema_version": 1}, "plugins": {plugin_id: _entry("demo", archive)}}
        service = MarketService(tmp_path / "data", tmp_path / "plugins", fetcher=lambda _: payload)
        original = await service.add_source(source_id="custom", name="original", url=url)

        def download(download_url, destination):
            loop.call_soon_threadsafe(started.set_result, download_url)
            assert release.wait(5)
            shutil.copyfile(archive, destination)

        monkeypatch.setattr("mohobot.plugin_market.service.download_file", download)
        installing = asyncio.create_task(service.install(plugin_id, source_id="custom"))
        try:
            used_url = await asyncio.wait_for(started, 5)
            if mutation == "recreate":
                await service.remove_source("custom")
            changed = await service.add_source(source_id="custom", name="changed", url=url + ("?changed=1" if mutation == "url" else ""))
        finally:
            release.set()
        if mutation == "rename":
            _, dest = await installing
            assert dest.is_dir()
            record = (await load_sources(service.data_dir))["demo"]
            assert record["registry_id"] == "custom"
            assert record["registry_url"] == original.url
            assert record["registry_generation"] == original.generation == changed.generation
            assert record["download_url"] == used_url
        else:
            with pytest.raises(MarketSourceChanged):
                await installing
            assert not (service.plugins_dir / "demo").exists()
            assert await load_sources(service.data_dir) == {}
        assert not list(service.plugins_dir.glob(".market-prepare-*"))

    asyncio.run(run())


@pytest.mark.parametrize("record_identity, expected", [
    ("wrong_url", MarketSourceChanged),
    ("wrong_generation", MarketSourceChanged),
    ("no_url", None),
    ("normalized_url", None),
])
def test_update_never_rebinds_recorded_source_and_backfills_legacy(tmp_path, monkeypatch, record_identity, expected):
    async def run():
        plugin_id = "CarefreeSongs712/demo"
        _archive(tmp_path, "demo")
        archive_v2 = _archive(tmp_path, "demo", "2.0.0")
        entry = _entry("demo", archive_v2)
        entry["version"] = "2.0.0"
        payload = {"$meta": {"schema_version": 1}, "plugins": {plugin_id: entry}}
        data = tmp_path / "data"
        service = MarketService(data, data / "plugins", fetcher=lambda _: payload)
        source = await service.add_source(source_id="custom", name="fixture", url="https://registry.example.test/i")
        destination = service.plugins_dir / "demo"
        destination.parent.mkdir(parents=True)
        shutil.copytree(tmp_path / "src-1.0.0" / "demo", destination)
        record = {"install_method": "market", "registry_id": "custom", "plugin_id": plugin_id}
        if record_identity == "wrong_url":
            record["registry_url"] = "https://other.example.test/i"
        elif record_identity == "wrong_generation":
            record.update(registry_url=source.url, registry_generation="0" * 32)
        elif record_identity == "normalized_url":
            record["registry_url"] = "HTTPS://Registry.Example.Test:443/i"
        await save_source(data, "demo", record)
        downloads = []
        stopped = []

        def download(url, dest):
            downloads.append(url)
            shutil.copyfile(archive_v2, dest)

        async def before_replace(name):
            stopped.append(name)

        monkeypatch.setattr("mohobot.plugin_market.service.download_file", download)
        if expected:
            with pytest.raises(expected):
                await service.update(plugin_id, before_replace=before_replace)
            assert not downloads and not stopped
            assert (await load_sources(data))["demo"] == {**record, "schema_version": 1}
            assert "version: 1.0.0" in (destination / "metadata.yaml").read_text()
        else:
            await service.update(plugin_id, before_replace=before_replace)
            assert len(downloads) == 1 and stopped == ["demo"]
            updated = (await load_sources(data))["demo"]
            assert updated["registry_url"] == source.url
            assert updated["registry_generation"] == source.generation
            assert updated["version"] == "2.0.0"

    asyncio.run(run())


def test_cancelled_download_keeps_staging_until_worker_exits(tmp_path, monkeypatch):
    async def run():
        service = MarketService(tmp_path / "data", tmp_path / "installed", fetcher=lambda url: _payload("demo"))
        loop = asyncio.get_running_loop()
        started = loop.create_future()
        release = threading.Event()

        def download(url, path):
            loop.call_soon_threadsafe(started.set_result, path)
            assert release.wait(5)
            assert path.parent.is_dir()
            path.write_bytes(b"discarded download")

        monkeypatch.setattr("mohobot.plugin_market.service.download_file", download)
        task = asyncio.create_task(service.install("CarefreeSongs712/demo"))
        path = await asyncio.wait_for(started, 5)
        try:
            for _ in range(2):
                task.cancel()
                turn = loop.create_future()
                loop.call_soon(turn.set_result, None)
                await turn
                assert not task.done()
                assert path.parent.is_dir()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not path.parent.exists()
        assert not (service.plugins_dir / "demo").exists()
        assert await load_sources(service.data_dir) == {}

    asyncio.run(run())


def test_reload_sources_reads_persisted_changes(tmp_path: Path) -> None:
    async def run() -> None:
        data = tmp_path / "data"
        service = MarketService(data, tmp_path / "plugins")
        await json_write(
            data / "plugin_registries.json",
            {
                "schema_version": 1,
                "sources": [
                    {
                        "id": "custom",
                        "name": "自定义源",
                        "url": "https://plugins.example.test/index.json",
                    }
                ],
            },
        )
        with patch(
            "mohobot.plugin_market.sources.validate_source_url",
            lambda url: url,
        ):
            sources = await service.reload_sources()
        assert [source.id for source in sources] == ["official", "custom"]

    asyncio.run(run())
