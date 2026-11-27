"""Plugin-declared tools shared by Legacy and Agent LLM paths."""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator


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


@dataclass
class PluginToolStage:
    registry: LLMToolRegistry
    owner: str
    state_path: Path
    tools: dict[str, LLMTool] = field(default_factory=dict)


_registration_stage: ContextVar[PluginToolStage | None] = ContextVar(
    "llm_tool_registration_stage", default=None
)


# ── 插件启用状态(data/plugins_state.json, Web 面板开关的落点) ──

_STATE_PATH = Path("data/plugins_state.json")
_state_cache: dict[Path, tuple[int, int, dict[str, Any]]] = {}


def _read_plugin_state(state_path: Path = _STATE_PATH) -> dict[str, Any]:
    """读取插件启停状态, 按 mtime 缓存(schemas()/execute 每次请求都会查询)。"""
    try:
        stat = state_path.stat()
    except OSError:
        return {}
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    cached = _state_cache.get(state_path)
    if cached is not None and fingerprint == cached[:2]:
        return cached[2]
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _state_cache[state_path] = (*fingerprint, data)
            return data
    except Exception:
        pass
    return cached[2] if cached is not None else {}


def plugin_enabled(name: str, state_path: Path = _STATE_PATH) -> bool:
    """插件是否启用；plugins_state.json 未登记的视为启用。"""
    entry = _read_plugin_state(state_path).get(name)
    if isinstance(entry, dict):
        return bool(entry.get("enabled", True))
    return True


def owner_plugin_of(tool: LLMTool) -> str | None:
    """根据 handler 来源模块推导所属插件(plugins.<name>), 内置工具返回 None。"""
    mod = getattr(tool.handler, "__module__", "") or ""
    if mod.startswith("plugins."):
        return mod[len("plugins."):].split(".")[0]
    return None


