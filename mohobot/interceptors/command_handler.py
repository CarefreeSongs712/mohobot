"""Command interceptor — handles slash commands like /sess, /forget, /hist, /help.

Commands are parsed from the message text and executed by delegating
to context_manager and llm_service / ws_server as appropriate.
"""

from __future__ import annotations

import re
import shlex
import time as _time
from typing import Any

from loguru import logger

from mohobot.interceptors.base import Interceptor
from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, MessageEvent


class CommandHandler(Interceptor):
    """Handles slash commands for session management and utilities."""

    # 未知指令提醒冷却(秒): 同一会话内 60 分钟最多提醒一次
    UNKNOWN_CMD_COOLDOWN = 3600

    def __init__(self, context_manager, llm_service, ws_server, plugin_system=None,
                 emotion_manager=None, tts_service=None, admins=None, persona_service=None,
                 persona_ab_service=None):
        self._persona_ab = persona_ab_service
        self._ctx_mgr = context_manager
        self._llm = llm_service
        self._ws = ws_server
        self._plugin_system = plugin_system
        self._tts = tts_service
        self._persona_service = persona_service
        self._admins: set[str] = {str(a) for a in (admins or [])}
        # Command registry: {name: (handler_func, help_text)}
        # help_text format: "<用途说明> | 用法: /cmd ..."
        self._commands: dict[str, tuple] = {
            "sess":   (self._cmd_sess,    "会话管理 | 用法: /sess list|new <name>|switch <id>|del <id>"),
            "forget": (self._cmd_forget,  "删除最近 N 条对话 | 用法: /forget <n>"),
            "hist":   (self._cmd_hist,    "打印当前会话内容 (调试)"),
            "help":   (self._cmd_help,    "显示此帮助"),
            "clear":  (self._cmd_clear,   "清空当前会话"),
            "tts":    (self._cmd_tts,     "语音合成 | 用法: /tts <文本>(非管理员限30字)"),
            "ab": (self._cmd_ab, "匿名人设评估 | /ab A|B|平局|都不好|跳过；/ab vote <id> A；/ab switch <id> A|B"),
            "persona": (self._cmd_persona, "私聊会话人设管理 (仅全局管理员) | " + self._PERSONA_USAGE),
        }
        # 情感系统命令(未启用时 emotion_manager 为 None, 不注册)
        if emotion_manager is not None:
            try:
                from mohobot.emotion.commands import build_command_registry
                self._commands.update(build_command_registry(emotion_manager))
            except Exception as e:
                logger.warning(f"情感命令注册失败: {e}")
        # Plugin-provided commands appear in /help (name -> {desc, plugin, admin})
        self._plugin_commands: dict[str, dict] = {}
        # 未知指令提醒时间戳: {(chat_type, chat_id): last_remind_ts} (会话级节流)
        self._unknown_remind_at: dict[tuple[str, str], float] = {}
        # /tts 指令冷却: {(bot_id, user_id): last_use_ts} (非管理员)
        self._tts_last_use: dict[tuple[str, str], float] = {}

    def register_plugin_commands(self, commands: dict[str, str]) -> None:
        """Register commands provided by plugins, shown in /help output."""
        for name, desc in commands.items():
            self._plugin_commands[name] = {
                "desc": desc, "usage": "", "aliases": [],
                "plugin": "", "admin": False, "category": "",
            }

    def collect_plugin_commands(self, bot_id: str | None = None) -> dict[str, dict]:
        """Discover plugin commands from the plugin system (status hook).

        返回 {name: {"desc", "usage", "aliases", "plugin", "admin", "category"}} —
        plugin=插件名(分组依据), category 可由插件声明 help_category 覆盖;
        admin 读取 info.commands[].admin, 描述中的"(管理员)"也会在 /help 侧识别。
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
                category = ""
                if inst is not None:
                    category = str(getattr(inst.__class__, "help_category", "") or "")
                for cmd in cmd_list:
                    if isinstance(cmd, dict):
                        cmd_name = cmd.get("name", "")
                        if not cmd_name:
                            continue
                        discovered[cmd_name] = {
                            "desc": cmd.get("desc", ""),
                            "usage": cmd.get("usage", ""),
                            "aliases": cmd.get("aliases", []),
                            "plugin": name,
                            "admin": bool(cmd.get("admin", False)),
                            "category": category,
                        }
                    elif isinstance(cmd, str):
                        discovered[cmd] = {
                            "desc": "", "usage": "", "aliases": [],
                            "plugin": name, "admin": False, "category": category,
                        }
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

    async def _cmd_ab(self, bot_id, event, args):
        if self._persona_ab is None:
            return "人设评估服务未启用。"
        return await self._persona_ab.command(bot_id, event, args)

    @staticmethod
    def _mentioned_bot_ids(event: GroupMessageEvent, bot_manager) -> set[str]:
        """消息里被 @ 的、已绑定且在线的 bot_id 集合(无 @ 返回空集)。"""
        from mohobot.utils.cq_code import extract_at_targets

        targets = extract_at_targets(getattr(event, "message", None))
        if not targets:
            return set()
        try:
            return bot_manager.bot_ids_for_qqs(targets)
        except Exception:  # noqa: BLE001 - 判定失败按"未 @ 任何 bot"处理
            return set()

    async def _handle_unknown_command(
        self, bot_id: str, event: MessageEvent, cmd_name: str
    ) -> tuple[bool, str | None]:
        """未知指令: 多 bot 群只由随机选中的一个 bot 提醒(其余静默);
        60 分钟冷却, 同一会话内只提醒一次(冷却期内静默拦截, 不回复也不传 LLM)。
        """
        chat_type, chat_id, _ = await self._get_target_info(event)

        # 多 bot 群去重: 同一条消息经每个 bot 连接各推送一次, 各协程都会
        # 走到这里; 按 (群, message_id) 共享抽签, 只有选中 bot 回复提醒
        if isinstance(event, GroupMessageEvent) and self._ws is not None:
            bm = getattr(self._ws, "_bot_manager", None)
            if bm is not None:
                # @ 定向: 用户点名了 bot 时, 只由被 @ 的 bot 提醒(可能多只),
                # 不再抽签, 否则可能抽到未被 @ 的 bot。
                mentioned = self._mentioned_bot_ids(event, bm)
                if mentioned:
                    if bot_id not in mentioned:
                        logger.debug(
                            f"Unknown command '/{cmd_name}' 定向给被 @ 的 bot, "
                            f"{bot_id} 跳过"
                        )
                        return (True, None)
                else:
                    chosen = bm.pick_bot_for_group(
                        str(event.group_id), str(getattr(event, "message_id", "") or ""),
                    )
                    if chosen is not None and chosen != bot_id:
                        logger.debug(
                            f"Unknown command '/{cmd_name}' reminder deferred to {chosen}"
                        )
                        return (True, None)

        # 节流按会话(不含 bot): 多 bot 群里即使每次抽中的 bot 不同, 也只提醒一次
        key = (chat_type, str(chat_id))
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

    # ── 人设预设(仅全局管理员, 显式私聊目标) ──────────────────

    _PERSONA_USAGE = (
        "用法:\n"
        "/persona list\n"
        "/persona sessions <bot_id> <QQ>\n"
        "/persona set <bot_id> <QQ> <session_id> <persona_id>\n"
        "/persona get <bot_id> <QQ> <session_id>\n"
        "/persona clear <bot_id> <QQ> <session_id>"
    )

    @staticmethod
    def _persona_summary(binding: dict) -> str:
        """只展示标识和实际来源, 不把私设正文带入命令回复。"""
        effective = binding.get("effective") or {}
        name = effective.get("name") or "未命名人设"
        persona_id = effective.get("id") or "无预设"
        source = effective.get("source") or "unknown"
        source_label = {"session": "私聊会话覆盖", "bot": "bot 默认"}.get(source, source)
        return f"生效人设: {name} [{persona_id}]\n来源: {source_label} ({source})"

    async def _cmd_persona(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str:
        """管理显式 bot+QQ+sid 的私聊覆盖; 不操作上下文或情感数据。"""
        if not self._is_admin(event):
            return "该指令仅全局管理员可用。"
        if self._persona_service is None:
            return "人设服务未启用。"
        # intercept 保留其它命令的 maxsplit=2 行为; persona 再分解剩余参数。
        try:
            tokens = shlex.split(" ".join(args))
        except ValueError as e:
            return f"人设命令参数错误: {e}\n{self._PERSONA_USAGE}"
        expected = {"list": 1, "sessions": 3, "set": 5, "get": 4, "clear": 4}
        sub = tokens[0].lower() if tokens else ""
        if sub not in expected or len(tokens) != expected[sub] or any(not t for t in tokens):
            return self._PERSONA_USAGE

        service = self._persona_service
        try:
            if sub == "list":
                personas = await service.list_personas()
                if not personas:
                    return "暂无人设预设。"
                return "人设预设:\n" + "\n".join(
                    f"  {p['name']} [{p['id']}]" for p in personas
                )

            target_bot, user_id = tokens[1:3]
            if sub == "sessions":
                result = await service.list_user_sessions(target_bot, user_id)
                sessions = result.get("sessions") or []
                header = f"私聊目标: bot={target_bot}, QQ={user_id}"
                if not sessions:
                    return f"{header}\n暂无会话。"
                active = result.get("active")
                lines = [header, f"当前会话: {active or '无'}"]
                for session in sessions:
                    marker = " (当前)" if session["id"] == active else ""
                    lines.append(f"  {session.get('name', '')} [{session['id']}]{marker}")
                return "\n".join(lines)

            session_id = tokens[3]
            if sub == "set":
                await service.bind_session(target_bot, user_id, session_id, tokens[4])
                action = "已设置私聊会话人设覆盖。"
            elif sub == "clear":
                await service.clear_session_binding(target_bot, user_id, session_id)
                action = "已清除私聊会话人设覆盖，使用 bot 默认。"
            else:
                action = "私聊会话人设查询。"
            binding = await service.get_session_binding(target_bot, user_id, session_id)
            return (
                f"{action}\n私聊目标: bot={target_bot}, QQ={user_id}, session={session_id}\n"
                f"{self._persona_summary(binding)}"
            )
        except ValueError as e:
            # bot/用户/会话/预设存在性和 ID 安全校验统一交给服务。
            return f"人设命令失败: {e}"

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

    # /help 固定分类顺序(内置 → 会话 → 情感 → 封禁 → 工具 → 娱乐 → 管理)
    _HELP_CATEGORY_ORDER = ("内置", "会话", "情感", "封禁", "工具", "娱乐", "管理")
    _HELP_BUILTIN_CATEGORY = {
        "sess": "会话", "forget": "会话", "hist": "会话", "clear": "会话",
        "help": "内置", "tts": "内置", "ab": "内置",
        "persona": "管理",
    }
    _HELP_EMOTION_COMMANDS = {
        "好感度", "关系阶段", "好感排行", "负好感排行",
        "设置好感", "设置亲密", "设置态度", "重置好感", "查看好感", "情感重置",
    }
    # 插件没有声明 help_category 时的兜底分类
    _HELP_PLUGIN_CATEGORY = {
        "anysearch": "工具", "usage_stats": "工具", "chat_manager": "工具",
        "broadcast": "工具", "ctx_restore": "工具", "perception": "工具",
        "song_sync": "工具", "bilibiliparse": "工具", "status": "工具",
        "welcome": "工具",
        "divination": "娱乐", "neteasemusic": "娱乐", "wifepicker": "娱乐",
        "qzone": "娱乐", "meme_stealer": "娱乐", "six66": "娱乐",
        "praise": "娱乐",
        "relationship": "管理", "proactive_chat": "管理",
    }
    _HELP_ADMIN_TAG = re.compile(r"[（(][^）)]*管理员[^）)]*[）)]")
    _HELP_ALIAS_TAG = re.compile(r"[（(]\s*别名\s*[:：]\s*([^）)]+)[）)]")
    _HELP_USAGE_PREFIX = re.compile(r"^(?:用法|用途)\s*[:：]\s*")

    @classmethod
    def _parse_help_desc(
        cls, desc: str, *, admin: bool = False, usage: str = "",
        aliases: list[str] | None = None,
    ) -> tuple[str, str, list[str], bool]:
        """从描述中拆出别名/管理员标记, 避免把权限写进正文。"""
        desc = str(desc or "").strip()
        alias_list = [str(a).strip() for a in (aliases or []) if str(a).strip()]
        match = cls._HELP_ALIAS_TAG.search(desc)
        if match:
            alias_list.extend(
                part.strip()
                for part in re.split(r"[/,，、\s]+", match.group(1))
                if part.strip()
            )
            desc = cls._HELP_ALIAS_TAG.sub("", desc).strip()
        if cls._HELP_ADMIN_TAG.search(desc):
            admin = True
            desc = cls._HELP_ADMIN_TAG.sub("", desc).strip()
        usage = cls._HELP_USAGE_PREFIX.sub("", str(usage or "").strip()).strip()
        # 卡片每条命令只占一行说明; 多行用法压成 " / ", 避免行高计算与实际
        # 绘制不一致导致内容越界。
        desc = re.sub(r"\s*[\r\n]+\s*", " ", desc).strip()
        usage = re.sub(r"\s*[\r\n]+\s*", " / ", usage).strip()
        # 去重并保持顺序
        alias_list = list(dict.fromkeys(a.lstrip("/") for a in alias_list if a))
        return desc, usage, alias_list, admin

    def _build_help_sections(self, bot_id: str | None = None) -> list[dict]:
        """构建 /help 分组数据: 固定分类顺序 + 插件名作为组标题。"""
        sections: list[dict] = []

        builtin_by_cat: dict[str, list[dict]] = {
            cat: [] for cat in self._HELP_CATEGORY_ORDER
        }
        for name, (_, help_text) in self._commands.items():
            if name == "help":
                continue  # help 本身在标题下方说明
            desc, _, usage = help_text.partition("|")
            desc, usage, aliases, admin = self._parse_help_desc(
                desc, admin=name == "persona", usage=usage,
            )
            cat = "情感" if name in self._HELP_EMOTION_COMMANDS else (
                self._HELP_BUILTIN_CATEGORY.get(name, "内置")
            )
            builtin_by_cat[cat].append({
                "name": name, "desc": desc, "usage": usage,
                "aliases": aliases, "admin": admin,
            })
        for cat in self._HELP_CATEGORY_ORDER:
            if builtin_by_cat[cat]:
                sections.append({
                    "title": cat, "category": cat,
                    "commands": sorted(builtin_by_cat[cat], key=lambda c: c["name"]),
                })

        # 封禁管理
        ban_cmds = []
        for name in ("ban", "ban-all", "pass", "pass-all",
                     "dec-ban", "dec-ban-all", "dec-pass", "dec-pass-all",
                     "ban-reset", "banlist", "ban-enable", "ban-disable", "ban-help"):
            desc, usage, aliases, admin = self._parse_help_desc(
                self._BAN_CMD_DESC.get(name, ""),
                admin=name not in ("banlist", "ban-help"),
            )
            ban_cmds.append({
                "name": name, "desc": desc, "usage": usage,
                "aliases": aliases, "admin": admin,
            })
        sections.append({"title": "封禁", "category": "封禁", "commands": ban_cmds})

        # 插件命令: (分类, 插件名) 分组
        by_plugin: dict[tuple[str, str], list[dict]] = {}
        for name, meta in self._all_plugin_commands(bot_id).items():
            plugin = meta.get("plugin") or "其他"
            category = str(meta.get("category") or "").strip()
            if category not in self._HELP_CATEGORY_ORDER:
                category = self._HELP_PLUGIN_CATEGORY.get(plugin, "工具")
            desc, usage, aliases, admin = self._parse_help_desc(
                meta.get("desc", ""),
                admin=bool(meta.get("admin", False)),
                usage=meta.get("usage", ""),
                aliases=meta.get("aliases") or [],
            )
            by_plugin.setdefault((category, plugin), []).append({
                "name": name, "desc": desc, "usage": usage,
                "aliases": aliases, "admin": admin,
            })
        for (category, plugin) in sorted(
            by_plugin,
            key=lambda key: (self._HELP_CATEGORY_ORDER.index(key[0]), key[1]),
        ):
            sections.append({
                "title": f"{category} · {plugin}",
                "category": category,
                "commands": sorted(by_plugin[(category, plugin)], key=lambda c: c["name"]),
            })

        # 同一分类内: 内置组("会话"/"情感"等)排在插件组之前
        sections.sort(
            key=lambda sec: (
                self._HELP_CATEGORY_ORDER.index(sec["category"]),
                0 if sec["title"] == sec["category"] else 1,
                sec["title"],
            )
        )
        return sections

    def _build_help_pages(
        self,
        sections: list[dict],
        *,
        is_admin: bool,
        category: str = "",
        page_size: int = 30,
    ) -> tuple[list[list[dict]], int, int, int]:
        """按分类/权限/页大小切分帮助卡片。

        返回 (pages, total, available, hidden_admin)。普通用户会隐藏管理员
        命令并给出折叠条数; 管理员看到全量。
        """
        selected = [
            sec for sec in sections
            if not category or sec.get("category") == category
        ]
        total = sum(len(sec.get("commands") or []) for sec in selected)

        visible: list[dict] = []
        hidden = 0
        for sec in selected:
            cmds = list(sec.get("commands") or [])
            if not is_admin:
                kept = [cmd for cmd in cmds if not cmd.get("admin")]
                hidden += len(cmds) - len(kept)
                cmds = kept
            if cmds:
                visible.append({**sec, "commands": cmds})

        pages: list[list[dict]] = []
        current: list[dict] = []
        count = 0

        def flush() -> None:
            nonlocal current, count
            if current:
                pages.append(current)
                current = []
                count = 0

        for sec in visible:
            cmds = sec["commands"]
            if len(cmds) > page_size:
                flush()
                chunks = [
                    cmds[i:i + page_size] for i in range(0, len(cmds), page_size)
                ]
                for index, chunk in enumerate(chunks):
                    title = sec["title"] + ("（续）" if index else "")
                    page_sec = {**sec, "title": title, "commands": chunk}
                    if index < len(chunks) - 1:
                        pages.append([page_sec])
                    else:
                        # 最后一段留在 current, 允许后续分组继续填满本页
                        current = [page_sec]
                        count = len(chunk)
                        if count >= page_size:
                            flush()
                continue
            if count and count + len(cmds) > page_size:
                flush()
            current.append(sec)
            count += len(cmds)
            if count >= page_size:
                flush()
        flush()
        return pages, total, total - hidden, hidden

    def _help_contacts(self) -> dict:
        """状态卡插件配置里的管理员/交流群/须知, 缺省回退全局 admins。"""
        cfg = None
        if self._plugin_system:
            try:
                cfg = self._plugin_system.get_plugin_config("status")
            except Exception:
                cfg = None
        from mohobot.utils.contact_info import resolve_contacts
        return resolve_contacts(cfg, fallback_admins=sorted(self._admins))

    def _help_text(
        self, bot_id: str | None = None, category: str = "",
        is_admin: bool = True,
    ) -> str:
        """文本版帮助(图片渲染失败/无法发送时的降级)。

        与图片路径保持同一权限口径: 普通用户不列出管理员命令, 只给折叠提示。
        """
        contacts = self._help_contacts()
        lines = [
            f"可用指令: 交流群 {contacts['group']} | {contacts['url']}",
            "  /help — 显示此帮助（/help <分类> 只看某一类）",
        ]
        hidden = 0
        for sec in self._build_help_sections(bot_id):
            if category and sec.get("category") != category:
                continue
            visible = []
            for cmd in sec["commands"]:
                if not is_admin and cmd.get("admin"):
                    hidden += 1
                    continue
                visible.append(cmd)
            if not visible:
                continue
            lines.append(f"[{sec['title']}]")
            for cmd in visible:
                marker = "[管理员] " if cmd.get("admin") else ""
                line = f"  {marker}/{cmd['name']} — {cmd['desc']}"
                if cmd.get("usage"):
                    line += f"（用法：{cmd['usage']}）"
                if cmd.get("aliases"):
                    line += "（别名：" + " ".join(cmd["aliases"]) + "）"
                lines.append(line)
        if hidden:
            lines.append(f"管理员指令已折叠：另有 {hidden} 条（管理员可见）")
        return "\n".join(lines)

    async def _cmd_help(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """Show help — 渲染成 PIL 图片发送, 失败降级为文本。

        `/help <分类>` 只显示该分类; 普通用户的管理员命令折叠为一行提示,
        管理员看到全量。单页超过 30 条时自动拆图。
        """
        import base64
        import os

        from mohobot.utils.image_card import render_help_card

        category = args[0].strip() if args else ""
        if category.lower() in ("all", "全部"):
            category = ""
        if category and category not in self._HELP_CATEGORY_ORDER:
            return (
                f"未知分类：{category}。可用分类："
                + "、".join(self._HELP_CATEGORY_ORDER)
            )

        is_admin = self._is_admin(event)
        sections = self._build_help_sections(bot_id)
        pages, total, available, hidden = self._build_help_pages(
            sections,
            is_admin=is_admin,
            category=category,
        )
        if not pages:
            return f"「{category}」下暂无可用指令。" if category else "暂无可用指令。"
        if self._ws is None:
            return self._help_text(bot_id, category, is_admin)

        paths: list[str] = []
        sent_pages = 0
        try:
            contacts = self._help_contacts()
            for index, page_sections in enumerate(pages, 1):
                img_path = render_help_card(
                    page_sections,
                    page=index,
                    pages=len(pages),
                    total_commands=total,
                    available_commands=available,
                    hidden_admin=hidden,
                    contacts=contacts,
                )
                if img_path is None:
                    raise RuntimeError("help card render returned None")
                paths.append(img_path)

            for img_path in paths:
                # 仅发送帮助卡片图片(群号/网址已渲染在卡片图内), 不附带文字。
                with open(img_path, "rb") as image:
                    encoded = base64.b64encode(image.read()).decode("ascii")
                message = [
                    {"type": "image", "data": {"file": f"base64://{encoded}"}},
                ]
                if isinstance(event, GroupMessageEvent):
                    await self._ws.send_group_msg(bot_id, str(event.group_id), message)
                else:
                    await self._ws.send_private_msg(bot_id, str(event.user_id), message)
                sent_pages += 1
            return None  # 帮助卡片图片已发送
        except Exception as e:
            logger.warning(f"渲染或发送帮助图片失败, 降级为文本: {e}")
            if sent_pages:
                # 已有页发出时不再整段重发文本, 避免重复刷屏。
                return (
                    f"帮助卡片发送失败（已发送 {sent_pages}/{len(paths)} 页），"
                    "请稍后重试。"
                )
            return self._help_text(bot_id, category, is_admin)
        finally:
            for img_path in paths:
                try:
                    os.remove(img_path)
                except OSError:
                    pass

    async def _cmd_clear(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """Clear current session."""
        chat_type, chat_id, _ = await self._get_target_info(event)
        await self._ctx_mgr.clear_context(bot_id, chat_type, chat_id)
        return "当前会话已清空。"

    # ── 指令 TTS(/tts) ────────────────────────────────────────

    def _is_admin(self, event: MessageEvent) -> bool:
        return str(getattr(event, "user_id", "") or "") in self._admins

    def _bot_tts_enabled(self, bot_id: str) -> bool:
        """该 bot 的 tts_enabled 开关(per-bot config)。"""
        if self._ws is None:
            return False
        bm = getattr(self._ws, "_bot_manager", None)
        if bm is None:
            return False
        instance = bm.get(bot_id)
        return bool(instance is not None and getattr(instance.config, "tts_enabled", False))

    def _bot_tts_voice_id(self, bot_id: str) -> str | None:
        """该 bot 的 MiniMax 音色(per-bot tts_voice_id; 留空返回 None=用全局)。"""
        if self._ws is None:
            return None
        bm = getattr(self._ws, "_bot_manager", None)
        if bm is None:
            return None
        instance = bm.get(bot_id)
        if instance is None:
            return None
        return (getattr(instance.config, "tts_voice_id", "") or "").strip() or None

    async def _cmd_tts(
        self, bot_id: str, event: MessageEvent, args: list[str]
    ) -> str | None:
        """语音合成: 把指定文本转成语音发出(只发语音, 不显示文本)。

        非管理员: 限 cmd_max_chars 字 + cmd_cooldown 秒冷却(全局配置)。
        队列满丢最新时回提示; 合成失败由 TTSService 回错误文本。
        """
        if self._tts is None:
            return "语音功能未开启。"
        tts_cfg = getattr(self._tts, "cfg", None)
        if tts_cfg is None or not tts_cfg.enabled:
            return "语音功能未开启。"
        if not self._bot_tts_enabled(bot_id):
            return "本 bot 未开启语音功能。"

        text = " ".join(args).strip()
        if not text:
            return "用法: /tts <文本>"

        is_admin = self._is_admin(event)
        if not is_admin:
            max_chars = max(1, int(tts_cfg.cmd_max_chars))
            if len(text) > max_chars:
                return f"文本太长啦，最多 {max_chars} 字（管理员不限）。"
            cooldown = max(0, int(tts_cfg.cmd_cooldown))
            if cooldown > 0:
                key = (bot_id, str(getattr(event, "user_id", "") or ""))
                now = _time.time()
                last = self._tts_last_use.get(key, 0.0)
                if now - last < cooldown:
                    remain = int(cooldown - (now - last)) + 1
                    return f"语音合成冷却中，{remain} 秒后再试～"
                self._tts_last_use[key] = now

        if isinstance(event, GroupMessageEvent):
            chat_type, chat_id = "group", str(event.group_id)
        else:
            chat_type, chat_id = "private", str(event.user_id)
        from mohobot.services.tts import TTSJob
        accepted = self._tts.submit(TTSJob(
            bot_id=bot_id, chat_type=chat_type, chat_id=chat_id,
            text=text, source="command",
            voice_id=self._bot_tts_voice_id(bot_id),
        ))
        if not accepted:
            return "语音队列已满，请稍后再试～"
        return None  # 语音随后由 TTS worker 异步发出