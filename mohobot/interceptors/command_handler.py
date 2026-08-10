"""Command interceptor — handles slash commands like /sess, /forget, /hist, /help.

Commands are parsed from the message text and executed by delegating
to context_manager and llm_service / ws_server as appropriate.
"""

from __future__ import annotations

import re
import time as _time
from typing import Any

from loguru import logger

from mohobot.interceptors.base import Interceptor
from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, MessageEvent


class CommandHandler(Interceptor):
    """Handles slash commands for session management and utilities."""

    # 未知指令提醒冷却(秒): 同一会话内 60 分钟最多提醒一次
    UNKNOWN_CMD_COOLDOWN = 3600

    def __init__(self, context_manager, llm_service, ws_server, plugin_system=None):
        self._ctx_mgr = context_manager
        self._llm = llm_service
        self._ws = ws_server
        self._plugin_system = plugin_system
        # Command registry: {name: (handler_func, help_text)}
        # help_text format: "<用途说明> | 用法: /cmd ..."
        self._commands: dict[str, tuple] = {
            "sess":   (self._cmd_sess,    "会话管理 | 用法: /sess list|new <name>|switch <id>|del <id>"),
            "forget": (self._cmd_forget,  "删除最近 N 条对话 | 用法: /forget <n>"),
            "hist":   (self._cmd_hist,    "打印当前会话内容 (调试)"),
            "help":   (self._cmd_help,    "显示此帮助"),
            "clear":  (self._cmd_clear,   "清空当前会话"),
        }
        # Plugin-provided commands appear in /help (name -> {desc, plugin, admin})
        self._plugin_commands: dict[str, dict] = {}
        # 未知指令提醒时间戳: {(bot_id, chat_type, chat_id): last_remind_ts}
        self._unknown_remind_at: dict[tuple[str, str, str], float] = {}

    def register_plugin_commands(self, commands: dict[str, str]) -> None:
        """Register commands provided by plugins, shown in /help output."""
        for name, desc in commands.items():
            self._plugin_commands[name] = {"desc": desc, "plugin": "", "admin": False}

    def collect_plugin_commands(self, bot_id: str | None = None) -> dict[str, dict]:
        """Discover plugin commands from the plugin system (status hook).

        返回 {name: {"desc", "plugin", "admin"}} — plugin=插件名(分组依据),
        admin 仅读取插件声明的 info.commands[].admin 字段。
        传 bot_id 时跳过 per-bot 绑定(bind_bots)不包含该 bot 的插件命令。
        """
        discovered: dict[str, dict] = {}
        if self._plugin_system:
            # 优先内部列表(含 instance 可判断 bind_bots); 无则退回公开接口
            metas = getattr(self._plugin_system, "_plugins", None)
            if not metas:
                metas = self._plugin_system.list_plugins()
            for meta in metas:
                name = meta.get("name", "")
                inst = meta.get("instance")
                if inst is not None and bot_id is not None:
                    bind = getattr(inst.__class__, "bind_bots", None)
                    if bind and bot_id not in {str(b) for b in bind}:
                        continue
                info = meta.get("info") or {}
                cmd_list = info.get("commands") or []
                for cmd in cmd_list:
                    if isinstance(cmd, dict):
                        cmd_name = cmd.get("name", "")
                        if not cmd_name:
                            continue
                        discovered[cmd_name] = {
                            "desc": cmd.get("desc", ""),
                            "plugin": name,
                            "admin": bool(cmd.get("admin", False)),
                        }
                    elif isinstance(cmd, str):
                        discovered[cmd] = {"desc": "", "plugin": name, "admin": False}
        return discovered

    def _all_plugin_commands(self, bot_id: str | None = None) -> dict[str, dict]:
        """合并手动注册 + 动态收集的插件命令(动态优先)。"""
        merged = dict(self._plugin_commands)
        merged.update(self.collect_plugin_commands(bot_id))
        return merged

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
            # Plugin-registered command? (e.g. 赞我 via /赞我) — let plugins handle it
            if cmd_name in self._all_plugin_commands(bot_id):
                return (False, None)
            # Unknown command — 60 分钟内同一会话最多提醒一次,
            # 冷却期内静默拦截(不回复, 也不传给 LLM)
            return await self._handle_unknown_command(bot_id, event, cmd_name)

        # Execute command — 指令存在但出错时每次都回复(不节流)
        try:
            reply = await handler[0](bot_id, event, args)
            if reply:
                return (True, reply)
            return (True, None)  # Handled but no reply needed
        except Exception as e:
            logger.error(f"Command '{cmd_name}' error: {e}")
            return (True, f"命令执行出错: {e}")

    async def _handle_unknown_command(
        self, bot_id: str, event: MessageEvent, cmd_name: str
    ) -> tuple[bool, str | None]:
        """未知指令: 60 分钟冷却, 同一会话内只提醒一次。"""
        chat_type, chat_id, _ = await self._get_target_info(event)
        key = (bot_id, chat_type, str(chat_id))
        now = _time.time()

        last = self._unknown_remind_at.get(key, 0.0)
        if now - last < self.UNKNOWN_CMD_COOLDOWN:
            logger.debug(
                f"Unknown command '/{cmd_name}' throttled "
                f"(last reminder {int(now - last)}s ago)"
            )
            return (True, None)

        self._unknown_remind_at[key] = now
        self._prune_unknown_reminders(now)
        return (True, f"未知指令: /{cmd_name}。输入 /help 查看可用指令。")

    def _prune_unknown_reminders(self, now: float) -> None:
        """清理超过两倍冷却期的记录, 防止字典无限增长。"""
        stale_before = now - self.UNKNOWN_CMD_COOLDOWN * 2
        for key in [k for k, ts in self._unknown_remind_at.items() if ts < stale_before]:
            del self._unknown_remind_at[key]

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

    # 封禁管理命令(管理员) — 与 ban_filter 的命令集对应
    _BAN_CMD_DESC = {
        "ban": "封禁 <@|QQ> [时间] [理由]",
        "ban-all": "全局封禁 <@|QQ> [时间] [理由]",
        "pass": "临时解禁 <@|QQ> [时间] [理由]",
        "pass-all": "全局临时解禁 <@|QQ> [时间] [理由]",
        "dec-ban": "删除/削减会话封禁",
        "dec-ban-all": "删除/削减全局封禁",
        "dec-pass": "删除/削减会话解禁",
        "dec-pass-all": "删除/削减全局解禁",
        "ban-reset": "清除该用户全部记录",
        "banlist": "查看封禁/解禁名单(所有人可用)",
        "ban-enable": "临时启用封禁",
        "ban-disable": "临时禁用封禁",
        "ban-help": "封禁系统使用指南",
    }

    def _build_help_sections(self, bot_id: str | None = None) -> list[dict]:
        """构建 /help 的分组数据: 系统 / 封禁管理 / 各插件。"""
        sections = []
        # 系统(内置命令)
        builtin = []
        for name, (_, help_text) in self._commands.items():
            if name == "help":
                continue  # help 本身在标题下方说明
            desc = help_text.split("|")[0].strip()
            builtin.append({"name": name, "desc": desc, "admin": False})
        sections.append({"title": "系统", "commands": builtin})

        # 封禁管理(管理员)
        ban_cmds = [
            {"name": name, "desc": self._BAN_CMD_DESC.get(name, ""),
             "admin": name not in ("banlist", "ban-help")}
            for name in ("ban", "ban-all", "pass", "pass-all",
                         "dec-ban", "dec-ban-all", "dec-pass", "dec-pass-all",
                         "ban-reset", "banlist", "ban-enable", "ban-disable", "ban-help")
        ]
        sections.append({"title": "封禁管理 (管理员)", "commands": ban_cmds})

        # 插件命令: 按插件名分组
        by_plugin: dict[str, list[dict]] = {}
        for name, meta in self._all_plugin_commands(bot_id).items():
            plugin = meta.get("plugin") or "其他"
            by_plugin.setdefault(plugin, []).append({
                "name": name,
                "desc": meta.get("desc", ""),
                "admin": bool(meta.get("admin", False)),
            })
        for plugin in sorted(by_plugin):
            sections.append({
                "title": f"插件 · {plugin}",
                "commands": sorted(by_plugin[plugin], key=lambda c: c["name"]),
            })
        return sections

    def _help_text(self, bot_id: str | None = None) -> str:
        """文本版帮助(图片渲染失败/无法发送时的降级)。"""
        lines = ["📖 可用指令:"]
        for name, (_, help_text) in self._commands.items():
            lines.append(f"  /{name} — {help_text}")
        for name, meta in sorted(self._all_plugin_commands(bot_id).items()):
            desc = meta.get("desc") or "插件指令"
            lines.append(f"  /{name} — {desc}")
        return "\n".join(lines)

    async def _cmd_help(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """Show help — 渲染成 PIL 图片发送, 失败降级为文本。"""
        from mohobot.utils.image_card import render_help_card
        from mohobot.models.onebot import GroupMessageEvent as _G

        sections = self._build_help_sections(bot_id)
        img_path = render_help_card(sections)
        if img_path is not None and self._ws is not None:
            try:
                if isinstance(event, _G):
                    chat_type, chat_id = "group", str(event.group_id)
                else:
                    chat_type, chat_id = "private", str(event.user_id)
                await self._ws.send_image(bot_id, chat_type, chat_id, img_path)
                import os
                os.remove(img_path)
                return None  # 已发送图片
            except Exception as e:
                logger.warning(f"发送帮助图片失败, 降级为文本: {e}")
                try:
                    import os
                    os.remove(img_path)
                except OSError:
                    pass
        return self._help_text(bot_id)

    async def _cmd_clear(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """Clear current session."""
        chat_type, chat_id, _ = await self._get_target_info(event)
        await self._ctx_mgr.clear_context(bot_id, chat_type, chat_id)
        return "当前会话已清空。"