class LLMToolRegistry:
    """Process-local registry populated by loaded plugins."""

    def __init__(self) -> None:
        self._tools: dict[str, LLMTool] = {}
        self._owners: dict[str, str] = {}
        self._owner_state_paths: dict[str, Path] = {}
        self._inactive_owners: set[str] = set()
        self._owner_inflight: dict[str, int] = {}
        self._owner_drained: dict[str, asyncio.Event] = {}
        self._owner_tasks: dict[str, dict[asyncio.Task[Any], int]] = {}
        self._owner_drain_timeout = 5.0

    def new_plugin_stage(self, owner: str, state_path: str | Path) -> PluginToolStage:
        """Create an unpublished registration set for one plugin load attempt."""
        return PluginToolStage(self, owner, Path(state_path))

    @contextmanager
    def registration_scope(self, stage: PluginToolStage) -> Iterator[None]:
        if stage.registry is not self:
            raise ValueError("plugin tool stage belongs to a different registry")
        token = _registration_stage.set(stage)
        try:
            yield
        finally:
            _registration_stage.reset(token)

    def set_owner_state_path(self, owner: str, state_path: str | Path) -> None:
        self._owner_state_paths[owner] = Path(state_path)

    def snapshot_owner_tools(self, owner: str) -> dict[str, LLMTool]:
        return {
            name: tool for name, tool in self._tools.items()
            if self._owners.get(name) == owner
        }

    def take_unowned_plugin_tools(self, owner: str) -> dict[str, LLMTool]:
        """Move eagerly imported tools into a plugin's pending stage.

        Built-in plugin modules are imported once when this registry is first
        imported, before :class:`PluginSystem` has a registration scope.  The
        plugin loader adopts those unowned tools before publishing its stage so
        disable, reload, and shutdown use the same owner lifecycle as normal
        staged registrations.
        """
        adopted: dict[str, LLMTool] = {}
        for name, tool in tuple(self._tools.items()):
            if name in self._owners or owner_plugin_of(tool) != owner:
                continue
            adopted[name] = tool
            self._tools.pop(name, None)
        return adopted

    def restore_owner_tools(self, owner: str, tools: dict[str, LLMTool]) -> None:
        for name in tools:
            if name in self._tools and self._owners.get(name) != owner:
                raise ValueError(f"LLM tool is already registered by another owner: {name}")
        for name, tool in tools.items():
            if name not in self._tools:
                self._tools[name] = tool
                self._owners[name] = owner

    def commit_plugin_stage(self, stage: PluginToolStage) -> None:
        if stage.registry is not self:
            raise ValueError("plugin tool stage belongs to a different registry")
        for name in stage.tools:
            if name in self._tools and self._owners.get(name) != stage.owner:
                raise ValueError(f"duplicate LLM tool: {name}")
        self.unregister_owner(stage.owner)
        self._owner_state_paths[stage.owner] = stage.state_path
        for name, tool in stage.tools.items():
            self._tools[name] = tool
            self._owners[name] = stage.owner
        self._inactive_owners.discard(stage.owner)

    @staticmethod
    def discard_plugin_stage(stage: PluginToolStage) -> None:
        stage.tools.clear()

    def block_owner(self, owner: str) -> None:
        self._inactive_owners.add(owner)

    async def _wait_for_owner(self, owner: str) -> None:
        if self._owner_inflight.get(owner, 0) <= 0:
            return
        event = self._owner_drained.get(owner)
        if event is None:
            raise RuntimeError(f"missing drain event for active LLM tool owner: {owner}")
        await event.wait()

    async def wait_for_owner(self, owner: str) -> None:
        await asyncio.wait_for(self._wait_for_owner(owner), timeout=self._owner_drain_timeout)

    async def deactivate_owner(
        self, owner: str, *, cancel_on_timeout: bool = False
    ) -> bool:
        owner_states = await self.deactivate_owners(
            [owner], cancel_on_timeout=cancel_on_timeout
        )
        return owner_states[owner]

    async def deactivate_owners(
        self, owners: list[str], *, cancel_on_timeout: bool = False
    ) -> dict[str, bool]:
        unique_owners = tuple(dict.fromkeys(owners))
        owner_states = {
            owner: owner in self._inactive_owners for owner in unique_owners
        }
        for owner in unique_owners:
            self.block_owner(owner)
        try:
            await asyncio.wait_for(
                asyncio.gather(*(self._wait_for_owner(owner) for owner in unique_owners)),
                timeout=self._owner_drain_timeout,
            )
        except asyncio.TimeoutError:
            if not cancel_on_timeout:
                self.restore_owner_activity(owner_states)
                raise
            self.cancel_owner_executions(unique_owners)
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(self._wait_for_owner(owner) for owner in unique_owners)
                    ),
                    timeout=self._owner_drain_timeout,
                )
            except BaseException:
                self.restore_owner_activity(owner_states)
                raise
        except BaseException:
            self.restore_owner_activity(owner_states)
            raise
        return owner_states

    def restore_owner_activity(self, owner_states: dict[str, bool]) -> None:
        for owner, was_inactive in owner_states.items():
            if was_inactive:
                self.block_owner(owner)
            else:
                self.activate_owner(owner)

    def cancel_owner_executions(self, owners: tuple[str, ...]) -> None:
        current_task = asyncio.current_task()
        for owner in owners:
            for task in tuple(self._owner_tasks.get(owner, {})):
                if task is not current_task and not task.done():
                    task.cancel()

    def activate_owner(self, owner: str) -> None:
        self._inactive_owners.discard(owner)

    def unregister_owner(self, owner: str) -> None:
        for name in [name for name, tool_owner in self._owners.items() if tool_owner == owner]:
            self._tools.pop(name, None)
            self._owners.pop(name, None)
        self._inactive_owners.add(owner)

    def _owner_of(self, name: str, tool: LLMTool) -> str | None:
        return self._owners.get(name) or owner_plugin_of(tool)

    def _tool_allowed(self, name: str, tool: LLMTool) -> bool:
        owner = self._owner_of(name, tool)
        if owner is None:
            return True
        registered_owner = self._owners.get(name)
        if registered_owner in self._inactive_owners:
            return False
        state_path = self._owner_state_paths.get(owner, _STATE_PATH)
        return plugin_enabled(owner, state_path)

    def _end_execution(self, owner: str, task: asyncio.Task[Any] | None) -> None:
        owner_tasks = self._owner_tasks.get(owner)
        if owner_tasks is not None and task is not None:
            task_count = owner_tasks.get(task, 0) - 1
            if task_count <= 0:
                owner_tasks.pop(task, None)
            else:
                owner_tasks[task] = task_count
            if not owner_tasks:
                self._owner_tasks.pop(owner, None)
        remaining = self._owner_inflight.get(owner, 0) - 1
        if remaining <= 0:
            self._owner_inflight.pop(owner, None)
            event = self._owner_drained.get(owner)
            if event is not None:
                event.set()
            return
        self._owner_inflight[owner] = remaining

    def register(self, tool: LLMTool) -> None:
        name = tool.name
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"invalid LLM tool name: {name!r}")
        stage = _registration_stage.get()
        if stage is not None and stage.registry is self:
            if name in stage.tools:
                raise ValueError(f"duplicate LLM tool: {name}")
            if name in self._tools and self._owners.get(name) != stage.owner:
                raise ValueError(f"duplicate LLM tool: {name}")
            stage.tools[name] = tool
            return
        if name in self._tools:
            raise ValueError(f"duplicate LLM tool: {name}")
        self._tools[name] = tool

    def contains(self, name: str) -> bool:
        """Return whether a plugin registered this exact tool name."""
        stage = _registration_stage.get()
        if stage is not None and stage.registry is self:
            if name in stage.tools:
                return True
            if self._owners.get(name) == stage.owner:
                return False
        return name in self._tools

    def unregister(self, name: str) -> None:
        """Remove a tool by name (no-op if absent).

        供服务降级/进程内重启时摘除自有工具(如知识库), 插件路径不使用。
        """
        self._tools.pop(name, None)
        self._owners.pop(name, None)

    def schemas(self) -> list[dict[str, Any]]:
        """当前可暴露给 LLM 的 schema; 所属插件被禁用的工具不会出现。"""
        return [
            tool.schema
            for name, tool in self._tools.items()
            if self._tool_allowed(name, tool)
        ]

    async def execute(
        self, name: str, arguments: str | dict[str, Any] | None,
        context: dict[str, Any] | None = None,
    ) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
        owner = self._owner_of(name, tool)
        if not self._tool_allowed(name, tool):
            owner = owner or "未知"
            return json.dumps({"error": f"插件 {owner} 已禁用, 工具 {name} 不可用"}, ensure_ascii=False)
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            return json.dumps({"error": "工具参数必须是有效的 JSON"}, ensure_ascii=False)
        if not isinstance(args, dict):
            return json.dumps({"error": "工具参数必须是 JSON 对象"}, ensure_ascii=False)
        tracked_owner = self._owners.get(name)
        execution_task: asyncio.Task[Any] | None = None
        if tracked_owner is not None:
            event = self._owner_drained.get(tracked_owner)
            if event is None:
                event = asyncio.Event()
                event.set()
                self._owner_drained[tracked_owner] = event
            if self._owner_inflight.get(tracked_owner, 0) == 0:
                event.clear()
            self._owner_inflight[tracked_owner] = self._owner_inflight.get(tracked_owner, 0) + 1
            execution_task = asyncio.current_task()
            if execution_task is not None:
                owner_tasks = self._owner_tasks.setdefault(tracked_owner, {})
                owner_tasks[execution_task] = owner_tasks.get(execution_task, 0) + 1
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
        finally:
            if tracked_owner is not None:
                self._end_execution(tracked_owner, execution_task)


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
