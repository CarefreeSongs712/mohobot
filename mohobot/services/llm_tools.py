"""Plugin-declared tools shared by Legacy and Agent LLM paths."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class LLMTool:
    schema: dict[str, Any]
    handler: Callable[..., Any]

    @property
    def name(self) -> str:
        return self.schema["function"]["name"]


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

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema for tool in self._tools.values()]

    async def execute(self, name: str, arguments: str | dict[str, Any] | None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
        try:
            args = json.loads(arguments) if isinstance(arguments, str) and arguments else (arguments or {})
            if not isinstance(args, dict):
                return json.dumps({"error": "工具参数必须是 JSON 对象"}, ensure_ascii=False)
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
    except Exception:
        return


load_plugin_tools()
