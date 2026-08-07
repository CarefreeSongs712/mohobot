"""Command interceptor — handles slash commands like /sess, /forget, /hist, /help.

Commands are parsed from the message text and executed by delegating
to context_manager and llm_service / ws_server as appropriate.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from mohobot.interceptors.base import Interceptor
from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, MessageEvent


class CommandHandler(Interceptor):
    """Handles slash commands for session management and utilities."""

    def __init__(self, context_manager, llm_service, ws_server):
        self._ctx_mgr = context_manager
        self._llm = llm_service
        self._ws = ws_server
        # Command registry: {name: (handler_func, help_text)}
        self._commands: dict[str, tuple] = {
            "sess":   (self._cmd_sess,    "会话管理: /sess list|new <name>|switch <id>|del <id>"),
            "forget": (self._cmd_forget,  "删除最近 N 条对话: /forget <n>"),
            "hist":   (self._cmd_hist,    "打印当前会话内容 (调试)"),
            "help":   (self._cmd_help,    "显示此帮助"),
            "clear":  (self._cmd_clear,   "清空当前会话"),
        }

    async def intercept(
        self,
        bot_id: str,
        event: MessageEvent,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | list[dict[str, Any]] | None]:
        """Check if the message is a command; if so, execute and return."""
        prefix = "/"  # Could be per-bot configurable

        # Extract plain text from message
        if isinstance(event.message, str):
            text = event.message.strip()
        elif isinstance(event.message, list):
            text = ""
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
            text = text.strip()
        else:
            text = ""

        if not text.startswith(prefix):
            return (False, None)

        # Parse command
        parts = text[len(prefix):].strip().split(maxsplit=2)
        cmd_name = parts[0].lower() if parts else ""
        args = parts[1:] if len(parts) > 1 else []

        handler = self._commands.get(cmd_name)
        if not handler:
            # Unknown command — let LLM handle it or return help
            return (False, None)

        # Execute command
        try:
            reply = await handler[0](bot_id, event, args)
            if reply:
                return (True, reply)
            return (True, None)  # Handled but no reply needed
        except Exception as e:
            logger.error(f"Command '{cmd_name}' error: {e}")
            return (True, f"命令执行出错: {e}")

    async def _get_target_info(
        self, event: MessageEvent
    ) -> tuple[str, str | int, str | int]:
        """Get (chat_type, user_or_group_id, target_id) from event."""
        if isinstance(event, PrivateMessageEvent):
            return ("private", str(event.user_id), str(event.user_id))
        elif isinstance(event, GroupMessageEvent):
            return ("group", str(event.group_id), str(event.user_id))
        return ("private", str(event.user_id), str(event.user_id))

    async def _cmd_sess(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """Session management."""
        chat_type, chat_id, _ = await self._get_target_info(event)

        if not args:
            return "用法: /sess list|new <name>|switch <id>|del <id>"

        sub = args[0].lower()

        if sub == "list":
            sessions = await self._ctx_mgr.list_sessions(bot_id, chat_type, chat_id)
            if not sessions:
                return "暂无会话。"
            active = await self._ctx_mgr.get_active_session_id(bot_id, chat_type, chat_id)
            lines = [f"当前会话: {active}"]
            for s in sessions:
                marker = " ⬅" if s["id"] == active else ""
                lines.append(f"  [{s['id']}] {s.get('name', '')}{marker}")
            return "\n".join(lines)

        elif sub == "new":
            name = " ".join(args[1:]) if len(args) > 1 else f"会话-{len(args)}"
            sess_id = await self._ctx_mgr.create_session(bot_id, chat_type, chat_id, name)
            return f"已创建并切换到会话: {sess_id}"

        elif sub == "switch":
            if len(args) < 2:
                return "请指定会话 ID。"
            target = args[1]
            ok = await self._ctx_mgr.switch_session(bot_id, chat_type, chat_id, target)
            if ok:
                return f"已切换到会话: {target}"
            return f"会话不存在: {target}"

        elif sub == "del":
            if len(args) < 2:
                return "请指定会话 ID。"
            target = args[1]
            ok = await self._ctx_mgr.delete_session(bot_id, chat_type, chat_id, target)
            if ok:
                return f"已删除会话: {target}"
            return f"会话不存在或无法删除: {target}"

        return f"未知子命令: {sub}"

    async def _cmd_forget(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """Forget last N messages in current session."""
        chat_type, chat_id, _ = await self._get_target_info(event)
        n = int(args[0]) if args else 1
        if n <= 0:
            return "请输入正数。"
        result = await self._ctx_mgr.forget_last_n(bot_id, chat_type, chat_id, n)
        if result > 0:
            return f"已删除最近 {result} 条对话记录。"
        return "会话中没有可删除的记录。"

    async def _cmd_hist(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """Print current session context (debug)."""
        chat_type, chat_id, _ = await self._get_target_info(event)
        context = await self._ctx_mgr.load_context(bot_id, chat_type, chat_id)
        if not context:
            return "当前会话为空。"
        lines = [f"会话共 {len(context)} 条记录:"]
        for i, entry in enumerate(context, 1):
            role = entry.get("role", "?")
            content = entry.get("content", "")
            if isinstance(content, str):
                preview = content[:200].replace("\n", "\\n")
            else:
                preview = str(content)[:200]
            lines.append(f"  {i}. [{role}] {preview}")
        return "\n".join(lines)

    async def _cmd_help(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """Show help."""
        lines = ["可用命令:"] + [f"  {h}" for _, h in self._commands.values()]
        return "\n".join(lines)

    async def _cmd_clear(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """Clear current session."""
        chat_type, chat_id, _ = await self._get_target_info(event)
        await self._ctx_mgr.clear_context(bot_id, chat_type, chat_id)
        return "当前会话已清空。"