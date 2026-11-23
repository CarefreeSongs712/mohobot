import asyncio
import shutil
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mohobot.plugin_market.service import MarketService
from mohobot.plugin_market.sources import add_registry_source
from mohobot.file_store import json_write
from mohobot.plugins.installer import InstallError, sha256_file
from mohobot.plugins.sources import load_sources, save_source


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


@pytest.mark.parametrize("operation", ["install", "update"])
@pytest.mark.parametrize("case", ["version", "author", "api", "host", "entry", "digest"])
def test_market_checks_actual_archive_before_replacing_target(tmp_path, monkeypatch, operation, case):
    async def run() -> None:
        data = tmp_path / "data"
        service = MarketService(data, data / "plugins", fetcher=lambda url: payload)
        target = service.plugins_dir / "demo"
        entry_id = "CarefreeSongs712/demo"
        record = {"install_method": "market", "plugin_id": entry_id, "version": "1.0.0"}
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
