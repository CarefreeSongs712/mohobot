"""Minimal plugin system — scans a plugins/ directory for Python files
with a 'Plugin' class and hooks into notice/meta events.

Each plugin file should define:
    class Plugin:
        async def on_notice(self, bot_id: str, event, raw_event) -> None: ...
        async def on_meta(self, bot_id: str, event, raw_event) -> None: ...
        async def on_message(self, bot_id: str, event, raw_event) -> tuple[bool, str | None]: ...

Plugins can be enabled/disabled via data/plugins_state.json (managed by the web panel).
"""

from __future__ import annotations

from collections.abc import Callable
import asyncio
import importlib
import importlib.util
import inspect
import json
import re
import shutil
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from loguru import logger

from mohobot import __version__ as MOHOBOT_VERSION
from mohobot.api import api_version as PLUGIN_API_VERSION
from mohobot.interceptors.base import Interceptor
from mohobot.models.onebot import (
    MessageEvent,
    MetaEvent,
    NoticeEvent,
)
from mohobot.plugin_market.journal import (
    MarketJournal,
    MarketOperation,
    MarketRecoveryError,
)
from mohobot.plugins._atomic_json import finish_task
from mohobot.plugins.imports import PluginImports, namespace_conflict
from mohobot.plugins.installer import (
    STAGING_MAX_AGE_SECONDS,
    STAGING_PREFIXES,
    InstallError,
)
from mohobot.plugins.manifest import (
    ManifestError,
    PluginManifest,
    check_compatibility,
    find_manifest,
    parse_manifest,
)
from mohobot.services.task_supervisor import TaskSupervisorClosed
from mohobot.utils.cq_code import extract_plain_text as _extract_plain_text


if TYPE_CHECKING:
    from mohobot.services.llm_tools import PluginToolStage


class PluginNamespaceConflict(ValueError):
    """A plugin operation would overwrite an occupied import namespace."""


class PluginSystemRetired(RuntimeError):
    """The plugin system is no longer accepting mutations."""


class PluginNotFound(InstallError):
    """The requested installed or discovered plugin does not exist."""


@dataclass
class _MarketRecovery:
    name: str
    operation: MarketOperation
    candidate: dict[str, Any] | None = None
    restoring_previous: bool = False
    previous: dict[str, Any] | None = None


class PluginConfigCommit(TypedDict):
    config: dict
    application: Literal["applied", "not_running", "apply_failed", "unchanged"]


class _PluginTaskSupervisor:
    """把插件显式使用的 owner="plugins" 映射为按插件隔离的 owner。"""

    def __init__(
        self, supervisor: Any, key: str, admission_closed: Callable[[], bool],
        in_hook: Callable[[], bool],
    ) -> None:
        self._supervisor = supervisor
        self._key = key
        self._admission_closed = admission_closed
        self._in_hook = in_hook

    @property
    def stopping(self) -> bool:
        return (self._admission_closed() and not self._in_hook()) or bool(self._supervisor.stopping)

    def _owner(self, owner: str) -> str:
        if not owner or owner == "plugins":
            return self._key
        return f"{self._key}:{owner.removeprefix('plugin:')}"

    def create_task(self, coro: Any, *, name: str, owner: str = "application") -> Any:
        if self._admission_closed() and not self._in_hook():
            coro.close()
            raise TaskSupervisorClosed("plugin instance is not accepting background tasks")
        return self._supervisor.create_task(coro, name=name, owner=self._owner(owner))

    async def cancel_owner(self, owner: str, timeout: float = 5.0) -> None:
        await self._supervisor.cancel_owner(self._owner(owner), timeout)

    async def cancel_owner_prefix(self, prefix: str, timeout: float = 5.0) -> None:
        owner = self._owner(prefix)
        if owner == self._key:
            await self._supervisor.cancel_owner_tree(owner, timeout)
        else:
            await self._supervisor.cancel_owner_prefix(owner, timeout)


