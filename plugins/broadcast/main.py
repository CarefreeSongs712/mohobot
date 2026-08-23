"""Mohobot 广播插件 — 管理员广播消息(预览→确认→广播)。

用法(管理员):
  /广播预览 <内容>                 预览内容(发到配置的预览号私聊+群), 10 分钟内可确认
  /广播确认 [bot_id] [私聊|群聊|全部]  确认并广播(缺省全部 bot、全部类型)
  /广播取消                       取消待确认的广播

广播对象: 指定 bot 的全部好友/全部群(或全部 bot)。
预览确认后异步后台广播, 完成后向发起会话汇报成功/失败统计。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

TRIGGERS = {"/广播预览", "/广播确认", "/广播取消"}

_DEFAULTS = {
    "preview_qq": 3831097597,
    "preview_group": 398870315,
    "confirm_timeout_sec": 600,
    "max_content_len": 2000,
    "send_interval": 0.5,  # 每条间隔(秒), 防风控
}


class Plugin:
    """广播: /广播预览 <内容> → /广播确认 [bot] [类型] → 全量发送。"""

    # 全局指令: 群内多 bot 时只由 bot_id 最小者回复(框架去重)
    global_triggers = TRIGGERS

    info = {
        "commands": [
            {"name": "广播预览", "desc": "广播预览 <内容> — 预发送到预览号查看效果(管理员)"},
            {"name": "广播确认", "desc": "广播确认 [bot_id] [私聊|群聊|全部] — 确认并广播(管理员)"},
            {"name": "广播取消", "desc": "取消待确认的广播(管理员)"},
        ],
    }

    _ws_server = None
    _admin_ids: list[str] = []

    _DEFAULTS_CFG = dict(_DEFAULTS)

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS_CFG)
        # 待确认广播: {"content", "bot_id", "scope", "expire", "by"}
        self._pending: dict | None = None
        # 运行中的广播任务计数(防并发)
        self._running = False

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_admin_ids(cls, admin_ids: list[int] | None) -> None:
        cls._admin_ids = [str(a) for a in (admin_ids or [])]

    def _cfg(self, key: str, default):
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None else default

    def _is_admin(self, user_id) -> bool:
        return str(user_id) in set(self._admin_ids)

    @staticmethod
    def _extract_text(event) -> str:
        if isinstance(event.message, str):
            return event.message.strip()
        text = ""
        if isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
        return text.strip()

    @staticmethod
    def _chat_of(event) -> tuple[str, str]:
        from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent
        if isinstance(event, GroupMessageEvent):
            return ("group", str(event.group_id))
        return ("private", str(event.user_id))

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        text = self._extract_text(event)
        if not text or not text.startswith("/广播"):
            return (False, None)
        if not self._is_admin(event.user_id):
            return (True, "❌ 你没有权限执行广播操作。")

        if text.startswith("/广播预览 "):
            content = text[len("/广播预览 "):].strip()
            return await self._preview(bot_id, event, content)
        if text == "/广播预览":
            return (True, "用法: /广播预览 <内容>")
        if text.startswith("/广播确认"):
            args = text[len("/广播确认"):].strip().split()
            target_bot = args[0] if args and args[0] != "全部" else ""
            scope = "全部"
            if len(args) >= 2 and args[1] in ("私聊", "群聊", "全部"):
                scope = args[1]
            elif len(args) == 1 and args[0] in ("私聊", "群聊", "全部"):
                scope = args[0]
                target_bot = ""
            return await self._confirm(bot_id, event, target_bot, scope)
        if text == "/广播取消":
            if self._pending is None:
                return (True, "当前没有待确认的广播。")
            self._pending = None
            return (True, "已取消待确认的广播。")
        return (False, None)

    # ── 预览 ────────────────────────────────────────────────

    async def _preview(
        self, bot_id: str, event: Any, content: str,
    ) -> tuple[bool, str | None]:
        ws = self._ws_server
        if ws is None:
            return (True, "广播服务未配置。")
        if not content:
            return (True, "广播内容不能为空。")
        max_len = int(self._cfg("max_content_len", 2000))
        if len(content) > max_len:
            return (True, f"❌ 内容过长({len(content)} 字), 上限 {max_len} 字。")

        preview_qq = int(self._cfg("preview_qq", 3831097597))
        preview_group = int(self._cfg("preview_group", 398870315))
        try:
            await ws.send_private_msg(bot_id, preview_qq, f"【广播预览】\n{content}")
            await ws.send_group_msg(bot_id, preview_group, f"【广播预览】\n{content}")
        except Exception as e:
            logger.warning(f"广播预览发送失败: {e}")
            return (True, f"❌ 预览发送失败: {e}")

        timeout = int(self._cfg("confirm_timeout_sec", 600))
        self._pending = {
            "content": content,
            "bot_id": "",  # 空 = 全部 bot
            "scope": "全部",
            "expire": time.time() + timeout,
            "by": (bot_id, *self._chat_of(event)),
        }
        return (
            True,
            "✅ 已发送预览到预览号(私聊 + 群), 请检查效果。\n"
            f"确认广播: /广播确认 [bot_id] [私聊|群聊|全部]\n"
            f"取消: /广播取消 ({timeout // 60} 分钟内有效)",
        )

    # ── 确认 ────────────────────────────────────────────────

    async def _confirm(
        self, bot_id: str, event: Any, target_bot: str, scope: str,
    ) -> tuple[bool, str | None]:
        ws = self._ws_server
        if ws is None:
            return (True, "广播服务未配置。")
        pending = self._pending
        if pending is None:
            return (True, "当前没有待确认的广播, 请先 /广播预览 <内容>。")
        if time.time() > pending["expire"]:
            self._pending = None
            return (True, "⏰ 待确认的广播已过期, 请重新 /广播预览。")

        content = pending["content"]
        self._pending = None
        if self._running:
            return (True, "⚠️ 已有广播任务在进行中, 请稍后再试。")
        self._running = True

        # 后台执行广播, 完成后汇报到发起会话
        asyncio.create_task(self._run_broadcast(
            bot_id=bot_id, event=event,
            content=content, target_bot=target_bot, scope=scope,
        ))
        return (True, "🚀 已开始广播, 完成后会汇报统计。")

    # ── 广播执行 ────────────────────────────────────────────

    async def _run_broadcast(
        self, *, bot_id: str, event: Any,
        content: str, target_bot: str, scope: str,
    ) -> None:
        ws = self._ws_server
        try:
            bm = getattr(ws, "_bot_manager", None) if ws is not None else None
            if target_bot:
                bots = [target_bot]
            elif bm is not None:
                bots = [b.bot_id for b in bm.all_bots]
            else:
                bots = [bot_id]

            interval = float(self._cfg("send_interval", 0.5))
            sent = 0
            failed = 0
            for bid in bots:
                if scope in ("私聊", "全部"):
                    try:
                        resp = await ws.send_to_bot(
                            bid, "get_friend_list", {}, wait_response=True, timeout=10.0,
                        )
                        friends = (resp or {}).get("data") or []
                        for f in friends:
                            try:
                                await ws.send_private_msg(bid, f["user_id"], content)
                                sent += 1
                                await asyncio.sleep(interval)
                            except Exception:
                                failed += 1
                    except Exception as e:
                        logger.warning(f"广播-好友列表失败({bid}): {e}")
                if scope in ("群聊", "全部"):
                    try:
                        resp = await ws.send_to_bot(
                            bid, "get_group_list", {}, wait_response=True, timeout=10.0,
                        )
                        groups = (resp or {}).get("data") or []
                        for g in groups:
                            try:
                                await ws.send_group_msg(bid, g["group_id"], content)
                                sent += 1
                                await asyncio.sleep(interval)
                            except Exception:
                                failed += 1
                    except Exception as e:
                        logger.warning(f"广播-群列表失败({bid}): {e}")

            summary = (
                f"📢 广播完成: 成功 {sent} 条, 失败 {failed} 条\n"
                f"范围: bot={target_bot or '全部'} 类型={scope}"
            )
            await self._send_to(bot_id, event=event, text=summary)
        except Exception as e:
            logger.error(f"广播执行异常: {e}")
            try:
                await self._send_to(bot_id, event=event, text=f"❌ 广播执行异常: {e}")
            except Exception:
                pass
        finally:
            self._running = False

    async def _send_to(self, bot_id: str, event: Any, text: str) -> None:
        ws = self._ws_server
        if ws is None:
            return
        chat_type, chat_id = self._chat_of(event)
        if chat_type == "group":
            await ws.send_group_msg(bot_id, chat_id, text)
        else:
            await ws.send_private_msg(bot_id, chat_id, text)
