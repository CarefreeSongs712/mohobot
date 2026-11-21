from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from dataclasses import replace
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from loguru import logger
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from mohobot import __version__ as MOHOBOT_VERSION
from mohobot.api import api_version as PLUGIN_API_VERSION
from mohobot.plugin_market.sources import (
    RegistrySource,
    load_registry_sources,
)
from mohobot.plugins.installer import (
    InstallError,
    download_file,
    install_from_archive,
    upgrade_from_archive,
    verify_sha256,
    with_proxy,
)
from mohobot.plugins.market import (
    MarketEntry,
    MarketIndex,
    entry_download_url,
    parse_market_index,
)
from mohobot.plugins.sources import load_sources, remove_source, save_source

MAX_INDEX_BYTES = 5 * 1024 * 1024


def _check_entry_compatibility(entry: MarketEntry) -> None:
    if entry.api_version != PLUGIN_API_VERSION:
        raise InstallError(
            f"插件要求 API {entry.api_version}, 当前支持 {PLUGIN_API_VERSION}"
        )
    if not entry.mohobot_version:
        return
    try:
        spec = SpecifierSet(entry.mohobot_version)
        current = Version(MOHOBOT_VERSION)
    except (InvalidSpecifier, InvalidVersion) as exc:
        raise InstallError(f"插件版本约束无效: {exc}") from exc
    if current not in spec:
        raise InstallError(
            f"插件要求 mohobot {entry.mohobot_version}, 当前 {MOHOBOT_VERSION}"
        )


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
        self._op_lock = asyncio.Lock()

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

    def entries(self, source_id: str | None = None) -> list[MarketEntry]:
        seen: set[str] = set()
        result: list[MarketEntry] = []
        for source in self._sources:
            if source_id is not None and source.id != source_id:
                continue
            index = self._indexes.get(source.id)
            if index is None:
                continue
            for entry in index.plugins.values():
                if entry.plugin_id in seen:
                    continue
                seen.add(entry.plugin_id)
                result.append(replace(entry, source_id=source.id))
        return result

    def find(self, plugin_id: str, source_id: str | None = None) -> MarketEntry | None:
        return next(
            (entry for entry in self.entries(source_id) if entry.plugin_id == plugin_id),
            None,
        )

    async def _download_entry(self, entry: MarketEntry, target: Path) -> None:
        """下载插件归档; 代理失败时回退 GitHub 直连。"""
        url = entry_download_url(entry)
        proxy_url = with_proxy(url, self.proxy) if self.proxy else url
        try:
            await asyncio.to_thread(download_file, proxy_url, target)
        except InstallError:
            if not self.proxy:
                raise
            await asyncio.to_thread(download_file, url, target)

    async def install_from_entry(
        self,
        entry: MarketEntry,
        *,
        source_id: str = "official",
    ) -> tuple[str, Path]:
        async with self._op_lock:
            return await self._install_from_entry_unlocked(entry, source_id=source_id)

    async def _install_from_entry_unlocked(
        self,
        entry: MarketEntry,
        *,
        source_id: str = "official",
    ) -> tuple[str, Path]:
        _check_entry_compatibility(entry)
        source_id = entry.source_id or source_id
        with tempfile.TemporaryDirectory(prefix="mohobot-market-") as temp:
            archive = Path(temp) / "plugin.zip"
            await self._download_entry(entry, archive)
            if entry.sha256:
                await asyncio.to_thread(verify_sha256, archive, entry.sha256)
            manifest, destination = await asyncio.to_thread(
                install_from_archive,
                archive,
                self.plugins_dir,
                expected_name=entry.name,
                expected_version=entry.version,
            )
        registry_url = next(
            (source.url for source in self._sources if source.id == source_id), ""
        )
        await save_source(
            self.data_dir,
            manifest.name,
            {
                "install_method": "market",
                "registry_id": source_id,
                "registry_url": registry_url,
                "plugin_id": entry.plugin_id,
                "repo": entry.repo,
                "download_url": entry.download_url,
                "sha256": entry.sha256,
                "version": manifest.version,
                "installed_at": time.time(),
            },
        )
        return manifest.name, destination

    async def install(self, plugin_id: str, *, source_id: str = "official") -> tuple[str, Path]:
        if not self._sources:
            await self.refresh()
        entry = self.find(plugin_id, source_id)
        if entry is None:
            raise InstallError(f"插件不存在: {plugin_id}")
        return await self.install_from_entry(entry, source_id=source_id)


    async def uninstall(
        self,
        name: str,
        *,
        config_dir: str | Path,
        recycle_dir: str | Path,
        delete_config: bool = False,
        delete_data: bool = False,
    ) -> Path:
        from mohobot.plugins.installer import uninstall_plugin

        async with self._op_lock:
            target = await asyncio.to_thread(
                uninstall_plugin,
                name,
                self.plugins_dir,
                recycle_dir,
                config_dir=config_dir,
                data_dir=self.data_dir,
                delete_config=delete_config,
                delete_data=delete_data,
            )
            await remove_source(self.data_dir, name)
            return target

    async def update(self, plugin_id: str, *, source_id: str = "official") -> tuple[str, Path]:
        async with self._op_lock:
            return await self._update_unlocked(plugin_id, source_id=source_id)

    async def _update_unlocked(self, plugin_id: str, *, source_id: str = "official") -> tuple[str, Path]:
        if not self._sources:
            await self.refresh()
        records = await load_sources(self.data_dir)
        record_name = next(
            (
                name
                for name, item in records.items()
                if isinstance(item, dict)
                and (
                    item.get("plugin_id") == plugin_id
                    or item.get("market_plugin_id") == plugin_id
                )
            ),
            plugin_id.rsplit("/", 1)[-1],
        )
        record = records.get(record_name)
        if not record or record.get("install_method") != "market":
            raise InstallError("该插件不是通过插件市场安装，无法更新")
        source_id = str(record.get("registry_id") or source_id)
        entry = self.find(plugin_id, source_id)
        if entry is None:
            raise InstallError(f"插件源中已找不到 {plugin_id}")
        _check_entry_compatibility(entry)
        installed_version = str(record.get("version") or "")
        if installed_version:
            try:
                if Version(entry.version) <= Version(installed_version):
                    raise InstallError(
                        f"市场版本 {entry.version} 不高于已安装版本 {installed_version}"
                    )
            except InvalidVersion:
                pass
        with tempfile.TemporaryDirectory(prefix="mohobot-market-") as temp:
            archive = Path(temp) / "plugin.zip"
            await self._download_entry(entry, archive)
            if entry.sha256:
                await asyncio.to_thread(verify_sha256, archive, entry.sha256)
            manifest, destination = await asyncio.to_thread(
                upgrade_from_archive, archive, self.plugins_dir, entry.name
            )
        record = dict(record)
        record.update(
            {
                "version": entry.version,
                "sha256": entry.sha256,
                "installed_at": time.time(),
                "registry_id": source_id,
            }
        )
        await save_source(self.data_dir, manifest.name, record)
        return manifest.name, destination
