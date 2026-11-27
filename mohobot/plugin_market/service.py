from __future__ import annotations

import asyncio
import hashlib
import json
import math
import tempfile
import time
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal
from zipfile import BadZipFile

from loguru import logger
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from mohobot import __version__ as MOHOBOT_VERSION
from mohobot.api import api_version as PLUGIN_API_VERSION
from mohobot.plugin_market.journal import (
    MarketJournal,
    MarketOperation,
    MarketRecoveryError,
)
from mohobot.plugin_market.sources import (
    DEFAULT_SOURCE_ID,
    RegistryIdentity,
    RegistrySource,
    load_registry_sources,
    normalize_source_url,
    remove_registry_source,
    save_registry_source,
    validate_source_url,
)
from mohobot.plugins._atomic_json import finish_task, thread_until_done
from mohobot.plugins.installer import (
    InstallError,
    download_file,
    extract_plugin_archive,
    with_proxy,
)
from mohobot.plugins.manifest import (
    ManifestError,
    PluginManifest,
    check_compatibility,
    find_manifest,
    parse_manifest,
)
from mohobot.plugins.market import (
    MarketEntry,
    MarketIndex,
    entry_download_url,
    parse_market_index,
)
from mohobot.plugins.sources import SOURCE_SCHEMA_VERSION, load_sources_strict

if TYPE_CHECKING:
    from mohobot.interceptors.plugin_system import PluginSystem

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


def _extract_market_package(
    archive: Path, staging: Path, entry: MarketEntry,
) -> tuple[PluginManifest, Path]:
    if not entry.sha256:
        raise InstallError("市场条目缺少归档 SHA256")
    try:
        manifest, root = extract_plugin_archive(archive, staging, expected_sha256=entry.sha256)
        if manifest.plugin_id != entry.plugin_id:
            raise InstallError(f"归档身份不匹配: 期望 {entry.plugin_id}, 实际 {manifest.plugin_id}")
        if manifest.version != entry.version:
            raise InstallError(f"归档版本不匹配: 期望 {entry.version}, 实际 {manifest.version}")
        check_compatibility(manifest, api_version=PLUGIN_API_VERSION, mohobot_version=MOHOBOT_VERSION)
    except (ManifestError, BadZipFile) as exc:
        raise InstallError(f"归档无效: {exc}") from exc
    return manifest, root


def _installed_manifest(destination: Path, entry: MarketEntry) -> PluginManifest:
    path = find_manifest(destination)
    if path is None:
        raise InstallError(f"已安装插件缺少 metadata.yaml: {destination.name}")
    try:
        manifest = parse_manifest(path)
    except ManifestError as exc:
        raise InstallError(f"已安装插件清单无效: {exc}") from exc
    if manifest.plugin_id != entry.plugin_id:
        raise InstallError(f"已安装插件身份不匹配: 期望 {entry.plugin_id}, 实际 {manifest.plugin_id}")
    try:
        newer = Version(entry.version) > Version(manifest.version)
    except InvalidVersion as exc:
        raise InstallError(f"市场版本无效: {entry.version}") from exc
    if not newer:
        raise InstallError(f"市场版本 {entry.version} 不高于已安装版本 {manifest.version}")
    return manifest


async def _commit_disk_operation(operation: MarketOperation) -> None:
    """Complete the transaction for callers without a running PluginSystem."""
    try:
        await operation.apply()
        await operation.commit()
    except Exception:
        await operation.rollback()
        await operation.cleanup()
        raise
    await operation.cleanup()


@dataclass(frozen=True)
class _IndexState:
    status: Literal["fresh", "cached", "failed", "unattempted"]
    index: MarketIndex | None = None
    last_success_at: float | None = None


class MarketSourceChanged(InstallError):
    """The selected registry no longer has its original identity."""


def _cache_path(data_dir: Path, identity: RegistryIdentity) -> Path:
    digest = hashlib.sha256(json.dumps(asdict(identity), sort_keys=True).encode()).hexdigest()
    return data_dir / "cache" / "plugin_market" / f"registry-v1-{digest}.json"


def _read_index_cache(data_dir: Path, identity: RegistryIdentity) -> _IndexState:
    cache = _cache_path(data_dir, identity)
    try:
        with cache.open("rb") as handle:
            raw = handle.read(MAX_INDEX_BYTES + 4097)
        if len(raw) > MAX_INDEX_BYTES + 4096:
            raise ValueError("缓存索引超过大小限制")
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("registry") != asdict(identity):
            raise ValueError("缓存身份与当前插件源不符")
        ts = doc.get("last_success_at")
        if type(ts) not in (int, float) or not math.isfinite(ts) or ts <= 0:
            raise ValueError("缓存时间无效")
        return _IndexState("cached", parse_market_index(doc.get("index")), ts)
    except FileNotFoundError:
        return _IndexState("failed")
    except (OSError, ValueError, InstallError, OverflowError, RecursionError) as exc:
        logger.warning(f"插件源 {identity.id} 缓存不可用: {exc}")
        return _IndexState("failed")


