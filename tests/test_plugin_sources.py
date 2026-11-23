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
