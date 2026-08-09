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
        # 插件配置目录(全局一份): data/plugins_config/{name}.json
        self._config_dir = Path(data_dir) / "plugins_config"
        # 运行时注入引用(热重载后重新注入)
        self._ws_server = None
        self._bot_manager = None
        self._anysearch_client = None
        self._admin_ids: set[str] = set()  # 全局管理员(封禁/插件命令共用)

    # ── 管理员注入 ─────────────────────────────────────────────

    def set_admin_ids(self, admins: list[int] | None) -> None:
        """热同步全局管理员(web 面板保存后调用)。"""
        if admins is not None:
            self._admin_ids = {str(a) for a in admins}
        self.apply_admin_injection()

    def apply_admin_injection(self) -> None:
        """把管理员列表注入插件实例(类级 inject_admin_ids classmethod)。"""
        for meta in self._plugins:
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

    def set_runtime_refs(self, ws_server=None, bot_manager=None, anysearch_client=None) -> None:
        """保存运行时引用, 供 load/reload 后注入插件。"""
        if ws_server is not None:
            self._ws_server = ws_server
        if bot_manager is not None:
            self._bot_manager = bot_manager
        if anysearch_client is not None:
            self._anysearch_client = anysearch_client

    def apply_injections(self) -> None:
        """对已加载插件实例执行注入(ws_server / bot_manager / data_dir / anysearch / admins)。

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
        # 管理员注入(类级)
        self.apply_admin_injection()

    async def load_plugins(self) -> int:
        """Scan plugins directory and load all enabled plugins.

        支持两种形态:
        - 单文件插件: plugins/xxx.py (定义 class Plugin)
        - 目录插件:   plugins/xxx/main.py (定义 class Plugin, 可含 core/ 子模块与 _conf_schema.json)
        """
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
            # 定位插件入口文件: 单文件 xxx.py 或 目录 xxx/main.py
            entry_path = None
            plugin_name = ""
            if entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("_") and entry.name != "__init__.py":
                entry_path = entry
                plugin_name = entry.stem
            elif entry.is_dir() and not entry.name.startswith("_") and entry.name != "__pycache__":
                main_file = entry / "main.py"
                if main_file.exists():
                    entry_path = main_file
                    plugin_name = entry.name
                else:
                    continue
            else:
                continue

            plugin_state = state.get(plugin_name, {"enabled": True})
            enabled = plugin_state.get("enabled", True)

            meta = {
                "name": plugin_name,
                "file": str(entry_path.relative_to(self._plugins_dir)),
                "entry_dir": str(entry_path.parent) if entry_path.parent != self._plugins_dir else "",
                "enabled": enabled,
                "loaded": False,
                "error": None,
                "info": {},
                "config_schema": None,
                "config": None,
                "loaded_at": None,
            }

            if not enabled:
                self._plugins.append(meta)
                logger.info(f"Plugin {plugin_name} skipped (disabled)")
                continue

            try:
                # 目录插件: 把插件目录加入 sys.path(其 main.py 内可 import 同目录模块)
                if meta["entry_dir"]:
                    plugin_dir_abs = str(entry_path.parent.absolute())
                    if plugin_dir_abs not in sys.path:
                        sys.path.insert(0, plugin_dir_abs)

                module_name = f"mohobot_plugin_{plugin_name}"
                spec = importlib.util.spec_from_file_location(module_name, entry_path)
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

                # 插件配置: 读取 _conf_schema.json(若有) + 合并已存配置
                schema = self._load_config_schema(entry_path.parent)
                if schema:
                    meta["config_schema"] = schema
                    meta["config"] = self._load_plugin_config(plugin_name, schema)
                    self._inject_config(plugin_instance, meta["config"])

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
        for meta in self._plugins:
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

    async def dispatch_request(
        self, bot_id: str, event, raw: dict,
    ) -> bool:
        """Dispatch a request event (好友申请/群邀请) to plugins.

        Returns True if a plugin handled the request (框架不再自动同意)。
        """
        for meta in self._plugins:
            if not meta.get("enabled") or not meta.get("loaded"):
                continue
            plugin = meta.get("instance")
            try:
                handler = getattr(plugin, "on_request", None)
                if handler:
                    handled = await handler(bot_id, event, raw)
                    if handled:
                        return True
            except Exception as e:
                logger.error(f"Plugin {meta['name']} request handler error: {e}")
        return False

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