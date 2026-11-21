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
