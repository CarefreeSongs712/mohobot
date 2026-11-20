from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from mohobot.plugin_market.sources import (
    RegistrySource,
    load_registry_sources,
)
from mohobot.plugins.installer import (
    InstallError,
    download_file,
    install_from_archive,
    verify_sha256,
    with_proxy,
)
from mohobot.plugins.market import (
    MarketEntry,
    MarketIndex,
    entry_download_url,
    parse_market_index,
)
from mohobot.plugins.sources import save_source

MAX_INDEX_BYTES = 5 * 1024 * 1024


def _cache_path(data_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return data_dir / "cache" / "plugin_market" / f"{digest}.json"


def fetch_json(url: str, timeout: float = 20.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "mohobot-plugin-market"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_INDEX_BYTES + 1)
    if len(raw) > MAX_INDEX_BYTES:
        raise InstallError("插件索引超过大小限制")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise InstallError("插件索引根节点必须是对象")
    return payload


class MarketService:
    def __init__(
        self,
        data_dir: str | Path,
        plugins_dir: str | Path,
        *,
        proxy: str = "",
        fetcher: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.plugins_dir = Path(plugins_dir)
        self.proxy = proxy
        self._fetcher = fetcher or fetch_json
        self._indexes: dict[str, MarketIndex] = {}
        self._sources: list[RegistrySource] = []

    @property
    def sources(self) -> list[RegistrySource]:
        return list(self._sources)

    async def refresh(self) -> list[RegistrySource]:
        self._sources = await load_registry_sources(self.data_dir)
        for source in self._sources:
            try:
                payload = await asyncio.to_thread(
                    self._fetcher, with_proxy(source.url, self.proxy)
                )
                index = parse_market_index(payload)
                self._indexes[source.id] = index
                cache = _cache_path(self.data_dir, source.url)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning(f"插件源 {source.id} 刷新失败: {exc}")
                cache = _cache_path(self.data_dir, source.url)
                if cache.is_file():
                    try:
                        self._indexes[source.id] = parse_market_index(
                            json.loads(cache.read_text(encoding="utf-8"))
                        )
                    except Exception:
                        pass
        return self.sources

    def entries(self) -> list[MarketEntry]:
        seen: set[str] = set()
        result: list[MarketEntry] = []
        for source in self._sources:
            index = self._indexes.get(source.id)
            if index is None:
                continue
            for entry in index.plugins.values():
                if entry.plugin_id in seen:
                    continue
                seen.add(entry.plugin_id)
                result.append(entry)
        return result

    def find(self, plugin_id: str) -> MarketEntry | None:
        return next((entry for entry in self.entries() if entry.plugin_id == plugin_id), None)

    async def install_from_entry(
        self,
        entry: MarketEntry,
        *,
        source_id: str = "official",
    ) -> tuple[str, Path]:
        url = with_proxy(entry_download_url(entry), self.proxy) if self.proxy else entry_download_url(entry)
        with tempfile.TemporaryDirectory(prefix="mohobot-market-") as temp:
            archive = Path(temp) / "plugin.zip"
            await asyncio.to_thread(download_file, url, archive)
            if entry.sha256:
                await asyncio.to_thread(verify_sha256, archive, entry.sha256)
            manifest, destination = await asyncio.to_thread(
                install_from_archive, archive, self.plugins_dir
            )
        await save_source(
            self.data_dir,
            manifest.name,
            {
                "install_method": "market",
                "registry_id": source_id,
                "plugin_id": entry.plugin_id,
                "repo": entry.repo,
                "download_url": entry.download_url,
                "version": entry.version,
                "installed_at": time.time(),
            },
        )
        return manifest.name, destination

    async def install(self, plugin_id: str, *, source_id: str = "official") -> tuple[str, Path]:
        if not self._sources:
            await self.refresh()
        entry = self.find(plugin_id)
        if entry is None:
            raise InstallError(f"插件不存在: {plugin_id}")
        return await self.install_from_entry(entry, source_id=source_id)
