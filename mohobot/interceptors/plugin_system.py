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

import importlib.util
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.interceptors.base import Interceptor
from mohobot.models.onebot import (
    Event,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
)


class PluginSystem(Interceptor):
    """Loads and manages plugins from a designated directory."""

    def __init__(self, plugins_dir: str = "./plugins", data_dir: str = "./data"):
        self._plugins_dir = Path(plugins_dir)
        self._data_dir = Path(data_dir)
        self._plugins: list[dict[str, Any]] = []  # Plugin metadata + instance
        # {plugin_name: {"enabled": bool, "file": str, "loaded": bool, "info": {...}}}
        self._state_file = self._data_dir / "plugins_state.json"
        # 运行时注入引用(热重载后重新注入)
        self._ws_server = None
        self._bot_manager = None
        self._anysearch_client = None

    # ── 运行时注入(热重载后自动重新注入) ────────────────────

    def set_runtime_refs(self, ws_server=None, bot_manager=None, anysearch_client=None) -> None:
        """保存运行时引用, 供 load/reload 后注入插件。"""
        if ws_server is not None:
            self._ws_server = ws_server
        if bot_manager is not None:
            self._bot_manager = bot_manager
        if anysearch_client is not None:
            self._anysearch_client = anysearch_client

    def apply_injections(self) -> None:
        """对已加载插件实例执行注入(ws_server / bot_manager / data_dir / anysearch)。

        通过实例的类注入(classmethod), 避免 re-import 产生第二份模块对象。
        """
        for meta in self._plugins:
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
            if self._anysearch_client is not None:
                injector = getattr(inst.__class__, "inject_anysearch_client", None)
                if injector:
                    injector(self._anysearch_client)

    async def load_plugins(self) -> int:
        """Scan plugins directory and load all enabled plugins."""
        self._plugins.clear()

        if not self._plugins_dir.exists():
            self._plugins_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created plugins directory: {self._plugins_dir}")
            return 0

        state = self._load_state()

        # Add plugins dir to sys.path
        plugins_path = str(self._plugins_dir.absolute())
        if plugins_path not in sys.path:
            sys.path.insert(0, plugins_path)

        count = 0
        for entry in sorted(self._plugins_dir.iterdir()):
            if entry.suffix != ".py" or entry.name.startswith("_"):
                continue
            if entry.name == "__init__.py":
                continue

            plugin_name = entry.stem
            plugin_state = state.get(plugin_name, {"enabled": True})
            enabled = plugin_state.get("enabled", True)

            meta = {
                "name": plugin_name,
                "file": entry.name,
                "enabled": enabled,
                "loaded": False,
                "error": None,
                "info": {},
                "loaded_at": None,
            }

            if not enabled:
                self._plugins.append(meta)
                logger.info(f"Plugin {plugin_name} skipped (disabled)")
                continue

            try:
                module_name = f"mohobot_plugin_{plugin_name}"
                spec = importlib.util.spec_from_file_location(module_name, entry)
                if spec is None or spec.loader is None:
                    meta["error"] = "spec load failed"
                    self._plugins.append(meta)
                    continue

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Find Plugin class in module
                plugin_instance = None
                for name, cls in inspect.getmembers(module, inspect.isclass):
                    if name == "Plugin" and cls.__module__ == module.__name__:
                        plugin_instance = cls()
                        break

                if plugin_instance is None:
                    meta["error"] = "no Plugin class found"
                    self._plugins.append(meta)
                    continue

                meta["instance"] = plugin_instance
                meta["loaded"] = True
                meta["loaded_at"] = time.time()
                # Collect plugin info: class docstring + optional class-level `info` dict
                # (plugins may declare info = {"commands": [{"name": "...", "desc": "..."}]})
                meta["info"] = {
                    "description": inspect.getdoc(plugin_instance.__class__) or "",
                }
                cls_info = getattr(plugin_instance.__class__, "info", None)
                if isinstance(cls_info, dict):
                    meta["info"].update(cls_info)
                self._plugins.append(meta)
                count += 1
                logger.info(f"Loaded plugin: {plugin_name}")
            except Exception as e:
                meta["error"] = str(e)
                self._plugins.append(meta)
                logger.error(f"Failed to load plugin {entry.name}: {e}")

        logger.info(f"Plugin system loaded {count} plugin(s)")
        # 加载完成后注入运行时引用(热重载也走这里)
        self.apply_injections()
        return count

    async def reload_plugins(self) -> int:
        """热重载: 重新扫描 plugins/ 目录并加载全部插件。

        新增/修改/删除插件文件、启停状态均立即生效, 无需重启。
        """
        logger.info("Hot-reloading plugins...")
        self._plugins.clear()
        count = await self.load_plugins()
        logger.info(f"Plugin hot-reload complete: {count} plugin(s) active")
        return count

    # ── Plugin State (enable/disable) ─────────────────────────

    def _load_state(self) -> dict[str, Any]:
        """Load plugin enable/disable state from data/plugins_state.json."""
        try:
            if self._state_file.exists():
                return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to load plugin state: {e}")
        return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        """Persist plugin state to data/plugins_state.json."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"Failed to save plugin state: {e}")

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/禁用插件, 立即生效(无需重启)。"""
        state = self._load_state()
        state[name] = {"enabled": enabled}
        self._save_state(state)

        found = False
        for meta in self._plugins:
            if meta["name"] == name:
                meta["enabled"] = enabled
                found = True
                break
        if not found:
            # 未扫描到(如新文件) — 全量重载以纳入
            await self.reload_plugins()
            return True
        if enabled:
            # 热启用: 若该插件此前加载失败或未加载, 重新加载它
            meta = next((m for m in self._plugins if m["name"] == name), None)
            if meta is not None and not meta.get("loaded"):
                logger.info(f"Plugin {name} 热启用: 重新加载")
                await self.reload_plugins()
        logger.info(f"Plugin {name} {'enabled' if enabled else 'disabled'} (热生效)")
        return True

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return plugin metadata (without instances) for the web panel."""
        result = []
        for meta in self._plugins:
            item = {k: v for k, v in meta.items() if k != "instance"}
            result.append(item)
        return result

    # ── Event Dispatch ────────────────────────────────────────

    async def dispatch_notice(self, bot_id: str, event: NoticeEvent, raw: dict) -> None:
        """Dispatch a notice event to all enabled plugins."""
        for meta in self._plugins:
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_notice", None)
                if handler:
                    await handler(bot_id, event, raw)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} notice handler error: {e}")

    async def dispatch_meta(self, bot_id: str, event: MetaEvent, raw: dict) -> None:
        """Dispatch a meta event to all enabled plugins."""
        for meta in self._plugins:
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_meta", None)
                if handler:
                    await handler(bot_id, event, raw)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} meta handler error: {e}")

    async def intercept(
        self,
        bot_id: str,
        event: MessageEvent,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | list[dict[str, Any]] | None]:
        """Give plugins a chance to intercept messages."""
        for meta in self._plugins:
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_message", None)
                if handler:
                    handled, response = await handler(bot_id, event, raw_event)
                    if handled:
                        return (True, response)
            except Exception as e:
                logger.error(f"Plugin {meta['name']} message handler error: {e}")
        return (False, None)