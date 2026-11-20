import asyncio
import json
import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

from mohobot.plugin_market.service import MarketService
from mohobot.plugin_market.sources import add_registry_source
from mohobot.plugins.installer import sha256_file
from mohobot.plugins.sources import load_sources


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
        data["download_url"] = archive.as_uri()
    return data


def _payload(name: str) -> dict:
    return {
        "$meta": {"schema_version": 1, "name": "index"},
        "plugins": {f"CarefreeSongs712/{name}": _entry(name)},
    }


def _archive(tmp_path: Path, name: str) -> Path:
    source = tmp_path / "src" / name
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
        "version: 1.0.0\n"
        "description: demo\n"
        f"repo: https://github.com/CarefreeSongs712/mohobot-plugin-{name}\n"
        "license: MIT\n"
        "api_version: 1\n",
        encoding="utf-8",
    )
    archive = tmp_path / f"{name}.zip"
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