class PluginSystem(Interceptor):
    """Loads and manages plugins from a designated directory."""

    def __init__(
        self,
        plugins_dir: str = "./plugins",
        data_dir: str = "./data",
        installed_dir: str | None = None,
    ):
        self._plugins_dir = Path(plugins_dir)
        self._data_dir = Path(data_dir)
        self._installed_dir = (
            Path(installed_dir) if installed_dir else self._data_dir / "plugins"
        )
        self._plugins: list[dict[str, Any]] = []  # Plugin metadata + instance
        # {plugin_name: {"enabled": bool, "file": str, "loaded": bool, "info": {...}}}
        self._state_file = self._data_dir / "plugins_state.json"
        # 插件配置目录(全局一份): data/plugins_config/{name}.json
        self._config_dir = Path(data_dir) / "plugins_config"
        # 运行时注入引用(热重载后重新注入)
        self._ws_server = None
        self._bot_manager = None
        self._anysearch_client = None
        self._llm_service = None
        self._emotion_manager = None
        self._context_manager = None
        self._persona_service = None
        self._message_handler = None
        self._admin_ids: set[str] = set()  # 全局管理员(封禁/插件命令共用)
        # 周期任务(插件声明 interval_sec + async def on_tick): 后台循环
        self._tick_tasks: list[Any] = []
        self._tick_tasks_by_name: dict[str, Any] = {}
        self._stopping_tick_tasks: set[asyncio.Task[Any]] = set()
        self._task_supervisor = None
        self._matcher = None
        self._reload_lock: asyncio.Lock | None = None
        self._admission_closed = False
        self._plugin_api_version = PLUGIN_API_VERSION
        self._active_dispatches = 0
        self._dispatch_drained = asyncio.Event()
        self._dispatch_drained.set()
        self._dispatch_tasks: dict[asyncio.Task[Any], int] = {}
        self._dispatch_drain_timeout = 5.0
        self._drain_timeout = 5.0
        self._hook_timeout = 30.0
        self._market_recovery: _MarketRecovery | None = None
        self._market_journal = MarketJournal(self._data_dir, self._installed_dir)

    @property
    def plugins_dir(self) -> Path:
        return self._plugins_dir

    @property
    def installed_dir(self) -> Path:
        return self._installed_dir

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def _lock(self) -> asyncio.Lock:
        if self._reload_lock is None:
            self._reload_lock = asyncio.Lock()
        return self._reload_lock

    @staticmethod
    def _module_name(plugin_name: str) -> str:
        return "mohobot_plugin_" + re.sub(r"[^0-9A-Za-z_]", "_", plugin_name)

    def _module_name_conflict(self, module_name: str, exclude_name: str | None = None) -> str | None:
        for meta in self._plugins:
            if not meta.get("loaded"):
                continue
            if exclude_name is not None and meta.get("name") == exclude_name:
                continue
            if meta.get("module_name") == module_name:
                return str(meta.get("name") or "?")
        replacing = next(
            (meta.get("_imports") for meta in self._plugins if meta["name"] == exclude_name),
            None,
        )
        return namespace_conflict(module_name, replacing)

    def _unload_plugin(self, meta: dict[str, Any]) -> None:
        name = meta.get("name", "")
        entry_dir = meta.get("entry_dir") or ""
        imports = meta.pop("_imports", None)
        if imports is not None:
            imports.detach()
        if entry_dir:
            for cache_dir in Path(entry_dir).rglob("__pycache__"):
                try:
                    shutil.rmtree(cache_dir, ignore_errors=True)
                except FileNotFoundError:
                    # Python 3.12 can propagate fstat failure on a vanished directory.
                    continue
        else:
            cache_dir = self._plugins_dir / "__pycache__"
            if cache_dir.is_dir():
                for cache_file in cache_dir.glob(f"{name}.*.pyc"):
                    cache_file.unlink(missing_ok=True)
        importlib.invalidate_caches()
        meta["instance"] = None
        meta["loaded"] = False

    @staticmethod
    def _validate_plugin(instance: Any) -> None:
        for hook_name in (
            "on_message",
            "on_message_observed",
            "on_notice",
            "on_meta",
            "on_request",
            "on_perception",
            "on_tick",
        ):
            hook = getattr(instance, hook_name, None)
            if hook is not None and not inspect.iscoroutinefunction(hook):
                raise ManifestError(f"{hook_name} 必须是 async 函数")
        updater = getattr(instance, "on_config_update", None)
        if updater is not None and inspect.iscoroutinefunction(updater):
            raise ManifestError("on_config_update 必须是同步函数")
        tick = getattr(instance, "on_tick", None)
        interval = getattr(instance, "interval_sec", None)
        if tick is not None and interval is not None:
            try:
                seconds = float(interval)
            except (TypeError, ValueError) as exc:
                raise ManifestError("interval_sec 必须是正数") from exc
            if seconds <= 0:
                raise ManifestError("interval_sec 必须是正数")

    def _load_one(
        self,
        entry_path: Path,
        plugin_name: str,
        entry_dir: str,
        state: dict[str, Any],
        source: str = "builtin",
    ) -> dict[str, Any]:
        # Discovery passes the package root for directory plugins, even when the entry is nested.
        plugin_dir = Path(entry_dir) if entry_dir else entry_path.parent
        manifest: PluginManifest | None = None
        manifest_path = find_manifest(plugin_dir) if entry_dir else None
        if manifest_path is not None:
            manifest = parse_manifest(manifest_path)
            plugin_name = manifest.name
            relative_entry = entry_path.relative_to(plugin_dir).as_posix()
            if manifest.entry != relative_entry:
                raise ManifestError(
                    f"metadata entry={manifest.entry} 与入口 {relative_entry} 不一致"
                )
            check_compatibility(
                manifest,
                api_version=self._plugin_api_version,
                mohobot_version=MOHOBOT_VERSION,
            )
            if source == "installed" and manifest.name != plugin_dir.name:
                raise ManifestError(
                    f"metadata name={manifest.name} 与安装目录 {plugin_dir.name} 不一致"
                )
        elif source == "installed":
            raise ManifestError("已安装插件缺少 metadata.yaml")

        root = self._installed_dir if source == "installed" else self._plugins_dir
        meta: dict[str, Any] = {
            "name": plugin_name,
            "file": entry_path.relative_to(root).as_posix(),
            "entry_dir": entry_dir,
            "enabled": state.get(plugin_name, {}).get("enabled", True),
            "loaded": False,
            "error": None,
            "info": {},
            "config_schema": None,
            "config": None,
            "loaded_at": None,
            "source": source,
            "version": manifest.version if manifest else "",
            "author": manifest.author if manifest else "",
            "plugin_id": manifest.plugin_id if manifest else "",
            "api_version": manifest.api_version if manifest else 0,
            "manifest": manifest,
            "module_name": self._module_name(plugin_name),
        }
        from mohobot.services.llm_tools import registry

        registry.set_owner_state_path(plugin_name, self._state_file)
        # schema 解析不依赖模块导入: 禁用插件也必须在面板里可见并可编辑配置
        # (#170 审查 P1)。坏 schema 与基线一致地降级为无配置。
        try:
            meta["config_schema"] = self._load_config_schema(plugin_dir)
        except Exception as exc:
            logger.warning(f"Plugin {plugin_name} config schema load failed: {exc}")
        if not meta["enabled"]:
            # llm_tools preloads built-in plugin modules before this system is
            # constructed.  Disabled plugins must not leave those unowned
            # tools callable outside the lifecycle registry.
            registry.take_unowned_plugin_tools(plugin_name)
            logger.info(f"Plugin {plugin_name} skipped (disabled)")
            return meta

        module_name = meta["module_name"]
        collision = namespace_conflict(module_name)
        if collision is not None:
            raise ManifestError(f"插件模块名冲突: {plugin_name} 与 {collision}")
        imports = PluginImports(module_name, entry_dir)
        meta["_imports"] = imports
        module = None
        tool_stage = registry.new_plugin_stage(plugin_name, self._state_file)
        meta["_llm_tool_stage"] = tool_stage
        tool_stage.tools.update(registry.take_unowned_plugin_tools(plugin_name))
        try:
            imports.attach()
            spec = importlib.util.spec_from_file_location(
                module_name,
                entry_path,
                submodule_search_locations=[imports.path] if imports.path is not None else None,
            )
            if spec is None or spec.loader is None:
                raise ManifestError("spec load failed")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            imports.modules[module_name] = module
            with registry.registration_scope(tool_stage):
                spec.loader.exec_module(module)
            with registry.registration_scope(tool_stage):
                plugin_instance = None
                for name, cls in inspect.getmembers(module, inspect.isclass):
                    if name == "Plugin" and cls.__module__ == module.__name__:
                        plugin_instance = cls()
                        break
                if plugin_instance is None:
                    raise ManifestError("no Plugin class found")
                self._validate_plugin(plugin_instance)

                meta["instance"] = plugin_instance
                meta["_ready"] = False
                meta["loaded"] = True
                meta["loaded_at"] = time.time()
                meta["info"] = {
                    "description": inspect.getdoc(plugin_instance.__class__) or "",
                }
                if manifest is not None:
                    meta["info"]["display_name"] = manifest.display_name
                    meta["info"]["version"] = manifest.version
                cls_info = getattr(plugin_instance.__class__, "info", None)
                if isinstance(cls_info, dict):
                    meta["info"].update(cls_info)

                schema = meta.get("config_schema") or self._load_config_schema(
                    plugin_dir,
                )
                if schema:
                    meta["config_schema"] = schema
                meta["_llm_tool_import_tools"] = dict(tool_stage.tools)
                logger.info(f"Loaded plugin: {plugin_name}")
        except BaseException as e:
            registry.discard_plugin_stage(tool_stage)
            self._unload_plugin(meta)
            if not isinstance(e, Exception):
                raise
            meta["error"] = str(e)
            logger.error(f"Failed to load plugin {entry_path.name}: {e}")
        return meta

    def _iter_entries(self) -> list[tuple[Path, str, str, str]]:
        entries: list[tuple[Path, str, str, str]] = []
        installed_names: set[str] = set()
        for root, source in (
            (self._installed_dir, "installed"),
            (self._plugins_dir, "builtin"),
        ):
            if not root.exists():
                continue
            for item in sorted(root.iterdir()):
                if item.is_file():
                    if source == "installed":
                        # 已安装插件统一使用目录形态，避免 metadata 与安装目录名无法对应。
                        continue
                    if (
                        item.suffix != ".py"
                        or item.name.startswith(("_", "."))
                        or item.name == "__init__.py"
                    ):
                        continue
                    name = item.stem
                    if name in installed_names:
                        continue
                    entries.append((item, name, "", source))
                    continue
                if not item.is_dir():
                    continue
                if item.name.startswith(("_", ".", "__pycache__")):
                    continue
                manifest_path = find_manifest(item)
                entry_name = "main.py"
                display_name = item.name
                if manifest_path is not None:
                    try:
                        manifest = parse_manifest(manifest_path)
                        entry_name = manifest.entry
                        display_name = manifest.name
                    except Exception:
                        entry_name = "main.py"
                entry_file = item / entry_name
                if not entry_file.is_file():
                    continue
                if source == "builtin" and display_name in installed_names:
                    continue
                if source == "installed":
                    installed_names.add(display_name)
                entries.append((entry_file, display_name, str(item), source))
        return entries

    def _error_meta(
        self,
        entry_path: Path,
        plugin_name: str,
        entry_dir: str,
        source: str,
        error: Exception | str,
    ) -> dict[str, Any]:
        root = self._installed_dir if source == "installed" else self._plugins_dir
        return {
            "name": plugin_name,
            "file": entry_path.relative_to(root).as_posix(),
            "entry_dir": entry_dir,
            "enabled": False,
            "loaded": False,
            "error": str(error),
            "info": {},
            "config_schema": None,
            "config": None,
            "loaded_at": None,
            "source": source,
            "version": "",
            "author": "",
            "plugin_id": "",
            "api_version": 0,
            "manifest": None,
            "module_name": self._module_name(plugin_name),
        }

    async def _load_all_unlocked(self) -> int:
        from mohobot.services.llm_tools import registry

        owners = list(dict.fromkeys(str(meta.get("name", "")) for meta in self._plugins))
        owner_states = await registry.deactivate_owners(
            owners, cancel_on_timeout=True
        )
        try:
            await self._wait_for_dispatches(cancel_on_timeout=True)
        except BaseException:
            registry.restore_owner_activity(owner_states)
            raise
        for owner in owners:
            registry.unregister_owner(owner)
        await self.stop_tick_tasks_async()
        self._plugins = []
        for directory in (self._plugins_dir, self._installed_dir):
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                logger.info(f"Created plugins directory: {directory}")
        self._cleanup_stale_staging()

        state = self._load_state()
        from mohobot.plugins.sources import load_sources

        sources = await load_sources(self._data_dir)
        seen: set[str] = set()
        for entry_path, plugin_name, entry_dir, source in self._iter_entries():
            if plugin_name in seen:
                self._plugins.append(
                    {
                        "name": plugin_name,
                        "file": str(entry_path.name),
                        "entry_dir": entry_dir,
                        "enabled": False,
                        "loaded": False,
                        "error": "插件名与已加载插件冲突",
                        "info": {},
                        "config_schema": None,
                        "config": None,
                        "loaded_at": None,
                        "source": source,
                        "version": "",
                        "author": "",
                        "plugin_id": "",
                        "api_version": 0,
                        "manifest": None,
                        "module_name": self._module_name(plugin_name),
                    }
                )
                logger.error(f"插件名冲突: {plugin_name} ({entry_path})")
                continue
            seen.add(plugin_name)
            module_name = self._module_name(plugin_name)
            previous = self._module_name_conflict(module_name)
            if previous is not None:
                logger.error(f"插件模块名冲突: {plugin_name} 与 {previous}")
                meta = self._error_meta(
                    entry_path, plugin_name, entry_dir, source,
                    ValueError(f"插件模块名冲突: {plugin_name} 与 {previous}"),
                )
            else:
                try:
                    meta = self._load_one(entry_path, plugin_name, entry_dir, state, source)
                except Exception as e:
                    meta = self._error_meta(entry_path, plugin_name, entry_dir, source, e)
                    logger.error(f"Failed to load plugin {entry_path.name}: {e}")
            record = sources.get(meta["name"])
            if record:
                meta["source_record"] = record
            self._plugins.append(meta)

        await self._startup_plugins_unlocked()
        count = sum(self._can_dispatch(meta) for meta in self._plugins)
        logger.info(f"Plugin system loaded {count} plugin(s)")
        return count

    def _cleanup_stale_staging(self) -> None:
        cutoff = time.time() - STAGING_MAX_AGE_SECONDS
        for root in (self._plugins_dir, self._installed_dir):
            if not root.exists():
                continue
            for path in root.iterdir():
                if not path.is_dir() or not path.name.startswith(STAGING_PREFIXES):
                    continue
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    shutil.rmtree(path, ignore_errors=True)
                    logger.warning(f"Removed stale plugin staging directory: {path}")
                except OSError as e:
                    logger.warning(f"Failed to remove stale staging directory {path}: {e}")

    async def _shutdown_unlocked(self) -> None:
        from mohobot.services.llm_tools import registry

        targets = tuple(meta for meta in self._plugins if meta.get("loaded"))
        owners = list(dict.fromkeys(str(meta.get("name", "")) for meta in self._plugins))
        owner_states = await registry.deactivate_owners(owners, cancel_on_timeout=True)
        try:
            await self._pause_dispatches(targets)
        except BaseException:
            registry.restore_owner_activity(owner_states)
            raise
        await self.stop_tick_tasks_async()
        if self._task_supervisor is not None:
            await self._task_supervisor.cancel_owner("plugins")
            await self._task_supervisor.cancel_owner_prefix("plugin:")
        failed_shutdowns: list[str] = []
        for meta in reversed(targets):
            try:
                await self._retire_plugin(meta)
            except Exception as exc:  # 单插件 hook 超时/失败不得中断其余插件
                failed_shutdowns.append(str(meta.get("name", "?")))
                logger.error(
                    "Plugin {} shutdown did not finish: {}",
                    meta.get("name", "?"), exc,
                )
                # 保留归属与 hook 任务供重试, 但释放模块名, 避免后续加载被旧模块阻塞。
                imports = meta.pop("_imports", None)
                if imports is not None:
                    imports.detach()
                continue
            self._unload_plugin(meta)
            meta.pop("_rollback", None)
        # 启动失败候选从未激活, 但保留的模块归属会挡住下一次全量加载;
        # 全量重载/关闭前统一释放, 避免 reload_plugins() 把自身残留判成
        # 模块名冲突并丢失所有权 (#166 审查 P1)。
        for meta in self._plugins:
            if not meta.get("loaded") and meta.get("_imports") is not None:
                self._unload_plugin(meta)
                meta.pop("_rollback", None)
        if failed_shutdowns:
            logger.warning(
                "Plugin shutdown incomplete for: {} (ownership kept for retry)",
                ", ".join(sorted(failed_shutdowns)),
            )
        if self._task_supervisor is not None:
            await self._task_supervisor.cancel_owner_prefix("plugin:")
        # Candidates whose startup failed keep their imported modules for retry;
        # shutdown must still detach them or the next load sees phantom modules.
        for meta in tuple(self._plugins):
            self._unload_plugin(meta)

    @staticmethod
    def _owner_key(meta: dict[str, Any]) -> str:
        # Escape the delimiter and the escape character in legacy names.
        name = meta["name"].replace("%", "%25").replace(":", "%3A")
        return f"plugin:{name}"

    async def _drain_plugin_tasks(self, meta: dict[str, Any]) -> None:
        if self._task_supervisor is not None:
            pending = await self._task_supervisor.cancel_owner_tree(
                self._owner_key(meta), timeout=self._drain_timeout,
            )
            if pending:
                raise TimeoutError(f"plugin {meta['name']} still owns background tasks")

    async def _retire_plugin(self, meta: dict[str, Any]) -> None:
        # Admission and events must already be drained before this point. Never
        # reopen an instance after beginning cancellation or its shutdown hook.
        from mohobot.services.llm_tools import registry

        meta["_retiring"] = True
        await self._stop_tick_for(meta["name"])
        if meta.get("_hook_name") == "on_startup":
            task = meta["_hook_task"]
            if not task.done():
                done, _ = await asyncio.wait({task}, timeout=self._drain_timeout)
                if not done:
                    raise TimeoutError(f"plugin {meta['name']} startup is still active")
            if not task.cancelled():
                task.exception()
            meta.pop("_hook_task")
            meta.pop("_hook_name")
        if not meta.get("_shutdown_done"):
            # A previous waiter may have cancelled the hook. Once it has
            # actually exited, drain its children before retrying shutdown.
            if meta.get("_hook_name") == "on_shutdown" and meta["_hook_task"].cancelled():
                meta.pop("_hook_task")
                meta.pop("_hook_name")
            if meta.get("_hook_name") != "on_shutdown":
                await self._drain_plugin_tasks(meta)
            if not await self._call_hook(meta, "on_shutdown"):
                # Keep ownership and the recovery intent: callers retry later.
                raise RuntimeError(f"plugin {meta['name']} on_shutdown failed")
            meta["_shutdown_done"] = True
        await self._drain_plugin_tasks(meta)
        registry.unregister_owner(str(meta.get("name", "")))

    async def _reload_unlocked(self) -> int:
        self._check_reload_namespaces(self._load_state())
        await self._shutdown_unlocked()
        return await self._load_all_unlocked()

    def _check_reload_namespaces(self, state: dict[str, Any]) -> None:
        owners = {
            imports.namespace: imports
            for meta in self._plugins
            if (imports := meta.get("_imports")) is not None
        }
        candidates: dict[str, str] = {}
        seen: set[str] = set()
        for _, name, _, _ in self._iter_entries():
            if name in seen:
                continue
            seen.add(name)
            if not state.get(name, {}).get("enabled", True):
                continue
            namespace = self._module_name(name)
            conflict = candidates.get(namespace) or namespace_conflict(namespace, owners.get(namespace))
            if conflict is not None:
                raise PluginNamespaceConflict(f"插件模块名冲突: {name} 与 {conflict}")
            candidates[namespace] = name

    def set_song_matcher(self, matcher) -> None:
        """注入全局歌曲匹配器, 供同步插件刷新内存索引。"""
        self._matcher = matcher

    def set_task_supervisor(self, supervisor) -> None:
        """注入应用级后台任务监督器(可选, 便于统一关闭插件任务)。"""
        self._task_supervisor = supervisor

    def _can_dispatch(self, meta: dict[str, Any]) -> bool:
        return (
            not self._admission_closed
            and meta.get("enabled", False)
            and meta.get("loaded", False)
            and meta.get("_ready", True)
        )

    async def _run_plugin_handler(self, meta, handler, *args, default=None):
        if not self._can_dispatch(meta):
            return default
        task = asyncio.current_task()
        drained = meta.get("_dispatch_drained")
        if drained is None:
            drained = meta["_dispatch_drained"] = asyncio.Event()
        meta["_active_dispatches"] = meta.get("_active_dispatches", 0) + 1
        self._active_dispatches += 1
        self._dispatch_drained.clear()
        drained.clear()
        if task is not None:
            self._dispatch_tasks[task] = self._dispatch_tasks.get(task, 0) + 1
        try:
            return await handler(*args)
        finally:
            self._active_dispatches -= 1
            meta["_active_dispatches"] = max(0, meta.get("_active_dispatches", 1) - 1)
            if meta["_active_dispatches"] <= 0:
                drained.set()
            if self._active_dispatches <= 0:
                self._dispatch_drained.set()
            if task is not None:
                task_count = self._dispatch_tasks.get(task, 0) - 1
                if task_count <= 0:
                    self._dispatch_tasks.pop(task, None)
                else:
                    self._dispatch_tasks[task] = task_count

    async def _wait_for_dispatches(self, *, cancel_on_timeout: bool = False) -> None:
        if self._active_dispatches <= 0:
            return
        try:
            await asyncio.wait_for(
                self._dispatch_drained.wait(), timeout=self._dispatch_drain_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Plugin dispatch barrier timed out with {self._active_dispatches} active handler(s)"
            )
            if not cancel_on_timeout:
                raise
            current_task = asyncio.current_task()
            for task in tuple(self._dispatch_tasks):
                if task is not current_task and not task.done():
                    task.cancel()
            await asyncio.wait_for(
                self._dispatch_drained.wait(), timeout=self._dispatch_drain_timeout
            )

    async def _pause_dispatches(self, targets: tuple[dict[str, Any], ...]) -> None:
        ready = [meta for meta in targets if meta.get("_ready", True)]
        for meta in targets:
            meta["_ready"] = False
        try:
            # A target cannot admit another callback while these counters drain.
            async with asyncio.timeout(self._drain_timeout):
                for meta in targets:
                    if meta.get("_active_dispatches"):
                        await meta["_dispatch_drained"].wait()
        except BaseException:
            if not self._admission_closed:
                for meta in ready:
                    meta["_ready"] = True
            raise

    async def _call_hook(
        self,
        meta: dict[str, Any],
        hook_name: str,
        registration_stage: PluginToolStage | None = None,
    ) -> bool:
        """Keep a timed-out or cancelled hook owned until it actually exits.

        Returns False for an ordinary hook failure; ownership failures
        (timeout / cancellation) still raise so the hook stays tracked.
        """
        task = meta.get("_hook_task")
        if task is None:
            plugin = meta.get("instance")
            hook = getattr(plugin, hook_name, None)
            if not callable(hook):
                return True

            async def invoke():
                if registration_stage is None:
                    result = hook()
                    if inspect.isawaitable(result):
                        await result
                else:
                    from mohobot.services.llm_tools import registry

                    with registry.registration_scope(registration_stage):
                        result = hook()
                        if inspect.isawaitable(result):
                            await result

            task = asyncio.create_task(invoke(), name=f"plugin:{meta['name']}:{hook_name}")
            meta["_hook_task"] = task
            meta["_hook_name"] = hook_name
        try:
            done, _ = await asyncio.wait({task}, timeout=self._hook_timeout)
        except asyncio.CancelledError:
            if not task.done() and not task.cancelling():
                task.cancel()
            raise
        if not done:
            if not task.cancelling():
                task.cancel()
            raise TimeoutError(f"plugin {meta['name']} {hook_name} is still active")
        meta.pop("_hook_task")
        meta.pop("_hook_name")
        if task.cancelled():
            raise RuntimeError(f"plugin {meta['name']} {hook_name} was cancelled")
        try:
            task.result()
        except Exception as exc:
            logger.error(f"Plugin {meta.get('name', '?')} {hook_name} failed: {exc}")
            if hook_name == "on_startup":
                meta["error"] = str(exc)
            return False
        return True

    async def _activate_plugin(
        self, meta: dict[str, Any], *, persist_enable: bool = False,
        publish: bool = True, seed_config: bool = True,
    ) -> bool:
        from mohobot.services.llm_tools import registry

        stage = meta.get("_llm_tool_stage")
        if stage is None:
            stage = registry.new_plugin_stage(str(meta.get("name", "")), self._state_file)
            import_tools = meta.get("_llm_tool_import_tools")
            if isinstance(import_tools, dict):
                stage.tools.update(import_tools)
            meta["_llm_tool_stage"] = stage
        cancelled = None
        try:
            schema = meta.get("config_schema")
            if schema is not None:
                effective = await self._read_effective_config(meta["name"], schema, seed=seed_config)
                config = meta.get("config")
                if config is None:
                    config = meta["config"] = effective
                else:
                    config.clear()
                    config.update(effective)
                self._inject_config(meta["instance"], config)
            self._inject_runtime(meta)
            if not await self._call_hook(meta, "on_startup", stage):
                raise RuntimeError(meta.get("error") or "on_startup failed")
            try:
                registry.commit_plugin_stage(stage)
            except Exception as exc:
                raise RuntimeError(f"LLM tool registration failed: {exc}") from exc
        except asyncio.CancelledError:
            registry.discard_plugin_stage(stage)
            meta["_llm_tool_stage"] = None
            meta["_retiring"] = True
            meta["error"] = "activation cancelled"
            raise
        except Exception as exc:
            # Keep the imported candidate retryable: startup_plugins can replay
            # on_startup with a fresh tool stage instead of losing the module.
            registry.discard_plugin_stage(stage)
            meta["_llm_tool_stage"] = None
            meta["_ready"] = False
            meta["loaded"] = False
            meta["error"] = str(exc)
            logger.error(f"Plugin {meta['name']} activation failed: {exc}")
            return False
        finally:
            if meta.get("_llm_tool_stage") is stage:
                meta["_llm_tool_stage"] = None
        if persist_enable:
            try:
                cancelled = await self._save_enabled(meta["name"], True)
            except Exception as exc:
                # The candidate was never published: retire it like #166 did.
                meta["error"] = str(exc)
                logger.error(f"Plugin {meta['name']} enable write failed: {exc}")
                await self._retire_plugin(meta)
                self._unload_plugin(meta)
                return False
            meta["enabled"] = True
        meta["loaded"] = True
        meta["error"] = None
        if publish:
            self._publish_plugin(meta)
        if cancelled is not None:
            raise cancelled
        return True

    def _publish_plugin(self, meta: dict[str, Any]) -> None:
        meta["_ready"] = True
        self._start_tick_for(meta)

    async def _start_plugin(self, meta: dict[str, Any]) -> bool:
        """Start one loaded candidate; kept as the single-plugin entry point."""
        return await self._activate_plugin(meta)

    async def startup_plugins(self) -> None:
        """Start candidates that have not yet been activated."""
        async with self._lock():
            self._require_admission()
            self._require_market_settled()
            await self._startup_plugins_unlocked()

    async def _startup_plugins_unlocked(self) -> None:
        for meta in tuple(self._plugins):
            if not meta.get("enabled") or meta.get("_retiring"):
                continue
            if meta.get("instance") is None or meta.get("_ready", True):
                continue
            # A failed startup keeps its imported instance so retries only replay
            # on_startup with a fresh registration stage.
            await self._activate_plugin(meta)

    def close_admission(self) -> None:
        """Retire this plugin system before the application drains it."""
        self._admission_closed = True

    def _require_admission(self) -> None:
        if self._admission_closed:
            raise PluginSystemRetired("plugin system is retired")

    def _require_market_settled(self, name: str | None = None) -> None:
        recovery = self._market_recovery
        if recovery is not None and (name is None or name == recovery.name):
            raise MarketRecoveryError(f"插件 {recovery.name} 市场事务待恢复：{recovery.operation.path}")

    async def shutdown_plugins(self) -> None:
        """停止 tick 并运行已加载插件的可选 on_shutdown hook。"""
        async with self._lock():
            await finish_task(self._finish_market_recovery(shutting_down=True))
            await self._shutdown_unlocked()

    # ── 周期任务 ─────────────────────────────────────────────

    def start_tick_tasks(self) -> None:
        """为声明了 interval_sec + on_tick 的插件启动后台周期任务(幂等)。

        每个插件一个循环: 每 interval_sec 秒(每次循环前读取, 配置热更新即时生效)
        调用一次插件 on_tick()。热重载前需先 stop_tick_tasks()。
        """
        for meta in tuple(self._plugins):
            self._start_tick_for(meta)

    def _start_tick_for(self, meta: dict[str, Any]) -> None:
        if not self._can_dispatch(meta):
            return
        plugin = meta.get("instance")
        if plugin is None:
            return
        ticker = getattr(plugin, "on_tick", None)
        interval = getattr(plugin, "interval_sec", None)
        if ticker is None or interval is None:
            return
        name = meta.get("name", "?")
        if name in self._tick_tasks_by_name and not self._tick_tasks_by_name[name].done():
            return

        async def _loop(inst=plugin, plugin_name=name):
            current = asyncio.current_task()
            while current not in self._stopping_tick_tasks:
                try:
                    secs = max(1, int(getattr(inst, "interval_sec", 60)))
                    await asyncio.sleep(secs)
                    if self._admission_closed or not meta.get("loaded"):
                        break
                    await self._run_plugin_handler(meta, inst.on_tick)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"插件 {plugin_name} 周期任务异常: {e}")

        task_name = f"plugin-tick:{name}"
        if self._task_supervisor is not None:
            task = self._task_supervisor.create_task(
                _loop(), name=task_name, owner=f"{self._owner_key(meta)}:tick"
            )
        else:
            task = asyncio.create_task(_loop(), name=task_name)
        self._tick_tasks.append(task)
        self._tick_tasks_by_name[name] = task

        def _forget(done: asyncio.Task) -> None:
            self._stopping_tick_tasks.discard(done)
            if self._tick_tasks_by_name.get(name) is done:
                self._tick_tasks_by_name.pop(name, None)
            if done in self._tick_tasks:
                self._tick_tasks.remove(done)

        task.add_done_callback(_forget)
        logger.info(f"插件周期任务已启动: {name}")

    def stop_tick_tasks(self) -> None:
        """同步兼容包装器: 发出取消请求；异步调用者应使用 stop_tick_tasks_async。"""
        for task in self._tick_tasks:
            self._request_tick_stop(task)

    def _request_tick_stop(self, task: asyncio.Task[Any]) -> None:
        if task.done() or task in self._stopping_tick_tasks:
            return
        # A tick may catch CancelledError; stop the loop after that callback exits.
        self._stopping_tick_tasks.add(task)
        if not task.cancelling():
            task.cancel()

    async def _stop_tick_for(self, name: str) -> None:
        task = self._tick_tasks_by_name.get(name)
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            raise TimeoutError(f"plugin {name} cannot stop its own tick task")
        self._request_tick_stop(task)
        done, _ = await asyncio.wait({task}, timeout=self._drain_timeout)
        if not done:
            raise TimeoutError(f"plugin {name} tick task is still active")

    async def stop_tick_tasks_async(self) -> None:
        """取消全部插件周期任务并等待其真正退出。"""
        tasks = {task for task in self._tick_tasks if not task.done()}
        self.stop_tick_tasks()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._drain_timeout)
            if pending:
                raise TimeoutError("plugin tick tasks are still active")

    # ── 管理员注入 ─────────────────────────────────────────────

    def set_admin_ids(self, admins: list[int] | None) -> None:
        """热同步全局管理员(web 面板保存后调用)。"""
        if admins is not None:
            self._admin_ids = {str(a) for a in admins}
        self.apply_admin_injection()

    def apply_admin_injection(self) -> None:
        """把管理员列表注入插件实例(类级 inject_admin_ids classmethod)。"""
        for meta in tuple(self._plugins):
            inst = meta.get("instance")
            if inst is None:
                continue
            injector = getattr(inst.__class__, "inject_admin_ids", None)
            if injector:
                try:
                    injector(list(self._admin_ids))
                except Exception as e:
                    logger.error(f"Admin injection failed for {meta['name']}: {e}")

    # ── 运行时注入(热重载后自动重新注入) ────────────────────

    def set_runtime_refs(self, ws_server=None, bot_manager=None, anysearch_client=None, llm_service=None, emotion_manager=None,
                         context_manager=None, persona_service=None, message_handler=None) -> None:
        """保存运行时引用, 供 load/reload 后注入插件。"""
        if ws_server is not None:
            self._ws_server = ws_server
        if bot_manager is not None:
            self._bot_manager = bot_manager
        if anysearch_client is not None:
            self._anysearch_client = anysearch_client
        if llm_service is not None:
            self._llm_service = llm_service
        if emotion_manager is not None:
            self._emotion_manager = emotion_manager
        if context_manager is not None:
            self._context_manager = context_manager
        if persona_service is not None:
            self._persona_service = persona_service
        if message_handler is not None:
            self._message_handler = message_handler

    def apply_injections(self) -> None:
        """对已加载插件实例执行注入(ws_server / bot_manager / data_dir / anysearch / admins)。

        通过实例的类注入(classmethod), 避免 re-import 产生第二份模块对象。
        """
        for meta in tuple(self._plugins):
            if self._can_dispatch(meta):
                self._inject_runtime(meta)

    def _inject_runtime(self, meta: dict[str, Any]) -> None:
        inst = meta.get("instance")
        if inst is None:
            return
        cls = type(inst)
        for key, value in (
            ("ws_server", self._ws_server),
            ("bot_manager", self._bot_manager),
            ("data_dir", str(self._data_dir)),
            ("anysearch_client", self._anysearch_client),
            ("llm_service", self._llm_service),
            ("emotion_manager", self._emotion_manager),
            ("context_manager", self._context_manager),
            ("persona_service", self._persona_service),
            ("message_handler", self._message_handler),
            ("song_matcher", self._matcher),
            ("admin_ids", list(self._admin_ids)),
        ):
            injector = getattr(cls, f"inject_{key}", None)
            if injector and value is not None:
                injector(value)
        injector = getattr(cls, "inject_plugin_data_dir", None)
        if injector:
            plugin_data_dir = self._data_dir / "plugins_data" / meta["name"]
            plugin_data_dir.mkdir(parents=True, exist_ok=True)
            injector(str(plugin_data_dir))
        injector = getattr(cls, "inject_task_supervisor", None)
        if injector and self._task_supervisor is not None:
            injector(_PluginTaskSupervisor(
                self._task_supervisor,
                self._owner_key(meta),
                lambda: not self._can_dispatch(meta),
                lambda: meta.get("_hook_task") is asyncio.current_task(),
            ))

    async def load_plugins(self) -> int:
        """扫描并加载插件; 与热重载共用同一把锁, 避免并发替换注册表。"""
        async with self._lock():
            self._require_admission()
            self._require_market_settled()
            if self._plugins:
                return await self._reload_unlocked()
            return await self._load_all_unlocked()

    # ── 插件配置 (schema 驱动) ────────────────────────────────

    @staticmethod
    def _load_config_schema(plugin_dir: Path) -> dict | None:
        """读取插件目录下的 _conf_schema.json(不存在返回 None)。"""
        schema_path = plugin_dir / "_conf_schema.json"
        if not schema_path.exists():
            return None
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to load config schema {schema_path}: {e}")
            return None

    @classmethod
    def _effective_config(cls, schema: dict, raw: Any) -> dict:
        return cls._deep_merge(
            cls._schema_defaults(schema),
            cls._coerce_config(schema, raw if isinstance(raw, dict) else {}),
        )

    async def _read_effective_config(self, name: str, schema: dict, *, seed: bool = False) -> dict:
        from mohobot.file_store import json_read

        path = self._config_dir / f"{name}.json"
        try:
            if seed:
                raw = await json_read(path, create_if_missing=self._effective_config(schema, None))
            else:
                raw = await json_read(path)
        except (ValueError, OSError) as exc:
            logger.warning(f"Failed to read plugin config {name}: {type(exc).__name__}")
            raw = None
        return self._effective_config(schema, raw)

    @classmethod
    def _schema_defaults(cls, schema: dict) -> dict:
        """从 schema 递归构造默认配置。"""
        result: dict = {}
        for key, spec in (schema or {}).items():
            if not isinstance(spec, dict):
                continue
            stype = spec.get("type", "string")
            if stype == "object":
                result[key] = cls._schema_defaults(spec.get("items") or {})
            elif "default" in spec:
                result[key] = deepcopy(spec["default"])
            elif stype == "list":
                result[key] = []
            elif stype == "bool":
                result[key] = False
            elif stype == "int":
                result[key] = 0
            elif stype == "float":
                result[key] = 0.0
            else:
                result[key] = ""
        return result

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        merged = dict(base)
        for k, v in (override or {}).items():
            if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k] = PluginSystem._deep_merge(merged[k], v)
            else:
                merged[k] = v
        return merged

    @classmethod
    def _coerce_config(
        cls, schema: dict, data: dict, *, strict: bool = False,
    ) -> dict:
        """按 schema 类型强制转换配置值(面板可能提交字符串/错误类型)。

        strict=True 用于面板提交: 无法解析的数字抛出带字段名的 ValueError,
        由面板返回 400; 读取磁盘存档时保持宽松回退默认值, 脏存档不阻塞启动。
        """
        result: dict = {}
        for key, spec in (schema or {}).items():
            if not isinstance(spec, dict) or key not in data:
                continue
            value = data[key]
            stype = spec.get("type", "string")
            if stype == "object":
                branch = cls._coerce_config(
                    spec.get("items") or {}, value if isinstance(value, dict) else {},
                    strict=strict,
                )
                if branch:
                    result[key] = branch
            elif stype == "bool":
                if isinstance(value, str):
                    result[key] = value.strip().lower() in ("true", "1", "yes", "on")
                else:
                    result[key] = bool(value)
            elif stype == "int":
                try:
                    result[key] = int(value)
                except (TypeError, ValueError):
                    if strict:
                        # 面板提交不能静默回退默认值 (#170 审查 P3)。
                        raise ValueError(f"配置字段 {key} 需要整数") from None
                    result[key] = spec.get("default", 0)
            elif stype == "float":
                try:
                    result[key] = float(value)
                except (TypeError, ValueError):
                    if strict:
                        raise ValueError(f"配置字段 {key} 需要数字") from None
                    result[key] = spec.get("default", 0.0)
            elif stype == "list":
                if isinstance(value, list):
                    result[key] = [str(i) for i in value]
                elif isinstance(value, str):
                    result[key] = [s.strip() for s in value.split(",") if s.strip()]
                else:
                    result[key] = []
            else:  # string
                result[key] = str(value) if value is not None else ""
        return result

    @staticmethod
    def _inject_config(plugin_instance, config: dict) -> None:
        """把配置注入插件实例(实例属性 plugin_config)。"""
        try:
            object.__setattr__(plugin_instance, "plugin_config", config)
        except Exception:
            try:
                plugin_instance.plugin_config = config
            except Exception:
                pass

    def get_plugin_config(self, name: str) -> dict | None:
        """Synchronous compatibility view; the panel uses the locked async read."""
        schema = self.get_config_schema(name)
        if schema is None:
            return None
        path = self._config_dir / f"{name}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = None
        except (ValueError, OSError) as exc:
            logger.warning(f"Failed to read plugin config {name}: {type(exc).__name__}")
            raw = None
        return self._effective_config(schema, raw)

    def get_config_schema(self, name: str) -> dict | None:
        for meta in tuple(self._plugins):
            if meta["name"] == name:
                return deepcopy(meta.get("config_schema"))
        return None

    async def read_plugin_config(self, name: str) -> dict | None:
        """Read one schema and its effective archive view under the lifecycle lock."""
        async with self._lock():
            self._require_market_settled(name)
            schema = self.get_config_schema(name)
            if schema is None:
                return None
            return {"schema": schema, "config": await self._read_effective_config(name, schema)}

    async def save_plugin_config(self, name: str, config: dict) -> bool:
        """Compatibility wrapper: False means no plugin config schema."""
        return await self.commit_plugin_config(name, config) is not None

    async def commit_plugin_config(self, name: str, config: dict) -> PluginConfigCommit | None:
        async with self._lock():
            self._require_admission()
            self._require_market_settled(name)
            meta = next((m for m in self._plugins if m["name"] == name), None)
            if meta is None or (schema := meta.get("config_schema")) is None:
                return None
            patch = self._coerce_config(schema, config or {}, strict=True)
            if not patch:
                return {"config": await self._read_effective_config(name, schema), "application": "unchanged"}
            commit = asyncio.create_task(self._commit_config(meta, schema, patch), name=f"plugin-config:{name}")
            cancelled = None
            while not commit.done():
                try:
                    # The lifecycle lock stays held until the write and publication finish.
                    await asyncio.wait({commit})
                except asyncio.CancelledError as exc:
                    cancelled = exc
            try:
                result = commit.result()
            except Exception as exc:
                if cancelled is not None:
                    raise cancelled from exc
                raise
            if cancelled is not None:
                raise cancelled
            return result

    async def _commit_config(self, meta: dict, schema: dict, patch: dict) -> PluginConfigCommit:
        from mohobot.file_store import json_update

        self._require_admission()
        name = meta["name"]

        def merge(raw):
            return self._deep_merge(raw if isinstance(raw, dict) else {}, patch)

        try:
            raw = await json_update(self._config_dir / f"{name}.json", merge)
        except Exception as exc:
            logger.error(f"Failed to save plugin config {name}: {type(exc).__name__}")
            raise
        # Do not yield between leaving the file lock and publishing the committed view.
        effective = self._effective_config(schema, raw)
        result: PluginConfigCommit = {"config": deepcopy(effective), "application": "not_running"}
        inst = meta.get("instance")
        if inst is None or not self._can_dispatch(meta):
            meta["config"] = effective
            return result
        active = meta.get("config")
        if active is None:
            active = meta["config"] = effective
        else:
            active.clear()
            active.update(effective)
        self._inject_config(inst, active)
        try:
            updater = getattr(inst, "on_config_update", None)
            if callable(updater):
                updater(active)
        except (Exception, asyncio.CancelledError) as exc:
            logger.error(f"Plugin {name} on_config_update failed: {type(exc).__name__}")
            result["application"] = "apply_failed"
        else:
            result["application"] = "applied"
        return result

    async def replace_market_plugin(
        self, manifest: PluginManifest, root: Path, record: dict, *,
        expect_installed: bool, recheck: Callable[[], Any],
    ) -> dict:
        """Called with the market write lock held, after archive validation."""
        async with self._lock():
            self._require_admission()
            await finish_task(self._finish_market_recovery())
            self._require_admission()
            return await finish_task(self._replace_market_plugin(manifest, root, record, expect_installed, recheck))

    async def _replace_market_plugin(self, manifest, root, record, expect_installed, recheck) -> dict:
        self._require_admission()
        await recheck()
        self._require_admission()
        name = manifest.name
        self._check_market_namespace(name)
        state = self._load_state()
        seed = None
        if state.get(name, {}).get("enabled", True):
            schema = self._load_config_schema(root)
            if schema is not None:
                seed = self._effective_config(schema, None)
        old = self._market_meta(name)
        entry = next((item for item in self._iter_entries() if item[1] == name), None)
        operation = await self._market_journal.begin_replace(
            name, root, record, expect_installed=expect_installed, config_seed=seed,
        )
        recovery = self._market_recovery = _MarketRecovery(
            name, operation, restoring_previous=entry is not None, previous=old,
        )
        try:
            await self._retire_market_meta(old)
            recovery.previous = None
            await operation.apply()
            package = self._installed_dir / name
            candidate = recovery.candidate = self._load_market_target(
                (package / manifest.entry, name, str(package), "installed"), state, record,
            )
            if candidate.get("loaded"):
                if not await self._activate_plugin(candidate, publish=False, seed_config=False):
                    # Recovery reloads the previous version under the same module
                    # name; release the failed candidate's imports first.
                    self._unload_plugin(candidate)
            error = candidate.get("error")
            if error is not None and (expect_installed or entry is not None):
                raise InstallError(f"插件 {name} 激活失败：{error}")
            await operation.commit()
        except Exception as exc:
            await self._abort_market_change(recovery, exc)
            raise

        recovery.candidate = None
        recovery.restoring_previous = False
        self._set_market_meta(name, candidate)
        if candidate.get("loaded"):
            self._publish_plugin(candidate)
        warning = await self._clean_market_commit(recovery)
        if error is not None:
            detail = f"插件 {name} 文件已安装，但激活失败：{error}"
            raise InstallError(f"{detail}；{warning}" if warning else detail)
        return self._market_result(candidate, warning)

    async def uninstall_market_plugin(self, name: str, *, delete_config: bool, delete_data: bool) -> tuple[Path, dict]:
        """Use this path whether or not a market index is configured."""
        async with self._lock():
            self._require_admission()
            await finish_task(self._finish_market_recovery())
            self._require_admission()
            return await finish_task(self._uninstall_market_plugin(name, delete_config, delete_data))

    async def _uninstall_market_plugin(self, name, delete_config, delete_data) -> tuple[Path, dict]:
        self._require_admission()
        installed = self._installed_dir / name
        if not installed.exists() and not installed.is_symlink():
            raise PluginNotFound(f"插件未安装: {name}")
        self._check_market_namespace(name)
        operation = await self._market_journal.begin_uninstall(name, delete_config=delete_config, delete_data=delete_data)
        recovery = self._market_recovery = _MarketRecovery(
            name, operation, restoring_previous=True, previous=self._market_meta(name),
        )
        try:
            await self._retire_market_meta(recovery.previous)
            recovery.previous = None
            await operation.apply()
            await operation.commit()
        except Exception as exc:
            await self._abort_market_change(recovery, exc)
            raise

        recovery.restoring_previous = False
        self._set_market_meta(name, None)
        try:
            fallback = await self._restore_market_target(recovery)
        except Exception as exc:
            # The external plugin is already gone. Retain any unfinished builtin
            # owner without conflating activation failure with uninstall failure.
            fallback = recovery.candidate
            if fallback is not None:
                fallback["error"] = f"内置插件激活失败，待收尾：{exc}"
                self._set_market_meta(name, fallback)
            logger.error(f"Plugin {name} builtin activation could not settle: {exc}")
        warning = await self._clean_market_commit(recovery)
        result = self._market_result(fallback, warning)
        if fallback is not None and fallback.get("error") is not None:
            result["activation"] = "failed"
        return operation.recycle_path, result

    def _market_meta(self, name: str) -> dict | None:
        return next((m for m in self._plugins if m["name"] == name), None)

    def _set_market_meta(self, name: str, meta: dict | None) -> None:
        index = next((i for i, m in enumerate(self._plugins) if m["name"] == name), len(self._plugins))
        self._plugins[index:index + 1] = [meta] if meta is not None else []

    def _check_market_namespace(self, name: str) -> None:
        module = self._module_name(name)
        other = self._module_name_conflict(module, exclude_name=name)
        if other is None:
            other = next((n for _, n, _, _ in self._iter_entries() if n != name and self._module_name(n) == module), None)
        if other is not None:
            raise PluginNamespaceConflict(f"插件模块名冲突: {name} 与 {other}")

    async def _retire_market_meta(self, meta: dict | None) -> None:
        if meta is not None and meta.get("loaded"):
            await self._pause_dispatches((meta,))
            await self._retire_plugin(meta)
            self._unload_plugin(meta)

    def _load_market_target(self, entry, state, record) -> dict:
        path, name, directory, source = entry
        try:
            meta = self._load_one(path, name, directory, state, source)
        except Exception as exc:
            meta = self._error_meta(path, name, directory, source, exc)
            meta["enabled"] = state.get(name, {}).get("enabled", True)
        if record is not None and source == "installed":
            meta["source_record"] = record
        return meta

    async def _restore_market_target(self, recovery: _MarketRecovery) -> dict | None:
        name = recovery.name
        entry = next((item for item in self._iter_entries() if item[1] == name), None)
        if entry is None:
            self._set_market_meta(name, None)
            return None
        meta = recovery.candidate = self._load_market_target(entry, self._load_state(), recovery.operation.intent.old)
        self._set_market_meta(name, meta)
        if meta.get("loaded"):
            activated = await self._activate_plugin(meta)
            if not activated:
                self._unload_plugin(meta)
                raise MarketRecoveryError(
                    f"插件 {name} 原版本激活失败，保留事务恢复状态：{meta.get('error')}"
                )
        elif meta.get("error") is not None:
            raise MarketRecoveryError(
                f"插件 {name} 原版本加载失败，保留事务恢复状态：{meta.get('error')}"
            )
        recovery.candidate = None
        return meta

    async def _abort_market_change(self, recovery: _MarketRecovery, error: Exception) -> None:
        # A failed startup may still own tasks. Never rename files until that
        # exact candidate has drained; a later retry will resume here.
        candidate = recovery.candidate
        if candidate is not None and candidate.get("loaded") and candidate.get("_retiring"):
            candidate["error"] = f"插件市场事务待恢复 ({recovery.operation.path.name})：{error}"
            self._set_market_meta(recovery.name, candidate)
            raise MarketRecoveryError(candidate["error"]) from error
        try:
            await self._finish_market_recovery()
        except Exception as exc:
            raise MarketRecoveryError(f"插件 {recovery.name} 事务待恢复：{error}；{exc}") from exc
        if isinstance(error, InstallError) and recovery.operation.intent.decision == "rolled_back":
            fallback = self._market_meta(recovery.name)
            status = "已回退"
            if fallback is None:
                status += "，未找到原插件"
            elif fallback.get("error") is not None:
                status += f"，原插件未激活：{fallback['error']}"
            raise InstallError(f"{error}；{status}") from error

    async def _finish_market_recovery(self, *, shutting_down: bool = False) -> None:
        recovery = self._market_recovery
        if recovery is None:
            return
        operation = recovery.operation
        # Keep the old owner reachable until its hook and supervised tasks have
        # actually exited. Rollback must not load over a half-retired instance.
        await self._retire_market_meta(recovery.previous)
        recovery.previous = None
        await self._retire_market_meta(recovery.candidate)
        recovery.candidate = None
        if operation.intent.decision == "pending":
            await operation.rollback()
        if recovery.restoring_previous and not shutting_down:
            await self._restore_market_target(recovery)
            recovery.restoring_previous = False
        try:
            await operation.cleanup()
        except MarketRecoveryError as exc:
            if not shutting_down:
                raise
            # A terminal journal cleanup can be retried on next startup. It no
            # longer affects the selected plugin owner, so do not skip teardown.
            logger.error(f"Plugin {recovery.name} market journal cleanup deferred during shutdown: {exc}")
            return
        self._market_recovery = None

    async def _clean_market_commit(self, recovery: _MarketRecovery) -> str | None:
        if recovery.candidate is not None:
            return "内置插件仍在收尾，暂未清理事务"
        try:
            await recovery.operation.cleanup()
        except MarketRecoveryError as exc:
            logger.error(str(exc))
            return f"已提交，待清理：{exc}"
        self._market_recovery = None
        return None

    def _market_result(self, meta: dict | None, warning: str | None) -> dict:
        target = None
        activation = None
        if meta is not None:
            target = {key: meta.get(key) for key in ("name", "source", "version", "enabled", "error", "source_record")}
            target["loaded"] = self._can_dispatch(meta)
            activation = "active" if target["loaded"] else "disabled" if not target["enabled"] else "failed"
        return {"activation": activation, "target": target, "warning": warning,
                "count": sum(self._can_dispatch(m) for m in self._plugins)}

    async def reload_plugins(self) -> int:
        """热重载: 重新扫描 plugins/ 目录并加载全部插件。"""
        logger.info("Hot-reloading plugins...")
        async with self._lock():
            self._require_admission()
            self._require_market_settled()
            count = await self._reload_unlocked()
        logger.info(f"Plugin hot-reload complete: {count} plugin(s) active")
        return count

    async def unload_plugin(self, name: str) -> bool:
        """卸载单个已加载插件; 文件移除前先停任务并执行 on_shutdown。"""
        async with self._lock():
            self._require_admission()
            self._require_market_settled(name)
            meta = next((m for m in self._plugins if m["name"] == name), None)
            if meta is None:
                return False
            from mohobot.services.llm_tools import registry

            owner_states = None
            try:
                was_inactive = await registry.deactivate_owner(name)
                owner_states = {name: was_inactive}
                await self._pause_dispatches((meta,))
                await self._retire_plugin(meta)
            except asyncio.TimeoutError:
                if owner_states is not None:
                    registry.restore_owner_activity(owner_states)
                logger.warning(f"Plugin {name} unload timed out before teardown")
                return False
            except BaseException:
                if owner_states is not None:
                    registry.restore_owner_activity(owner_states)
                raise
            self._unload_plugin(meta)
            self._plugins.remove(meta)
            return True

    async def reload_plugin(self, name: str) -> bool:
        """只重载一个插件, 其余插件实例与周期任务保持不变。"""
        async with self._lock():
            self._require_admission()
            recovery = self._market_recovery
            if recovery is not None and recovery.name == name:
                await finish_task(self._finish_market_recovery())
                return self._market_meta(name) is not None
            meta = self._market_meta(name)
            if not (meta is not None and "_rollback" in meta) and not any(
                item[1] == name for item in self._iter_entries()
            ):
                raise PluginNotFound(f"未找到插件: {name}")
            return await self._reload_one_unlocked(name)

    async def _reload_one_unlocked(self, name: str, *, enable: bool = False) -> bool:
        from mohobot.plugins.sources import load_sources

        from mohobot.services.llm_tools import registry

        meta = next((m for m in self._plugins if m["name"] == name), None)
        if meta is not None and "_rollback" in meta:
            # Finish the interrupted replacement before reconsidering files on disk.
            await self._rollback_reload(meta, self._plugins.index(meta))
            return False
        entry = next((item for item in self._iter_entries() if item[1] == name), None)
        if entry is None:
            return False
        module_name = self._module_name(name)
        previous = self._module_name_conflict(module_name, exclude_name=name)
        if previous is not None:
            logger.error(f"插件模块名冲突: {name} 与 {previous}")
            return False
        sources = await load_sources(self._data_dir)
        state = self._load_state()
        was_enabled = state.get(name, {}).get("enabled", True)
        if enable:
            state[name] = {"enabled": True}

        index = self._plugins.index(meta) if meta is not None else len(self._plugins)
        old_instance = None
        old_imports = None
        old_tools: dict[str, Any] = {}
        # on_startup 失败的候选 loaded=False 但仍持有 "_imports" 以便
        # startup_plugins() 重试。重新加载前必须先释放它占用的命名空间,
        # 否则 _load_one 会把它自己的旧模块判为冲突, 且旧模块残留在
        # sys.modules, 让之后的 reload/enable 永久报模块名冲突。
        if meta is not None and (meta.get("loaded") or meta.get("_imports") is not None):
            if meta.get("loaded"):
                can_restore = self._can_dispatch(meta)
                old_tools = registry.snapshot_owner_tools(name)
                await self._pause_dispatches((meta,))
                await self._retire_plugin(meta)
                if can_restore:
                    old_instance = meta.get("instance")
                    old_imports = meta.get("_imports")
            self._unload_plugin(meta)
        entry_path, _, entry_dir, source = entry
        try:
            candidate = self._load_one(entry_path, name, entry_dir, state, source)
        except Exception as exc:
            candidate = self._error_meta(entry_path, name, entry_dir, source, exc)
        candidate["source_record"] = sources.get(name)
        if enable:
            # Expose the saved setting until startup and the enable write succeed.
            candidate["enabled"] = was_enabled
        # Keep the snapshot reachable while the candidate still owns resources.
        if old_instance is not None:
            candidate["_rollback"] = (meta, old_instance, old_imports, old_tools)
        self._plugins[index:index + 1] = [candidate]
        try:
            if candidate.get("loaded") and await self._activate_plugin(candidate, persist_enable=enable):
                candidate.pop("_rollback", None)
                return True
        except asyncio.CancelledError:
            if candidate.get("_ready"):
                # An enable write committed despite cancellation of its waiter.
                candidate.pop("_rollback", None)
            elif "_rollback" in candidate:
                try:
                    await self._rollback_reload(candidate, index)
                except Exception as exc:
                    logger.error(f"Plugin {name} rollback interrupted during cleanup: {exc}")
            raise
        if "_rollback" in candidate and candidate.get("error"):
            await self._rollback_reload(candidate, index)
        else:
            candidate.pop("_rollback", None)
        return False

    async def _rollback_reload(self, candidate: dict[str, Any], index: int) -> None:
        from mohobot.services.llm_tools import registry

        if candidate.get("_retiring") or candidate.get("loaded"):
            # Cancellation/failed startup may still own the startup hook or tasks.
            await self._retire_plugin(candidate)
        # A failed candidate may still own imported modules even without _ready.
        self._unload_plugin(candidate)

        meta, instance, imports, old_tools = candidate.pop("_rollback")
        name = meta["name"]
        if not imports.restore():
            candidate["error"] = f"{candidate['error']}; previous plugin modules are occupied"
            logger.error(f"Plugin {name} rollback failed: {candidate['error']}")
            return
        meta.update(
            instance=instance, loaded=True, _imports=imports, _ready=False,
            _retiring=False, _shutdown_done=False,
        )
        self._plugins[index] = meta
        if await self._activate_plugin(meta):
            try:
                registry.restore_owner_tools(name, old_tools)
            except Exception as exc:
                logger.error(f"Plugin {name} tool rollback failed: {exc}")
            logger.error(f"Plugin {name} reload failed, restored previous instance: {candidate['error']}")

    # ── Plugin State (enable/disable) ─────────────────────────

    def _load_state(self) -> dict[str, Any]:
        """Load plugin enable/disable state from data/plugins_state.json."""
        try:
            if self._state_file.exists():
                return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to load plugin state: {e}")
        return {}

    async def _save_enabled(self, name: str, enabled: bool) -> asyncio.CancelledError | None:
        """Finish the write with the lifecycle lock held, even if the waiter cancels."""
        from mohobot.file_store import json_update

        def merge(current):
            state = dict(current) if isinstance(current, dict) else {}
            entry = state.get(name)
            state[name] = {**(entry if isinstance(entry, dict) else {}), "enabled": enabled}
            return state

        write = asyncio.create_task(json_update(self._state_file, merge), name=f"plugin-state:{name}")
        cancelled = None
        while not write.done():
            try:
                # wait() doesn't forward cancellation to the JSON writer.
                await asyncio.wait({write})
            except asyncio.CancelledError as exc:
                cancelled = exc
        write.result()
        # The caller publishes the committed state before re-raising cancellation.
        return cancelled

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/禁用插件, 立即生效(无需重启)。"""
        async with self._lock():
            self._require_admission()
            self._require_market_settled(name)
            meta = next((m for m in self._plugins if m["name"] == name), None)
            if meta is None:
                return False
            if enabled and self._can_dispatch(meta):
                return True
            if enabled:
                state = self._load_state()
                state[name] = {"enabled": True}
                self._check_reload_namespaces(state)
                return await self._reload_one_unlocked(name, enable=True)
            if meta.get("loaded"):
                await self._pause_dispatches((meta,))
                await self._retire_plugin(meta)
                self._unload_plugin(meta)
            meta.pop("_rollback", None)
            try:
                cancelled = await self._save_enabled(name, False)
            except Exception as exc:
                meta["error"] = str(exc)
                raise
            meta["enabled"] = False
            meta["error"] = None
            if cancelled is not None:
                raise cancelled
            return True

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return plugin metadata (without instances) for the web panel."""
        result = []
        for meta in tuple(self._plugins):
            item = {k: v for k, v in meta.items() if k != "instance" and not k.startswith("_")}
            item["config"] = deepcopy(item.get("config"))
            item["config_schema"] = deepcopy(item.get("config_schema"))
            item["loaded"] = self._can_dispatch(meta)
            result.append(item)
        return result

    # ── Event Dispatch ────────────────────────────────────────

    @staticmethod
    def _bot_allowed(meta: dict, bot_id: str) -> bool:
        """per-bot 插件绑定: 插件类声明 bind_bots=["bot_001"] 时仅对这些 bot 生效。

        空/未声明 = 对所有 bot 生效。观察钩子/拦截/通知/请求分发统一走此过滤。
        """
        inst = meta.get("instance")
        if inst is None:
            return True
        bind = getattr(inst.__class__, "bind_bots", None)
        if not bind:
            return True
        return bot_id in {str(b) for b in bind}

    async def dispatch_notice(self, bot_id: str, event: NoticeEvent, raw: dict) -> None:
        """Dispatch a notice event to all enabled plugins."""
        for meta in tuple(self._plugins):
            if not self._can_dispatch(meta):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_notice", None)
                if handler:
                    await self._run_plugin_handler(meta, handler, bot_id, event, raw)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} notice handler error: {e}")

    async def dispatch_meta(self, bot_id: str, event: MetaEvent, raw: dict) -> None:
        """Dispatch a meta event to all enabled plugins."""
        for meta in tuple(self._plugins):
            if not self._can_dispatch(meta):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_meta", None)
                if handler:
                    await self._run_plugin_handler(meta, handler, bot_id, event, raw)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} meta handler error: {e}")

    async def dispatch_request(
        self, bot_id: str, event, raw: dict,
    ) -> bool:
        """Dispatch a request event (好友申请/群邀请) to plugins.

        Returns True if a plugin handled the request (框架不再自动同意)。
        """
        for meta in tuple(self._plugins):
            if not self._can_dispatch(meta):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_request", None)
                if handler:
                    handled = await self._run_plugin_handler(meta, handler, bot_id, event, raw)
                    if handled:
                        return True
            except Exception as e:
                logger.error(f"Plugin {meta['name']} request handler error: {e}")
        return False

    async def dispatch_observed(
        self,
        bot_id: str,
        event,
        raw: dict,
    ) -> tuple[bool, str | list[dict[str, Any]] | None]:
        """分发"消息观察"钩子: 所有消息(含群内未 @bot 的)在 gate 前先过一遍插件。

        用于插件捕获普通消息(活跃记录、求婚"同意/拒绝"回复、无前缀关键词触发)。
        返回 (是否消费, 回复); 不消费时消息继续走正常流程(gate → 拦截链 → LLM)。
        """
        for meta in tuple(self._plugins):
            if not self._can_dispatch(meta):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_message_observed", None)
                if handler:
                    handled, response = await self._run_plugin_handler(
                        meta, handler, bot_id, event, raw, default=(False, None),
                    )
                    if handled:
                        return (True, response)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} observe handler error: {e}")
            if not self._can_dispatch(meta):
                continue
            # 无前缀命令触发: 插件声明 no_prefix_triggers 后, 消息文本精确匹配
            # 集合中的词(群聊无需 @, 私聊直接) → 调用插件 on_message 处理
            try:
                text = _extract_plain_text(getattr(event, "message", None))
                if text:
                    npt = getattr(plugin.__class__, "no_prefix_triggers", None)
                    if npt and text in {str(t) for t in npt}:
                        # 群内去重: 该词同时是插件的全局指令(global_triggers)时,
                        # 群聊只由随机选中的一个 bot 处理(与 / 前缀命令去重一致)
                        if not self._is_chosen_bot_for_global(bot_id, event, text, plugin):
                            continue
                        msg_handler = getattr(plugin, "on_message", None)
                        if msg_handler:
                            handled, response = await self._run_plugin_handler(
                                meta, msg_handler, bot_id, event, raw, default=(False, None),
                            )
                            if handled:
                                return (True, response)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} no-prefix trigger error: {e}")
        return (False, None)

    def _is_chosen_bot_for_global(self, bot_id: str, event, text: str, plugin) -> bool:
        """无前缀触发词同时是全局指令时, 群聊只由随机选中的一个 bot 处理。"""
        gt = getattr(plugin.__class__, "global_triggers", None)
        if not (gt and text in {str(t) for t in gt}):
            return True  # 非全局指令词 → 不去重
        from mohobot.models.onebot import GroupMessageEvent
        if not isinstance(event, GroupMessageEvent):
            return True  # 私聊不去重
        bm = self._bot_manager
        if bm is None:
            return True
        # @ 定向优先: 被 @ 的 bot 都应执行, 不再抽签
        if self._mentioned_bot_ids(event, bm):
            return True
        chosen = bm.pick_bot_for_group(
            str(event.group_id), str(getattr(event, "message_id", "") or ""),
        )
        if chosen is None or chosen == bot_id:
            return True
        logger.debug(f"无前缀全局指令 {text!r} 由 {chosen} 回复, {bot_id} 跳过")
        return False

    @staticmethod
    def _mentioned_bot_ids(event, bot_manager) -> set[str]:
        """消息里被 @ 的、已绑定且在线的 bot_id 集合(无 @ 返回空集)。"""
        from mohobot.utils.cq_code import extract_at_targets

        targets = extract_at_targets(getattr(event, "message", None))
        if not targets:
            return set()
        try:
            return bot_manager.bot_ids_for_qqs(targets)
        except Exception:  # noqa: BLE001 - 判定失败按"未 @ 任何 bot"处理
            return set()

    async def collect_perception(self, bot_id: str, event, raw: dict) -> str:
        """收集"环境感知"文本: 遍历插件调用 on_perception(bot_id, event, raw)。

        感知插件返回当前环境信息(时间/节假日/农历/节气/群聊环境等),
        由 message_handler 附加到 LLM 请求(仅回复生成, 不写入 context)。
        """
        parts: list[str] = []
        for meta in tuple(self._plugins):
            if not self._can_dispatch(meta):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_perception", None)
                if not handler:
                    continue
                text = await self._run_plugin_handler(meta, handler, bot_id, event, raw)
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            except Exception as e:
                logger.error(f"Plugin {meta['name']} perception handler error: {e}")
        return "\n".join(parts)

    async def intercept(
        self,
        bot_id: str,
        event: MessageEvent,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | list[dict[str, Any]] | None]:
        """Give plugins a chance to intercept messages."""
        for meta in tuple(self._plugins):
            if not self._can_dispatch(meta):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_message", None)
                if handler:
                    handled, response = await self._run_plugin_handler(
                        meta, handler, bot_id, event, raw_event, default=(False, None),
                    )
                    if handled:
                        return (True, response)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} message handler error: {e}")
        return (False, None)
