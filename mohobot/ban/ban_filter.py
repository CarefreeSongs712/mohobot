"""封禁拦截器 — 静默拦截被禁用户消息 + 管理员封禁命令。

放拦截链最前(在命令/关键词/插件之前), 被禁用户的所有消息
(普通聊天/命令/插件触发)一律静默丢弃。
命令(仅 admins 可执行): /ban /ban-all /pass /pass-all
  /dec-ban /dec-ban-all /dec-pass /dec-pass-all /ban-reset /banlist
  /ban-enable /ban-disable /ban-help
"""

from __future__ import annotations

import time as _time
from typing import Any

from loguru import logger

from mohobot.ban.store import BanStore
from mohobot.ban.time_utils import timelast_format, time_format, timestr_to_int
from mohobot.interceptors.base import Interceptor
from mohobot.models.onebot import GroupMessageEvent, MessageEvent, PrivateMessageEvent

BAN_COMMANDS = {
    "ban", "ban-all", "pass", "pass-all",
    "dec-ban", "dec-ban-all", "dec-pass", "dec-pass-all",
    "ban-reset", "banlist", "ban-enable", "ban-disable", "ban-help",
}

HELP_TEXT = f"""🚫 封禁系统使用指南:

📒 查询:
  /banlist            — 查看当前会话与全局的封禁/解禁名单
  /ban-help           — 查看这份指南

🚫 封禁:
  /ban <@用户|QQ号> [时间] [理由]      — 封禁于当前会话(群=本群/私聊=对方)
  /ban-all <@用户|QQ号> [时间] [理由]  — 全局封禁(所有 bot 所有会话)

🎀 临时解禁(优先级: 会话解禁 > 会话封禁 > 全局解禁 > 全局封禁):
  /pass <@用户|QQ号> [时间] [理由]     — 当前会话临时解禁
  /pass-all <@用户|QQ号> [时间] [理由] — 全局临时解禁

🗑 删除记录:
  /dec-ban <@用户|QQ号> [时间]         — 删除/削减会话封禁
  /dec-ban-all <@用户|QQ号> [时间]     — 删除/削减全局封禁
  /dec-pass <@用户|QQ号> [时间]        — 删除/削减会话解禁
  /dec-pass-all <@用户|QQ号> [时间]    — 删除/削减全局解禁
  /ban-reset <@用户|QQ号>              — 清除该用户全部记录

⚙️ 功能控制:
  /ban-enable / /ban-disable — 临时启停(重启后按配置文件恢复)

⏰ 时间格式: 1d(天) 2h(小时) 30m(分钟) 10s(秒), 可组合 1d2h30m;
  默认(不带时间) = 永久。被禁用户的全部消息将被静默忽略。"""


