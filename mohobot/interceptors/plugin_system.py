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
import shutil
import json
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from mohobot.interceptors.base import Interceptor
from mohobot.models.onebot import (
    MessageEvent,
    MetaEvent,
    NoticeEvent,
)
from mohobot import __version__ as MOHOBOT_VERSION
from mohobot.plugins.imports import PluginImports, namespace_conflict
from mohobot.plugins.installer import STAGING_MAX_AGE_SECONDS, STAGING_PREFIXES
from mohobot.plugins.manifest import (
    ManifestError,
    PluginManifest,
    check_compatibility,
    find_manifest,
    parse_manifest,
)
from mohobot.utils.cq_code import extract_plain_text as _extract_plain_text

from mohobot.api import api_version as PLUGIN_API_VERSION

if TYPE_CHECKING:
    from mohobot.services.llm_tools import PluginToolStage


class PluginNamespaceConflict(ManifestError):
    """A rescan cannot proceed without displacing an existing module."""


class _PluginTaskSupervisor:
    """把插件显式使用的 owner="plugins" 映射为按插件隔离的 owner。"""

    def __init__(self, supervisor: Any, plugin_name: str) -> None:
        self._supervisor = supervisor
        self._plugin_name = plugin_name

    @property
    def stopping(self) -> bool:
        return bool(getattr(self._supervisor, "stopping", False))

    def _owner(self, owner: str) -> str:
        prefix = f"plugin:{self._plugin_name}"
        if not owner or owner == "plugins":
            return prefix
        if owner.startswith("plugin:"):
            return f"{prefix}:{owner[7:]}"
        return f"{prefix}:{owner}"

    def create_task(self, coro: Any, *, name: str, owner: str = "application") -> Any:
        return self._supervisor.create_task(coro, name=name, owner=self._owner(owner))

    async def cancel_owner(self, owner: str, timeout: float = 5.0) -> None:
        await self._supervisor.cancel_owner(self._owner(owner), timeout)

    async def cancel_owner_prefix(self, prefix: str, timeout: float = 5.0) -> None:
        await self._supervisor.cancel_owner_prefix(self._owner(prefix), timeout)
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
        self._task_supervisor = None
        self._matcher = None
        self._reload_lock: asyncio.Lock | None = None
        self._plugin_api_version = PLUGIN_API_VERSION
        self._active_dispatches = 0
        self._dispatch_drained = asyncio.Event()
        self._dispatch_drained.set()
        self._dispatch_tasks: dict[asyncio.Task[Any], int] = {}
        self._dispatch_drain_timeout = 5.0
        self._hook_timeout = 30.0

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
        if imports is None:
            return
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

    def _unload_all_plugins(self) -> None:
        for meta in tuple(self._plugins):
            self._unload_plugin(meta)

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
        manifest: PluginManifest | None = None
        manifest_path = find_manifest(entry_path.parent) if entry_dir else None
        if manifest_path is not None:
            manifest = parse_manifest(manifest_path)
            plugin_name = manifest.name
            if manifest.entry != entry_path.name:
                raise ManifestError(
                    f"metadata entry={manifest.entry} 与入口 {entry_path.name} 不一致"
                )
            check_compatibility(
                manifest,
                api_version=self._plugin_api_version,
                mohobot_version=MOHOBOT_VERSION,
            )
        elif source == "installed":
            raise ManifestError("已安装插件缺少 metadata.yaml")
        if source == "installed" and manifest.name != entry_path.parent.name:
            raise ManifestError(
                f"metadata name={manifest.name} 与安装目录 {entry_path.parent.name} 不一致"
            )

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
        if not meta["enabled"]:
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

                schema = self._load_config_schema(entry_path.parent)
                if schema:
                    meta["config_schema"] = schema
                    meta["config"] = self._load_plugin_config(plugin_name, schema)
                    self._inject_config(plugin_instance, meta["config"])
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
        self._unload_all_plugins()
        self._plugins = []
        for directory in (self._plugins_dir, self._installed_dir):
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                logger.info(f"Created plugins directory: {directory}")
        self._cleanup_stale_staging()

        state = self._load_state()
        from mohobot.plugins.sources import load_sources

        sources = await load_sources(self._data_dir)
        count = 0
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
            previous = None
            if state.get(plugin_name, {}).get("enabled", True):
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
            if meta.get("loaded"):
                count += 1

        logger.info(f"Plugin system loaded {count} plugin(s)")
        try:
            self.apply_injections()
        except Exception as e:
            logger.error(f"Plugin injection failed: {e}")
        await self.startup_plugins()
        self.start_tick_tasks()
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

        owners = list(dict.fromkeys(str(meta.get("name", "")) for meta in self._plugins))
        owner_states = await registry.deactivate_owners(
            owners, cancel_on_timeout=True
        )
        try:
            await self._wait_for_dispatches(cancel_on_timeout=True)
        except BaseException:
            registry.restore_owner_activity(owner_states)
            raise
        await self.stop_tick_tasks_async()
        if self._task_supervisor is not None:
            await self._task_supervisor.cancel_owner("plugins")
            await self._task_supervisor.cancel_owner_prefix("plugin:")
        for meta in reversed(tuple(self._plugins)):
            if meta.get("loaded"):
                await self._call_hook(meta, "on_shutdown")
        if self._task_supervisor is not None:
            await self._task_supervisor.cancel_owner_prefix("plugin:")
        for owner in owners:
            registry.unregister_owner(owner)
        self._unload_all_plugins()

    async def _reload_unlocked(self, *, enabled_update: dict[str, Any] | None = None) -> int:
        state = self._load_state()
        if enabled_update is not None:
            state.update(enabled_update)
        self._check_reload_namespaces(state)
        if enabled_update is not None:
            await self._save_state(enabled_update)
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

    async def _run_plugin_handler(
        self, handler: Callable[..., Any], *args: Any
    ) -> Any:
        task = asyncio.current_task()
        self._active_dispatches += 1
        self._dispatch_drained.clear()
        if task is not None:
            self._dispatch_tasks[task] = self._dispatch_tasks.get(task, 0) + 1
        try:
            return await handler(*args)
        finally:
            self._active_dispatches -= 1
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

    async def _call_hook(
        self,
        meta: dict[str, Any],
        hook_name: str,
        registration_stage: PluginToolStage | None = None,
    ) -> bool:
        """调用可选生命周期 hook, 同步/异步实现均可且互不影响。"""
        plugin = meta.get("instance")
        hook = getattr(plugin, hook_name, None) if plugin is not None else None
        if not callable(hook):
            return True
        try:
            if registration_stage is None:
                result = hook()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=self._hook_timeout)
            else:
                from mohobot.services.llm_tools import registry

                with registry.registration_scope(registration_stage):
                    result = hook()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=self._hook_timeout)
            return True
        except Exception as e:
            logger.error(f"Plugin {meta.get('name', '?')} {hook_name} failed: {e}")
            return False

    async def _start_plugin(self, meta: dict[str, Any]) -> bool:
        from mohobot.services.llm_tools import registry

        stage = meta.get("_llm_tool_stage")
        if stage is None:
            stage = registry.new_plugin_stage(str(meta.get("name", "")), self._state_file)
            meta["_llm_tool_stage"] = stage
        success = await self._call_hook(meta, "on_startup", stage)
        try:
            if success:
                registry.commit_plugin_stage(stage)
            else:
                registry.discard_plugin_stage(stage)
        except Exception as e:
            registry.discard_plugin_stage(stage)
            logger.error(f"Plugin {meta.get('name', '?')} LLM tool registration failed: {e}")
            success = False
        meta["_llm_tool_stage"] = None
        return success

    async def startup_plugins(self) -> None:
        """运行已加载插件的可选 on_startup hook。"""
        for meta in list(self._plugins):
            if meta.get("enabled") and meta.get("loaded"):
                await self._start_plugin(meta)

    async def shutdown_plugins(self) -> None:
        """停止 tick 并运行已加载插件的可选 on_shutdown hook。"""
        async with self._lock():
            await self._shutdown_unlocked()

    # ── 周期任务 ─────────────────────────────────────────────

    def start_tick_tasks(self) -> None:
        """为声明了 interval_sec + on_tick 的插件启动后台周期任务(幂等)。

        每个插件一个循环: 每 interval_sec 秒(每次循环前读取, 配置热更新即时生效)
        调用一次插件 on_tick()。热重载前需先 stop_tick_tasks()。
        """
        self.stop_tick_tasks()

        for meta in tuple(self._plugins):
            self._start_tick_for(meta)

    def _start_tick_for(self, meta: dict[str, Any]) -> None:
        if not meta.get("enabled") or not meta.get("loaded"):
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
            while True:
                try:
                    secs = max(1, int(getattr(inst, "interval_sec", 60)))
                    await asyncio.sleep(secs)
                    await self._run_plugin_handler(inst.on_tick)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"插件 {plugin_name} 周期任务异常: {e}")

        task_name = f"plugin-tick:{name}"
        if self._task_supervisor is not None:
            task = self._task_supervisor.create_task(
                _loop(), name=task_name, owner="plugins"
            )
        else:
            task = asyncio.create_task(_loop(), name=task_name)
        self._tick_tasks.append(task)
        self._tick_tasks_by_name[name] = task

        def _forget(done: asyncio.Task) -> None:
            if self._tick_tasks_by_name.get(name) is done:
                self._tick_tasks_by_name.pop(name, None)

        task.add_done_callback(_forget)
        logger.info(f"插件周期任务已启动: {name}")

    def stop_tick_tasks(self) -> None:
        """同步兼容包装器: 发出取消请求；异步调用者应使用 stop_tick_tasks_async。"""
        for task in self._tick_tasks:
            if not task.done():
                task.cancel()
        self._tick_tasks_by_name.clear()

    async def _stop_tick_for(self, name: str) -> None:
        task = self._tick_tasks_by_name.pop(name, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if task in self._tick_tasks:
            self._tick_tasks.remove(task)

    async def stop_tick_tasks_async(self) -> None:
        """取消全部插件周期任务并等待其真正退出。"""
        tasks = list(self._tick_tasks)
        self.stop_tick_tasks()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tick_tasks.clear()
        self._tick_tasks_by_name.clear()

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
            inst = meta.get("instance")
            if inst is None:
                continue
            if self._ws_server is not None:
                injector = getattr(inst.__class__, "inject_ws_server", None)
                if injector:
                    injector(self._ws_server)
            if self._bot_manager is not None:
                injector = getattr(inst.__class__, "inject_bot_manager", None)
                if injector:
                    injector(self._bot_manager)
            injector = getattr(inst.__class__, "inject_data_dir", None)
            if injector:
                injector(str(self._data_dir))
            plugin_data_dir = self._data_dir / "plugins_data" / str(meta.get("name", ""))
            injector = getattr(inst.__class__, "inject_plugin_data_dir", None)
            if injector:
                plugin_data_dir.mkdir(parents=True, exist_ok=True)
                injector(str(plugin_data_dir))
            if self._anysearch_client is not None:
                injector = getattr(inst.__class__, "inject_anysearch_client", None)
                if injector:
                    injector(self._anysearch_client)
            if self._llm_service is not None:
                injector = getattr(inst.__class__, "inject_llm_service", None)
                if injector:
                    injector(self._llm_service)
            if self._emotion_manager is not None:
                injector = getattr(inst.__class__, "inject_emotion_manager", None)
                if injector:
                    injector(self._emotion_manager)
            if self._context_manager is not None:
                injector = getattr(inst.__class__, "inject_context_manager", None)
                if injector:
                    injector(self._context_manager)
            if self._persona_service is not None:
                injector = getattr(inst.__class__, "inject_persona_service", None)
                if injector:
                    injector(self._persona_service)
            if self._message_handler is not None:
                injector = getattr(inst.__class__, "inject_message_handler", None)
                if injector:
                    injector(self._message_handler)
            if self._task_supervisor is not None:
                injector = getattr(inst.__class__, "inject_task_supervisor", None)
                if injector:
                    injector(_PluginTaskSupervisor(self._task_supervisor, meta["name"]))
            if self._matcher is not None:
                injector = getattr(inst.__class__, "inject_song_matcher", None)
                if injector:
                    injector(self._matcher)
        # 管理员注入(类级)
        self.apply_admin_injection()

    async def load_plugins(self) -> int:
        """扫描并加载插件; 与热重载共用同一把锁, 避免并发替换注册表。"""
        async with self._lock():
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

    def _load_plugin_config(self, plugin_name: str, schema: dict) -> dict:
        """合并 schema 默认值 + 已存配置(存于 data/plugins_config/{name}.json)。

        存档按 schema 强转类型, 防止手工编辑/旧版本写入脏值导致插件崩溃。
        """
        defaults = self._schema_defaults(schema)
        saved: dict = {}
        config_file = self._config_dir / f"{plugin_name}.json"
        if config_file.exists():
            try:
                saved = json.loads(config_file.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    saved = {}
            except Exception as e:
                logger.error(f"Failed to load plugin config {config_file}: {e}")
        coerced = self._coerce_config(schema, saved)
        merged = self._deep_merge(defaults, coerced)
        # 首次无存档时写默认值, 便于面板编辑
        if not saved:
            try:
                config_file.parent.mkdir(parents=True, exist_ok=True)
                config_file.write_text(
                    json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as e:
                logger.error(f"Failed to save default plugin config: {e}")
        return merged

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
                result[key] = spec["default"]
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
    def _coerce_config(cls, schema: dict, data: dict) -> dict:
        """按 schema 类型强制转换配置值(面板可能提交字符串/错误类型)。"""
        result: dict = {}
        for key, spec in (schema or {}).items():
            if not isinstance(spec, dict) or key not in data:
                continue
            value = data[key]
            stype = spec.get("type", "string")
            if stype == "object":
                result[key] = cls._coerce_config(
                    spec.get("items") or {}, value if isinstance(value, dict) else {}
                )
            elif stype == "bool":
                if isinstance(value, str):
                    result[key] = value.strip().lower() in ("true", "1", "yes", "on")
                else:
                    result[key] = bool(value)
            elif stype == "int":
                try:
                    result[key] = int(value)
                except (TypeError, ValueError):
                    result[key] = spec.get("default", 0)
            elif stype == "float":
                try:
                    result[key] = float(value)
                except (TypeError, ValueError):
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
        """Web 面板读取插件配置(优先读存档, 插件运行时 _persist 的修改立即可见)。"""
        meta = next((m for m in self._plugins if m["name"] == name), None)
        if meta is None:
            return None
        # 插件运行时可写回配置文件(如关系插件黑名单); 面板应显示最新存档
        config_file = self._config_dir / f"{name}.json"
        if config_file.exists():
            try:
                saved = json.loads(config_file.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    return saved
            except Exception as e:
                logger.error(f"Failed to read plugin config {config_file}: {e}")
        return meta.get("config")

    def get_config_schema(self, name: str) -> dict | None:
        for meta in tuple(self._plugins):
            if meta["name"] == name:
                return meta.get("config_schema")
        return None

    async def save_plugin_config(self, name: str, config: dict) -> bool:
        """保存插件配置并热同步到插件实例(立即生效, 无需重启)。

        用 json_update 原子读改写: 以磁盘当前存档为基础合并面板提交值,
        与插件运行时 _persist(黑名单/审批员)并发写同一存档时不互相覆盖。
        """
        from mohobot.file_store import json_update

        meta = next((m for m in self._plugins if m["name"] == name), None)
        if meta is None or meta.get("config_schema") is None:
            return False
        # 按 schema 类型强转(面板可能提交字符串/错误类型)
        coerced = self._coerce_config(meta["config_schema"], config or {})
        merged = self._deep_merge(
            self._schema_defaults(meta["config_schema"]), coerced
        )
        meta["config"] = merged
        try:
            self._config_dir.mkdir(parents=True, exist_ok=True)
            path = self._config_dir / f"{name}.json"

            def _merge(current):
                base = current if isinstance(current, dict) else {}
                return self._deep_merge(base, merged)

            await json_update(path, _merge, default=merged)
        except Exception as e:
            logger.error(f"Failed to save plugin config {name}: {e}")
            return False
        inst = meta.get("instance")
        if inst is not None:
            self._inject_config(inst, merged)
            updater = getattr(inst, "on_config_update", None)
            if callable(updater):
                try:
                    updater(merged)
                except Exception as e:
                    logger.error(f"Plugin {name} on_config_update failed: {e}")
        logger.info(f"Plugin {name} config updated (热生效)")
        return True

    async def reload_plugins(self) -> int:
        """热重载: 重新扫描 plugins/ 目录并加载全部插件。"""
        logger.info("Hot-reloading plugins...")
        async with self._lock():
            count = await self._reload_unlocked()
        logger.info(f"Plugin hot-reload complete: {count} plugin(s) active")
        return count

    async def unload_plugin(self, name: str) -> bool:
        """卸载单个已加载插件; 文件移除前先停任务并执行 on_shutdown。"""
        async with self._lock():
            meta = next((m for m in self._plugins if m["name"] == name), None)
            if meta is None:
                return False
            from mohobot.services.llm_tools import registry

            owner_states = None
            try:
                was_inactive = await registry.deactivate_owner(name)
                owner_states = {name: was_inactive}
                await self._wait_for_dispatches()
            except asyncio.TimeoutError:
                if owner_states is not None:
                    registry.restore_owner_activity(owner_states)
                logger.warning(f"Plugin {name} unload timed out before teardown")
                return False
            except BaseException:
                if owner_states is not None:
                    registry.restore_owner_activity(owner_states)
                raise
            await self._stop_tick_for(name)
            if self._task_supervisor is not None:
                await self._task_supervisor.cancel_owner_prefix(f"plugin:{name}")
            if meta.get("loaded"):
                await self._call_hook(meta, "on_shutdown")
            if self._task_supervisor is not None:
                await self._task_supervisor.cancel_owner_prefix(f"plugin:{name}")
            registry.unregister_owner(name)
            self._unload_plugin(meta)
            self._plugins.remove(meta)
            return True

    async def reload_plugin(self, name: str) -> bool:
        """只重载一个插件, 其余插件实例与周期任务保持不变。"""
        async with self._lock():
            meta = next((m for m in self._plugins if m["name"] == name), None)
            if meta is None:
                await self._reload_unlocked()
                meta = next((m for m in self._plugins if m["name"] == name), None)
                return bool(meta and meta.get("loaded"))
            from mohobot.services.llm_tools import registry

            entry = next((item for item in self._iter_entries() if item[1] == name), None)
            if entry is not None:
                previous = self._module_name_conflict(self._module_name(name), exclude_name=name)
                if previous is not None:
                    logger.error(f"插件模块名冲突: {name} 与 {previous}")
                    return False
            owner_states = None
            try:
                was_inactive = await registry.deactivate_owner(name)
                owner_states = {name: was_inactive}
                await self._wait_for_dispatches()
            except asyncio.TimeoutError:
                if owner_states is not None:
                    registry.restore_owner_activity(owner_states)
                logger.warning(f"Plugin {name} reload timed out before teardown")
                return False
            except BaseException:
                if owner_states is not None:
                    registry.restore_owner_activity(owner_states)
                raise
            old_tool_snapshot = registry.snapshot_owner_tools(name)
            index = self._plugins.index(meta)
            old_meta = dict(meta)
            old_imports = old_meta.get("_imports")
            await self._stop_tick_for(name)
            if self._task_supervisor is not None:
                await self._task_supervisor.cancel_owner_prefix(f"plugin:{name}")
            if meta.get("loaded"):
                await self._call_hook(meta, "on_shutdown")
            if self._task_supervisor is not None:
                await self._task_supervisor.cancel_owner_prefix(f"plugin:{name}")
            self._unload_plugin(meta)
            if entry is None:
                registry.unregister_owner(name)
                self._plugins.pop(index)
                return False
            try:
                new_meta = self._load_one(
                    entry[0], entry[1], entry[2], self._load_state(), entry[3]
                )
            except Exception as e:
                new_meta = self._error_meta(entry[0], entry[1], entry[2], entry[3], e)
                logger.error(f"Failed to reload plugin {entry[1]}: {e}")
                # A load error still permits restoring the old enabled instance.
                new_meta["enabled"] = old_meta["enabled"]
            from mohobot.plugins.sources import load_sources

            sources = await load_sources(self._data_dir)
            new_meta["source_record"] = sources.get(new_meta.get("name", ""))
            self._plugins[index] = new_meta
            started = False
            if new_meta.get("loaded"):
                try:
                    self.apply_injections()
                except Exception as e:
                    logger.error(f"Plugin injection failed after reload: {e}")
                started = await self._start_plugin(new_meta)

            if not started and new_meta.get("loaded"):
                await self._call_hook(new_meta, "on_shutdown")
                self._unload_plugin(new_meta)
                new_meta["error"] = new_meta.get("error") or "on_startup failed"
                new_meta["loaded"] = False

            if not started and new_meta.get("enabled", True) and old_meta.get("loaded"):
                if old_imports is not None and not old_imports.restore():
                    new_meta["error"] = f"{new_meta['error']}; previous plugin modules are occupied"
                    logger.error(f"Plugin {name} rollback failed: {new_meta['error']}")
                    return False
                self._plugins[index] = old_meta
                try:
                    self.apply_injections()
                except Exception as e:
                    logger.error(f"Plugin injection failed after reload rollback: {e}")
                rollback_started = await self._start_plugin(old_meta)
                if rollback_started:
                    try:
                        registry.restore_owner_tools(name, old_tool_snapshot)
                    except Exception as e:
                        logger.error(f"Plugin {name} tool rollback failed: {e}")
                        rollback_started = False
                if rollback_started:
                    registry.restore_owner_activity(owner_states or {})
                    self._start_tick_for(old_meta)
                else:
                    registry.unregister_owner(name)
                    old_meta["loaded"] = False
                    old_meta["error"] = "previous plugin failed to restart after reload"
                logger.error(
                    f"Plugin {name} reload failed, kept previous instance: {new_meta.get('error')}"
                )
                return False
            if not started:
                registry.unregister_owner(name)
                self._plugins[index] = new_meta
                return False
            self._start_tick_for(new_meta)
            return True

    # ── Plugin State (enable/disable) ─────────────────────────

    def _load_state(self) -> dict[str, Any]:
        """Load plugin enable/disable state from data/plugins_state.json."""
        try:
            if self._state_file.exists():
                return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to load plugin state: {e}")
        return {}

    async def _save_state(self, state: dict[str, Any]) -> None:
        """原子合并写入插件启停状态。"""
        from mohobot.file_store import json_update

        try:
            await json_update(
                self._state_file,
                lambda current: {**(current if isinstance(current, dict) else {}), **state},
                default=state,
            )
        except Exception as e:
            logger.error(f"Failed to save plugin state: {e}")

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/禁用插件, 立即生效(无需重启)。"""
        async with self._lock():
            meta = next((m for m in self._plugins if m["name"] == name), None)
            if meta is None:
                return False
            update = {name: {"enabled": enabled}}
            if not enabled or not meta.get("loaded"):
                await self._reload_unlocked(enabled_update=update)
            else:
                await self._save_state(update)
                meta["enabled"] = enabled
        logger.info(f"Plugin {name} {'enabled' if enabled else 'disabled'} (热生效)")
        return True

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return plugin metadata (without instances) for the web panel."""
        result = []
        for meta in tuple(self._plugins):
            item = {k: v for k, v in meta.items() if k != "instance" and not k.startswith("_")}
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
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_notice", None)
                if handler:
                    await self._run_plugin_handler(handler, bot_id, event, raw)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} notice handler error: {e}")

    async def dispatch_meta(self, bot_id: str, event: MetaEvent, raw: dict) -> None:
        """Dispatch a meta event to all enabled plugins."""
        for meta in tuple(self._plugins):
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_meta", None)
                if handler:
                    await self._run_plugin_handler(handler, bot_id, event, raw)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} meta handler error: {e}")

    async def dispatch_request(
        self, bot_id: str, event, raw: dict,
    ) -> bool:
        """Dispatch a request event (好友申请/群邀请) to plugins.

        Returns True if a plugin handled the request (框架不再自动同意)。
        """
        for meta in tuple(self._plugins):
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_request", None)
                if handler:
                    handled = await self._run_plugin_handler(handler, bot_id, event, raw)
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
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_message_observed", None)
                if handler:
                    handled, response = await self._run_plugin_handler(handler, bot_id, event, raw)
                    if handled:
                        return (True, response)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} observe handler error: {e}")
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
                                msg_handler, bot_id, event, raw
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
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_perception", None)
                if not handler:
                    continue
                text = await self._run_plugin_handler(handler, bot_id, event, raw)
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
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            if not self._bot_allowed(meta, bot_id):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_message", None)
                if handler:
                    handled, response = await self._run_plugin_handler(handler, bot_id, event, raw_event)
                    if handled:
                        return (True, response)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} message handler error: {e}")
        return (False, None)