def _write_index_cache(data_dir: Path, identity: RegistryIdentity, payload: dict, ts: float) -> None:
    cache = _cache_path(data_dir, identity)
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Bound the serialized index too: escaped input can expand on re-serialization.
    data = json.dumps({"registry": asdict(identity), "last_success_at": ts, "index": payload},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_INDEX_BYTES + 4096:
        raise ValueError("缓存索引超过大小限制")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache.parent, prefix=".index-", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(data)
        temp.replace(cache)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


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
        self._indexes: dict[RegistryIdentity, _IndexState] = {}
        self._sources: dict[str, RegistrySource] = {}
        self._refresh_order: dict[RegistryIdentity, int] = {}
        self._refresh_sequence = 0
        self._index_init_lock = asyncio.Lock()
        self._op_lock = asyncio.Lock()
        self._journal = MarketJournal(self.data_dir, self.plugins_dir)

    async def _read_installed_sources(self) -> dict:
        try:
            return await load_sources_strict(self.data_dir)
        except (OSError, ValueError) as exc:
            raise MarketRecoveryError(f"读取已安装插件来源失败: {exc}") from exc

    @property
    def sources(self) -> list[RegistrySource]:
        return list(self._sources.values())

    async def _reload_sources_locked(self) -> None:
        sources = await load_registry_sources(self.data_dir)
        self._sources = {source.id: source for source in sources}
        current = {source.identity for source in sources}
        self._indexes = {key: value for key, value in self._indexes.items() if key in current}
        self._refresh_order = {key: order for key, order in self._refresh_order.items() if key in current}

    async def reload_sources(self) -> list[RegistrySource]:
        async with self._op_lock:
            await self._reload_sources_locked()
            return self.sources

    async def add_source(self, *, source_id: str, name: str, url: str) -> RegistrySource:
        validated = await asyncio.to_thread(validate_source_url, url)

        async def commit():
            source = await save_registry_source(self.data_dir, source_id=source_id, name=name, validated_url=validated)
            await self._reload_sources_locked()
            return source

        async with self._op_lock:
            return await finish_task(commit())

    async def remove_source(self, source_id: str) -> None:
        async def commit():
            await remove_registry_source(self.data_dir, source_id)
            await self._reload_sources_locked()

        async with self._op_lock:
            await finish_task(commit())

    def source_status(self, source_id: str) -> dict[str, str | float | None]:
        source = self._sources.get(source_id)
        state = self._indexes.get(source.identity) if source is not None else None
        return {"status": state.status if state else "unattempted",
                "last_success_at": state.last_success_at if state else None}

    async def ensure_indexes(self) -> None:
        # Deduplicate concurrent first queries without blocking explicit refreshes.
        async with self._index_init_lock:
            await self._refresh(only_unattempted=True)

    async def refresh(self) -> list[RegistrySource]:
        return await self._refresh(only_unattempted=False)

    async def _refresh(self, *, only_unattempted: bool) -> list[RegistrySource]:
        async with self._op_lock:
            await self._reload_sources_locked()
            self._refresh_sequence += 1
            order = self._refresh_sequence
            identities = [source.identity for source in self._sources.values()
                          if not only_unattempted or source.identity not in self._indexes]
            for identity in identities:
                self._refresh_order[identity] = order
        for identity in identities:
            await self._refresh_index(identity, order)
        return self.sources

    def _fetch_index(self, identity: RegistryIdentity) -> tuple[dict, MarketIndex]:
        # Keep URL validation on the network path, outside the operation lock.
        url = identity.url if identity.id == DEFAULT_SOURCE_ID else validate_source_url(identity.url)
        if self.proxy:
            try:
                payload = self._fetcher(with_proxy(url, self.proxy))
                return payload, parse_market_index(payload)
            except Exception as exc:
                logger.warning(f"插件源 {identity.id} 代理刷新失败，尝试直连: {exc}")
        payload = self._fetcher(url)
        return payload, parse_market_index(payload)

    async def _refresh_index(self, identity: RegistryIdentity, order: int) -> None:
        payload = None
        try:
            payload, index = await asyncio.to_thread(self._fetch_index, identity)
            state = _IndexState("fresh", index, time.time())
        except Exception as exc:
            logger.warning(f"插件源 {identity.id} 刷新失败: {exc}")
            state = await asyncio.to_thread(_read_index_cache, self.data_dir, identity)
        async def publish():
            published = state
            prior = self._indexes.get(identity)
            if payload is not None:
                try:
                    await asyncio.to_thread(_write_index_cache, self.data_dir, identity, payload, state.last_success_at)
                except (OSError, ValueError, TypeError, RecursionError) as exc:
                    logger.warning(f"插件源 {identity.id} 缓存写入失败: {exc}")
            elif prior is not None and prior.last_success_at is not None and (
                state.last_success_at is None or prior.last_success_at >= state.last_success_at
            ):
                published = replace(prior, status="cached")
            self._indexes[identity] = published

        async with self._op_lock:
            await self._reload_sources_locked()
            if self._refresh_order.get(identity) == order:
                await finish_task(publish())

    def entries(self, source_id: str | None = None) -> list[MarketEntry]:
        seen: set[str] = set()
        result: list[MarketEntry] = []
        for source in self._sources.values():
            if source_id is not None and source.id != source_id:
                continue
            state = self._indexes.get(source.identity)
            if state is None or state.index is None:
                continue
            for entry in state.index.plugins.values():
                if entry.plugin_id in seen:
                    continue
                seen.add(entry.plugin_id)
                result.append(replace(entry, source_id=source.id, source_url=source.url,
                                      source_generation=source.generation))
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
            await thread_until_done(download_file, proxy_url, target)
        except InstallError:
            if not self.proxy:
                raise
            await thread_until_done(download_file, url, target)

    @asynccontextmanager
    async def _prepare_package(self, entry: MarketEntry):
        _check_entry_compatibility(entry)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".market-prepare-", dir=self.plugins_dir) as temp:
            staging = Path(temp)
            archive = staging / "plugin.zip"
            await self._download_entry(entry, archive)
            manifest, root = await thread_until_done(_extract_market_package, archive, staging / "extract", entry)
            yield manifest, root

    def _check_current_source(self, entry: MarketEntry) -> None:
        source = self._sources.get(entry.source_id)
        if source is None or source.identity != RegistryIdentity(entry.source_id, entry.source_url, entry.source_generation):
            raise MarketSourceChanged(f"插件源 {entry.source_id} 已变更或删除，请刷新市场后重试")

    @staticmethod
    def _source_record(entry: MarketEntry, manifest: PluginManifest) -> dict:
        return {
            "schema_version": SOURCE_SCHEMA_VERSION,
            "install_method": "market",
            "registry_id": entry.source_id,
            "registry_url": entry.source_url,
            "registry_generation": entry.source_generation,
            "plugin_id": manifest.plugin_id,
            "repo": entry.repo,
            "download_url": entry_download_url(entry),
            "sha256": entry.sha256,
            "version": manifest.version,
            "installed_at": time.time(),
        }

    def _check_runtime_paths(self, plugin_system: PluginSystem) -> None:
        if (plugin_system.data_dir.resolve() != self._journal.data_dir
                or plugin_system.installed_dir.resolve() != self._journal.plugins_dir):
            raise MarketRecoveryError("插件市场与运行时目录不一致，拒绝变更")

    async def _replace(self, manifest, root, record, *, expect_installed, recheck, plugin_system):
        async with self._op_lock:
            if plugin_system is not None:
                self._check_runtime_paths(plugin_system)
                result = await plugin_system.replace_market_plugin(
                    manifest, root, record, expect_installed=expect_installed, recheck=recheck,
                )
                return manifest.name, self.plugins_dir / manifest.name, result

            async def commit():
                await recheck()
                operation = await self._journal.begin_replace(
                    manifest.name, root, record, expect_installed=expect_installed,
                )
                await _commit_disk_operation(operation)

            await finish_task(commit())
        return manifest.name, self.plugins_dir / manifest.name

    async def install_from_entry(
        self,
        entry: MarketEntry,
        *,
        source_id: str = "official",
        plugin_system: PluginSystem | None = None,
    ) -> tuple[str, Path] | tuple[str, Path, dict]:
        # Direct callers must either pass a frozen selection or match the current index.
        if not entry.source_generation:
            await self.ensure_indexes()
            found = self.find(entry.plugin_id, entry.source_id or source_id)
            if found is None or replace(found, source_id=entry.source_id, source_url=entry.source_url,
                                         source_generation="") != entry:
                raise MarketSourceChanged("市场条目已变更，请刷新后重试")
            entry = found
        async with self._prepare_package(entry) as (manifest, root):
            async def recheck():
                await self._reload_sources_locked()
                self._check_current_source(entry)

            return await self._replace(
                manifest, root, self._source_record(entry, manifest), expect_installed=False,
                recheck=recheck, plugin_system=plugin_system,
            )

    async def install(
        self, plugin_id: str, *, source_id: str = "official", source_generation: str | None = None,
        plugin_system: PluginSystem | None = None,
    ) -> tuple[str, Path] | tuple[str, Path, dict]:
        await self.ensure_indexes()
        source = self._sources.get(source_id)
        if source_generation is not None and (source is None or source.generation != source_generation):
            raise MarketSourceChanged("插件源已变更，请刷新市场后重试")
        entry = self.find(plugin_id, source_id)
        if entry is None:
            raise InstallError(f"插件不存在: {plugin_id}")
        return await self.install_from_entry(entry, source_id=source_id, plugin_system=plugin_system)


    async def uninstall(
        self,
        name: str,
        *,
        config_dir: str | Path,
        recycle_dir: str | Path,
        delete_config: bool = False,
        delete_data: bool = False,
        plugin_system: PluginSystem | None = None,
    ) -> Path | tuple[Path, dict]:
        if (Path(config_dir).absolute() != self._journal.data_dir / "plugins_config"
                or Path(recycle_dir).absolute() != self._journal.data_dir / "plugin_recycle"):
            raise ValueError("卸载必须使用宿主配置目录和回收目录")

        async def commit():
            operation = await self._journal.begin_uninstall(name, delete_config=delete_config, delete_data=delete_data)
            await _commit_disk_operation(operation)
            return operation.recycle_path

        async with self._op_lock:
            if plugin_system is not None:
                self._check_runtime_paths(plugin_system)
                return await plugin_system.uninstall_market_plugin(name, delete_config=delete_config, delete_data=delete_data)
            return await finish_task(commit())

    async def update(
        self,
        plugin_id: str,
        *,
        source_id: str = "official",
        source_generation: str | None = None,
        before_replace=None,
        plugin_system: PluginSystem | None = None,
    ) -> tuple[str, Path] | tuple[str, Path, dict]:
        await self.ensure_indexes()
        records = await self._read_installed_sources()
        record_name = plugin_id.rsplit("/", 1)[-1]
        record = records.get(record_name)
        if not isinstance(record, dict) or record.get("install_method") != "market":
            raise InstallError("该插件不是通过插件市场安装，无法更新")
        recorded_plugin_id = record.get("plugin_id") or record.get("market_plugin_id")
        if recorded_plugin_id and recorded_plugin_id != plugin_id:
            raise InstallError("来源记录的插件身份与请求不匹配")
        registry_id = record.get("registry_id")
        source = self._sources.get(registry_id) if isinstance(registry_id, str) else None
        if source is None:
            raise MarketSourceChanged("已安装插件的来源已删除或缺失，无法确认更新来源")
        self._check_recorded_source(record, source)
        if source_generation is not None and source_generation != source.generation:
            raise MarketSourceChanged("插件源已变更，请刷新市场后重试")
        entry = self.find(plugin_id, source.id)
        if entry is None:
            raise InstallError(f"插件源中已找不到 {plugin_id}")
        destination = self.plugins_dir / entry.name
        old_manifest = _installed_manifest(destination, entry)
        if plugin_system is not None and before_replace is not None:
            raise ValueError("运行时更新不接受外部停用回调")
        async with self._prepare_package(entry) as (manifest, root):
            async def recheck():
                await self._reload_sources_locked()
                self._check_current_source(entry)
                if (await self._read_installed_sources()).get(record_name) != record:
                    raise MarketSourceChanged("下载期间插件来源记录已变化，请重试")
                if _installed_manifest(destination, entry) != old_manifest:
                    raise InstallError("下载期间已安装插件发生变化，请重试")
                if before_replace is not None:
                    await before_replace(entry.name)

            return await self._replace(
                manifest, root, {**record, **self._source_record(entry, manifest)}, expect_installed=True,
                recheck=recheck, plugin_system=plugin_system,
            )

    @staticmethod
    def _check_recorded_source(record: dict, source: RegistrySource) -> None:
        recorded_url = record.get("registry_url")
        generation = record.get("registry_generation")
        try:
            mismatched_url = bool(recorded_url) and normalize_source_url(recorded_url) != source.url
        except (TypeError, ValueError, AttributeError):
            mismatched_url = True
        if mismatched_url or (generation and (generation != source.generation or not recorded_url)):
            raise MarketSourceChanged("已安装插件的来源身份已变化，拒绝从其他来源更新")
