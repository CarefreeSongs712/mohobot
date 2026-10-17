"""Plugin-declared tools shared by Legacy and Agent LLM paths."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class LLMTool:
    schema: dict[str, Any]
    handler: Callable[..., Any]
    # True 时 handler 以 handler(**args, context=<dict>) 调用; context 由
    # llm_service 在执行时注入(bot_id/会话/发送能力等运行时信息)。
    # 纯只读工具保持 False, 签名不变。
    context_aware: bool = False

    @property
    def name(self) -> str:
        return self.schema["function"]["name"]


# ── 插件启用状态(data/plugins_state.json, Web 面板开关的落点) ──

_STATE_PATH = Path("data/plugins_state.json")
_state_cache: dict[str, float] = {"mtime": -1.0}
_state_data: dict[str, Any] = {}


def _read_plugin_state() -> dict[str, Any]:
    """读取插件启停状态, 按 mtime 缓存(schemas()/execute 每次请求都会查询)。"""
    try:
        mtime = _STATE_PATH.stat().st_mtime
    except OSError:
        return {}
    if mtime != _state_cache["mtime"]:
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _state_data.clear()
                _state_data.update(data)
        except Exception:
            pass
        _state_cache["mtime"] = mtime
    return _state_data


def plugin_enabled(name: str) -> bool:
    """插件是否启用；plugins_state.json 未登记的视为启用。"""
    try:
        mtime = _STATE_PATH.stat().st_mtime
    except OSError:
        return True
    if mtime != _state_cache["mtime"]:
        _read_plugin_state()
    entry = _state_data.get(name)
    if isinstance(entry, dict):
        return bool(entry.get("enabled", True))
    return True


def owner_plugin_of(tool: LLMTool) -> str | None:
    """根据 handler 来源模块推导所属插件(plugins.<name>), 内置工具返回 None。"""
    mod = getattr(tool.handler, "__module__", "") or ""
    if mod.startswith("plugins."):
        return mod[len("plugins."):].split(".")[0]
    return None


def _tool_allowed(tool: LLMTool) -> bool:
    owner = owner_plugin_of(tool)
    return owner is None or plugin_enabled(owner)


class LLMToolRegistry:
    """Process-local registry populated by loaded plugins."""

    def __init__(self) -> None:
        self._tools: dict[str, LLMTool] = {}

    def register(self, tool: LLMTool) -> None:
        name = tool.name
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"invalid LLM tool name: {name!r}")
        if name in self._tools:
            raise ValueError(f"duplicate LLM tool: {name}")
        self._tools[name] = tool

    def contains(self, name: str) -> bool:
        """Return whether a plugin registered this exact tool name."""
        return name in self._tools

    def schemas(self) -> list[dict[str, Any]]:
        """当前可暴露给 LLM 的 schema; 所属插件被禁用的工具不会出现。"""
        return [tool.schema for tool in self._tools.values() if _tool_allowed(tool)]

    async def execute(
        self, name: str, arguments: str | dict[str, Any] | None,
        context: dict[str, Any] | None = None,
    ) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
        if not _tool_allowed(tool):
            owner = owner_plugin_of(tool) or "未知"
            return json.dumps({"error": f"插件 {owner} 已禁用, 工具 {name} 不可用"}, ensure_ascii=False)
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            return json.dumps({"error": "工具参数必须是有效的 JSON"}, ensure_ascii=False)
        if not isinstance(args, dict):
            return json.dumps({"error": "工具参数必须是 JSON 对象"}, ensure_ascii=False)
        try:
            if tool.context_aware:
                result = tool.handler(**args, context=context or {})
            else:
                result = tool.handler(**args)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"error": f"工具执行失败: {exc}"}, ensure_ascii=False)


registry = LLMToolRegistry()


def tool_schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def load_plugin_tools() -> None:
    """Load built-in tool plugins before either LLM path builds schemas."""
    try:
        import plugins.song_tools  # noqa: F401
        import plugins.snitch  # noqa: F401  (告状: 需要会话上下文, run 时注入)
    except Exception:
        return


load_plugin_tools()
