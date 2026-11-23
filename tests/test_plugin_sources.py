import asyncio
from pathlib import Path

import pytest

from mohobot.plugins.sources import get_source, load_sources, remove_source, save_source


def test_source_records_merge_and_remove(tmp_path: Path) -> None:
    async def run() -> None:
        data = tmp_path / "data"
        await save_source(
            data,
            "alpha",
            {"install_method": "market", "repo": "https://github.com/a/alpha"},
        )
        await save_source(
            data,
            "beta",
            {"install_method": "repository", "repo": "https://github.com/a/beta"},
        )
        records = await load_sources(data)
        assert set(records) == {"alpha", "beta"}
        assert records["alpha"]["schema_version"] == 1

        alpha = await get_source(data, "alpha")
        assert alpha is not None
        assert alpha["install_method"] == "market"

        await remove_source(data, "alpha")
        records = await load_sources(data)
        assert set(records) == {"beta"}

    asyncio.run(run())


def test_atomic_source_replace_compares_only_target_and_keeps_peers(tmp_path):
    from mohobot.plugins.sources import SourceRecordConflict, replace_source_record

    async def run():
        await save_source(tmp_path, "alpha", {"version": "1.0.0"})
        previous = await get_source(tmp_path, "alpha")
        replacement = {**previous, "version": "2.0.0"}
        await save_source(tmp_path, "beta", {"extra": {"keep": True}})
        await replace_source_record(tmp_path, "alpha", expected=previous, record=replacement)
        with pytest.raises(SourceRecordConflict, match="alpha"):
            await replace_source_record(tmp_path, "alpha", expected=previous, record=None)
        assert await get_source(tmp_path, "alpha") == replacement
        await replace_source_record(tmp_path, "alpha", expected=replacement, record=previous)
        assert await load_sources(tmp_path) == {
            "alpha": previous, "beta": {"schema_version": 1, "extra": {"keep": True}},
        }

    asyncio.run(run())


@pytest.mark.parametrize("content", ["", "{", "null", "[]", '{"alpha": 1}', '{"x": {}, "x": {}}', '{"alpha": {"v": NaN}}'])
def test_malformed_source_record_file_is_not_overwritten(tmp_path, content):
    path = tmp_path / "plugin_sources.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        asyncio.run(save_source(tmp_path, "alpha", {}))
    assert path.read_text(encoding="utf-8") == content


def test_source_replace_failure_preserves_old_bytes(tmp_path, monkeypatch):
    async def run():
        await save_source(tmp_path, "alpha", {"version": "1.0.0"})
        path = tmp_path / "plugin_sources.json"
        previous = path.read_bytes()

        def deny_replace(source, destination):
            assert destination == path
            raise PermissionError("fixture denied replace")

        monkeypatch.setattr("mohobot.plugins._atomic_json.os.replace", deny_replace)
        with pytest.raises(PermissionError, match="fixture denied"):
            await save_source(tmp_path, "alpha", {"version": "2.0.0"})
        assert path.read_bytes() == previous
        assert list(tmp_path.iterdir()) == [path]

    asyncio.run(run())


def test_source_mutation_refuses_symlink(tmp_path):
    target = tmp_path / "other.json"
    target.write_text("{}", encoding="utf-8")
    path = tmp_path / "plugin_sources.json"
    path.symlink_to(target)
    with pytest.raises(ValueError, match="plugin_sources.json"):
        asyncio.run(save_source(tmp_path, "alpha", {}))
    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "{}"


def test_registry_generation_tracks_url_changes_not_display_names(tmp_path, monkeypatch):
    from mohobot.plugin_market import sources

    monkeypatch.setattr(sources.socket, "getaddrinfo", lambda *args: [])
    async def run():
        url = "https://EXAMPLE.com:443/Registry%2Findex.json?v=A%2Bb"
        canonical = "https://example.com/Registry%2Findex.json?v=A%2Bb"
        original = await sources.add_registry_source(tmp_path, id="custom", name="first", url=url)
        assert original.url == canonical
        assert original.generation
        persisted = next(source for source in await sources.load_registry_sources(tmp_path) if source.id == "custom")
        assert original.identity == persisted.identity

        renamed = await sources.add_registry_source(tmp_path, id="custom", name="renamed", url=canonical)
        assert renamed.name == "renamed"
        assert renamed.identity == original.identity

        swapped = await sources.add_registry_source(tmp_path, id="custom", name="renamed", url=canonical + "2")
        assert swapped.generation != original.generation
        assert swapped.identity != original.identity

        restored = await sources.add_registry_source(tmp_path, id="custom", name="renamed", url=canonical)
        assert restored.identity != original.identity
        await sources.remove_registry_source(tmp_path, "custom")
        recreated = await sources.add_registry_source(tmp_path, id="custom", name="renamed", url=canonical)
        assert recreated.identity not in {original.identity, restored.identity}

    asyncio.run(run())


def test_registry_legacy_identity_is_stable_until_source_is_replaced(tmp_path, monkeypatch):
    from mohobot.file_store import json_write
    from mohobot.plugin_market import sources

    monkeypatch.setattr(sources.socket, "getaddrinfo", lambda *args: [])
    async def run():
        await json_write(tmp_path / "plugin_registries.json", {"sources": [
            {"id": "custom", "name": "legacy", "url": "HTTP://EXAMPLE.COM:80/index.json?"},
            {"id": "official", "name": "obsolete", "url": "https://ao.example.com/i", "official": True},
        ]})
        official, legacy = await sources.load_registry_sources(tmp_path)
        assert official.official and official.url == sources.DEFAULT_INDEX_URL
        assert legacy.url == "http://example.com/index.json?"
        assert legacy.generation
        rename = await sources.add_registry_source(tmp_path, id="custom", name="new name", url=legacy.url)
        assert rename.identity == legacy.identity
        assert (await sources.load_registry_sources(tmp_path))[1].identity == legacy.identity

    asyncio.run(run())


def test_registry_source_rejects_credentials() -> None:
    from mohobot.plugin_market.sources import validate_source_url

    try:
        validate_source_url("https://user:pass@example.com/index.json")
    except ValueError as exc:
        assert "凭据" in str(exc)
    else:
        raise AssertionError("带凭据的来源 URL 未拒绝")


def test_source_host_resolving_to_private_ip_is_rejected() -> None:
    import socket

    from mohobot.plugin_market import sources

    original = sources.socket.getaddrinfo

    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    sources.socket.getaddrinfo = fake_getaddrinfo
    try:
        with pytest.raises(ValueError):
            sources.validate_source_url("https://plugins.example.test/index.json")
    finally:
        sources.socket.getaddrinfo = original
