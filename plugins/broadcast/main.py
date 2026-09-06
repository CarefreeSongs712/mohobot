"""Mohobot 广播插件 — 管理员广播消息(预览→确认→广播)。

用法(管理员):
  /广播预览 <内容>                 预览内容(发到配置的预览号私聊+群), 10 分钟内可确认
  /广播确认 [bot_id] [私聊|群聊|全部]  确认并广播(缺省全体 bot、全部类型)
  /广播确认 全体 [私聊|群聊|全部]     全体广播(与缺省 bot_id 等价)
  /广播取消                       取消待确认的广播

广播对象: 指定 bot 的全部好友/全部群; 缺省(或"全体")为全体 bot。
全体广播自动去重: 同一个群即使进了多个 bot 也只发一条(由 bot_id 最小者发送,
发送失败自动回退到该群的其他 bot); 同一好友在多个 bot 的好友列表中也只收一条。
指定单个 bot 广播时不做去重(该 bot 的全部好友/群各发一条)。
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

    # 全局指令: 群内多 bot 时只由随机选中的一个 bot 回复(框架去重)
    global_triggers = TRIGGERS

    info = {
        "commands": [
            {"name": "广播预览", "desc": "广播预览 <内容> — 预发送到预览号查看效果(管理员)"},
            {"name": "广播确认", "desc": "广播确认 [bot_id|全体] [私聊|群聊|全部] — 全体广播自动去重(管理员)"},
            {"name": "广播取消", "desc": "取消待确认的广播(管理员)"},
        ],
    }

    _ws_server = None
    _task_supervisor = None
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
    def inject_task_supervisor(cls, supervisor) -> None:
        cls._task_supervisor = supervisor

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
            target_bot = ""
            if args and args[0] not in ("全部", "全体"):
                target_bot = args[0]
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
            "bot_id": "",  # 空 = 全体 bot(自动去重)
            "scope": "全部",
            "expire": time.time() + timeout,
            "by": (bot_id, *self._chat_of(event)),
        }
        return (
            True,
            "✅ 已发送预览到预览号(私聊 + 群), 请检查效果。\n"
            "确认广播: /广播确认 [bot_id|全体] [私聊|群聊|全部]\n"
            "(全体广播自动去重: 同一群/同一好友只收一条)\n"
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
        task_coro = self._run_broadcast(
            bot_id=bot_id, event=event,
            content=content, target_bot=target_bot, scope=scope,
        )
        if self._task_supervisor is not None:
            self._task_supervisor.create_task(
                task_coro, name=f"broadcast:{bot_id}", owner="plugins"
            )
        else:
            asyncio.create_task(task_coro)
        return (True, "🚀 已开始广播, 完成后会汇报统计。")

    # ── 广播执行 ────────────────────────────────────────────

    @staticmethod
    def _build_owner_map(
        per_bot_targets: dict[str, list[dict]],
        id_key: str,
    ) -> tuple[dict[str, list[str]], dict[str, Any]]:
        """把各 bot 的好友/群列表合并为 目标ID → 按 bot_id 升序的负责列表。

        返回 (owner_map, raw_targets): owner_map 的值首元素即首选 bot;
        raw_targets 保留目标的原始字段(发送时用)。
        """
        owner_map: dict[str, list[str]] = {}
        raw_targets: dict[str, Any] = {}
        for bid in sorted(per_bot_targets):
            for item in per_bot_targets[bid]:
                key = str(item.get(id_key))
                owner_map.setdefault(key, []).append(bid)
                raw_targets.setdefault(key, item)
        return owner_map, raw_targets

    async def _send_once(
        self, owner_bots: list[str], send: Any, target_id: Any,
        content: str,
    ) -> bool:
        """按 bot 优先级发送, 首选失败自动回退到下一个 bot。"""
        for bid in owner_bots:
            try:
                await send(bid, target_id, content)
                return True
            except Exception as e:
                logger.warning(f"广播发送失败({bid} → {target_id}): {e}")
        return False

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
                bots = sorted(b.bot_id for b in bm.all_bots)
            else:
                bots = [bot_id]
            all_mode = not target_bot and len(bots) > 1  # 全体广播: 跨 bot 去重

            interval = float(self._cfg("send_interval", 0.5))
            sent = failed = dedup_skipped = 0
            group_sent = private_sent = 0

            async def _do_scope(kind: str) -> None:
                nonlocal sent, failed, dedup_skipped, group_sent, private_sent
                action = "get_friend_list" if kind == "private" else "get_group_list"
                id_key = "user_id" if kind == "private" else "group_id"
                # 1) 采集各 bot 的目标列表
                per_bot: dict[str, list[dict]] = {}
                for bid in bots:
                    try:
                        resp = await ws.send_to_bot(
                            bid, action, {}, wait_response=True, timeout=10.0,
                        )
                        per_bot[bid] = (resp or {}).get("data") or []
                    except Exception as e:
                        logger.warning(f"广播-{action}失败({bid}): {e}")
                        per_bot[bid] = []
                if all_mode:
                    owner_map, raw_targets = self._build_owner_map(per_bot, id_key)
                    dedup_skipped += sum(
                        len(owner_list) - 1 for owner_list in owner_map.values()
                        if len(owner_list) > 1
                    )
                else:
                    # 单 bot 模式: 不去重, 全发
                    owner_map, raw_targets = {}, {}
                    for bid, items in per_bot.items():
                        for item in items:
                            owner_map[str(item.get(id_key))] = [bid]
                            raw_targets[str(item.get(id_key))] = item
                # 2) 逐目标发送(首选 bot 失败自动回退)
                send = ws.send_private_msg if kind == "private" else ws.send_group_msg
                for key, owner_bots in owner_map.items():
                    target_id = raw_targets[key].get(id_key)
                    if await self._send_once(owner_bots, send, target_id, content):
                        sent += 1
                        if kind == "private":
                            private_sent += 1
                        else:
                            group_sent += 1
                    else:
                        failed += 1
                    await asyncio.sleep(interval)

            if scope in ("私聊", "全部"):
                await _do_scope("private")
            if scope in ("群聊", "全部"):
                await _do_scope("group")

            range_text = f"bot={target_bot}" if target_bot else f"全体({len(bots)} bot, 去重)"
            summary = (
                f"📢 广播完成: 群聊 {group_sent} 条, 私聊 {private_sent} 条, "
                f"失败 {failed} 条"
                + (f", 去重跳过 {dedup_skipped} 条重复" if all_mode and dedup_skipped else "")
                + f"\n范围: {range_text} 类型={scope}"
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