class BanInterceptor(Interceptor):
    """封禁过滤 + 封禁命令。"""

    def __init__(
        self,
        data_dir: str = "./data",
        *,
        enabled: bool = True,
        admins: list[int] | None = None,
        store: BanStore | None = None,
        command_prefix: str = "/",
    ):
        self._enabled = enabled          # 运行时开关(ban-enable/disable 可临时改)
        self._admins = {str(a) for a in (admins or [])}
        self._command_prefix = command_prefix
        self._store = store or BanStore(data_dir=data_dir)

    # ── 运行时配置同步 ─────────────────────────────────────────

    def sync_config(self, enabled: bool | None = None, admins: list[int] | None = None) -> None:
        """web 面板改配置后热同步。"""
        if enabled is not None:
            self._enabled = enabled
        if admins is not None:
            self._admins = {str(a) for a in admins}

    def is_admin(self, user_id: int | str) -> bool:
        return str(user_id) in self._admins

    @property
    def store(self) -> BanStore:
        return self._store

    # ── 消息解析 ───────────────────────────────────────────────

    @staticmethod
    def _extract_text(event: MessageEvent) -> str:
        if isinstance(event.message, str):
            return event.message.strip()
        text = ""
        for seg in event.message:
            if isinstance(seg, dict) and seg.get("type") == "text":
                text += seg.get("data", {}).get("text", "")
        return text.strip()

    @staticmethod
    def _extract_at(event: MessageEvent) -> str | None:
        """取第一个 @ 的 QQ(@ 自己除外); 无则 None。"""
        if isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    qq = str(seg.get("data", {}).get("qq", ""))
                    if qq and qq != "all" and qq != str(event.self_id):
                        return qq
        return None

    def _session_key(self, event: MessageEvent) -> str:
        if isinstance(event, GroupMessageEvent):
            return f"group:{event.group_id}"
        return f"private:{event.user_id}"

    # ── 入口 ───────────────────────────────────────────────────

    async def intercept(
        self,
        bot_id: str,
        event: MessageEvent,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | list[dict[str, Any]] | None]:
        text = self._extract_text(event)
        if not text:
            return (False, None)

        cmd_name, rest = self._parse_command(text)

        # 1. 封禁命令(管理员执行; 查询类 banlist/ban-help 所有人可用)
        if cmd_name in BAN_COMMANDS:
            if cmd_name not in ("banlist", "ban-help") and not self.is_admin(event.user_id):
                logger.info(
                    f"非管理员 {event.user_id} 尝试 {cmd_name} (bot {bot_id})"
                )
                return (True, "❌ 你没有权限执行封禁操作。")
            reply = await self._execute_command(
                cmd_name, rest, event, bot_id
            )
            return (True, reply)

        # 2. 被禁用户 → 静默拦截
        if self._enabled:
            banned, reason = await self._store.is_banned(
                self._session_key(event), str(event.user_id)
            )
            if banned:
                logger.debug(
                    f"屏蔽被禁用户消息: user={event.user_id} "
                    f"session={self._session_key(event)} reason={reason}"
                )
                return (True, None)

        return (False, None)

    def _parse_command(self, text: str) -> tuple[str, str]:
        """按命令前缀解析命令名与剩余参数。"""
        prefix = self._command_prefix or "/"
        if not text.startswith(prefix):
            return ("", "")
        parts = text[len(prefix):].strip().split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        return (name, rest.strip())

    # ── 命令执行 ───────────────────────────────────────────────

    async def _execute_command(
        self, cmd_name: str, rest: str, event: MessageEvent, bot_id: str
    ) -> str:
        if cmd_name == "ban-help":
            return HELP_TEXT
        if cmd_name == "banlist":
            return await self._cmd_banlist(event)
        if cmd_name == "ban-enable":
            self._enabled = True
            logger.warning(f"封禁功能已临时启用 (by {event.user_id}, bot {bot_id})")
            return "✅ 封禁功能已临时启用(重启后按配置文件恢复)。"
        if cmd_name == "ban-disable":
            self._enabled = False
            logger.warning(f"封禁功能已临时禁用 (by {event.user_id}, bot {bot_id})")
            return "✅ 封禁功能已临时禁用(重启后按配置文件恢复)。"
        if cmd_name == "ban-reset":
            uid = await self._resolve_uid(rest, event)
            if not uid:
                return "❌ 请指定用户: /ban-reset <@用户|QQ号>"
            await self._store.reset_user(uid)
            logger.warning(f"[ban-reset] {uid} (by {event.user_id})")
            return f"✅ 已清除用户 {uid} 的所有封禁记录。"

        # 带目标的命令: ban/ban-all/pass/pass-all/dec-*
        uid, time_str, reason = await self._parse_target_args(rest, event)
        if not uid:
            usage = {
                "ban": "/ban <@用户|QQ号> [时间] [理由]",
                "ban-all": "/ban-all <@用户|QQ号> [时间] [理由]",
                "pass": "/pass <@用户|QQ号> [时间] [理由]",
                "pass-all": "/pass-all <@用户|QQ号> [时间] [理由]",
                "dec-ban": "/dec-ban <@用户|QQ号> [时间]",
                "dec-ban-all": "/dec-ban-all <@用户|QQ号> [时间]",
                "dec-pass": "/dec-pass <@用户|QQ号> [时间]",
                "dec-pass-all": "/dec-pass-all <@用户|QQ号> [时间]",
            }.get(cmd_name, "")
            return f"❌ 请指定用户: {usage}"

        try:
            seconds = timestr_to_int(time_str) if time_str else 0
        except ValueError:
            return f"❌ 时间格式错误: {time_str!r}(支持 1d/2h/30m/10s 组合, 0=永久)"

        session_key = self._session_key(event)
        # 私聊 1:1 场景: 会话封禁作用于"被禁用户自己的私聊会话"
        # (管理员在自己与 bot 的私聊里无法禁掉别人, 目标只能是对方的私聊)
        if isinstance(event, PrivateMessageEvent):
            session_key = f"private:{uid}"
        is_delete = cmd_name.startswith("dec-")

        if is_delete:
            target = cmd_name[len("dec-"):]
            ok, err = await self._store.delete(
                target, uid, session_key=session_key, seconds=seconds, reason=reason,
            )
            if not ok:
                return f"❌ 删除失败: {err or '记录不存在'}"
            return f"✅ 已删除 {uid} 的{self._list_label(target)}记录。"

        # upsert: ban/ban-all/pass/pass-all
        ok = await self._store.upsert(
            cmd_name, uid, session_key=session_key,
            time_val=seconds, reason=reason,
        )
        if not ok:
            return "❌ 操作失败。"
        label = {
            "ban": "封禁", "ban-all": "全局封禁",
            "pass": "临时解禁", "pass-all": "全局临时解禁",
        }[cmd_name]
        logger.warning(f"[{cmd_name}] {uid} {time_format(time_str or '0')} (by {event.user_id})")
        return (
            f"✅ 已{label} {uid}"
            f"，时限：{time_format(time_str or '0')}"
            f"{('，理由：' + reason) if reason else ''}"
        )

    @staticmethod
    def _list_label(target: str) -> str:
        return {
            "ban": "会话封禁", "ban-all": "全局封禁",
            "pass": "会话解禁", "pass-all": "全局解禁",
        }.get(target, target)

    async def _resolve_uid(self, rest: str, event: MessageEvent) -> str | None:
        """从参数解析目标 QQ: 优先 @, 其次参数里的数字。"""
        at = self._extract_at(event)
        if at:
            return at
        first = (rest or "").strip().split()[0] if (rest or "").strip() else ""
        if first.isdigit():
            return first
        return None

    async def _parse_target_args(
        self, rest: str, event: MessageEvent
    ) -> tuple[str | None, str, str | None]:
        """解析 "<@|QQ> [时间] [理由...]"。"""
        at = self._extract_at(event)
        parts = (rest or "").split()
        if at:
            # @ 时, 参数从文本剩余部分取
            if parts and parts[0].isdigit() and parts[0] != at:
                pass  # 正常: @ 在消息段里, 剩余参数是时间/理由
            return (at, parts[0] if parts and self._is_time_token(parts[0]) else "0",
                    " ".join(parts[1:]) if parts and self._is_time_token(parts[0]) else " ".join(parts))
        if not parts:
            return (None, "0", None)
        uid = parts[0]
        if not uid.isdigit():
            return (None, "0", None)
        if len(parts) >= 2 and self._is_time_token(parts[1]):
            return (uid, parts[1], " ".join(parts[2:]) or None)
        return (uid, "0", " ".join(parts[1:]) or None)

    @staticmethod
    def _is_time_token(token: str) -> bool:
        """判断是否是时间参数(数字/1d2h 格式)。"""
        if token.isdigit():
            return True
        try:
            timestr_to_int(token)
            return True
        except ValueError:
            return False

    async def _cmd_banlist(self, event: MessageEvent) -> str:
        """查看当前会话 + 全局名单。"""
        data = await self._store.get_all()
        now = _time.time()
        session = self._session_key(event)

        def fmt_records(records: list[dict]) -> str:
            lines = []
            for r in records:
                remain = 0 if r["time"] == 0 else r["time"] - int(now)
                lines.append(
                    f"  - {r['uid']} - {timelast_format(remain)}"
                    f"{(' - ' + str(r['reason'])) if str(r.get('reason', '')) not in ('', '无理由') else ''}"
                )
            return "\n".join(lines) if lines else "  (空)"

        out = []
        session_ban = (data.get("ban") or {}).get(session, [])
        session_pass = (data.get("pass") or {}).get(session, [])
        out.append(f"本会话封禁:\n{fmt_records(session_ban)}")
        out.append(f"本会话解禁:\n{fmt_records(session_pass)}")
        out.append(f"全局封禁:\n{fmt_records(data.get('ban_all') or [])}")
        out.append(f"全局解禁:\n{fmt_records(data.get('pass_all') or [])}")
        return "\n\n".join(out)
