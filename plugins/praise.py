"""Mohobot 点赞插件 — 发送"赞我"/"/赞我"/"zanwo"/"/zanwo"时给用户点 20 个赞。

OneBot v11 API: send_like (user_id, times) — 每个好友每天最多 10 次,
因此 20 个赞分两次调用,每次 10。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

PRAISE_TRIGGERS = {"赞我", "/赞我", "zanwo", "/zanwo"}


class Plugin:
    """Responds to praise requests by sending 20 likes via send_like API."""

    info = {
        "commands": [
            {"name": "赞我", "desc": "给自己点 20 个赞"},
        ],
    }

    # WS server injected by main.py via inject_ws_server() classmethod
    _ws_server = None

    # 当日点赞上限缓存: f"{bot_id}:{user_id}" -> "YYYY-MM-DD"
    # 同一好友当天达到上限后不再调用 API(QQ 每日最多 10 赞/好友)
    _like_limit_cache: dict[str, str] = {}

    # 判定"已达上限"的错误关键词(平台返回)
    _LIMIT_HINTS = ("频繁", "上限", "过快", "too many", "频繁操作", "操作过快")

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        """Inject the WS server for API calls (called from main.py)."""
        cls._ws_server = ws_server

    @staticmethod
    def _mark_like_limit(cache_key: str, today: str) -> None:
        """记录当日已达上限(跨天自动清理旧条目)。"""
        for k in [k for k, v in Plugin._like_limit_cache.items() if v != today]:
            del Plugin._like_limit_cache[k]
        Plugin._like_limit_cache[cache_key] = today

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Check for praise trigger and send likes."""
        # Extract plain text
        text = ""
        if isinstance(event.message, str):
            text = event.message.strip()
        elif isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
            text = text.strip()

        if text not in PRAISE_TRIGGERS:
            return (False, None)

        user_id = event.user_id
        logger.info(f"Praise request from {user_id} (bot {bot_id})")

        ws_server = self._ws_server
        if ws_server is None:
            return (True, "点赞服务未配置,无法发送点赞。")

        from mohobot.utils.time_utils import format_utc8
        today = format_utc8("%Y-%m-%d")
        cache_key = f"{bot_id}:{user_id}"
        # 当日已达上限 → 直接提示, 不再调用 API
        if self._like_limit_cache.get(cache_key) == today:
            logger.debug(f"Praise daily limit cached: {user_id}")
            return (True, "😴 今天已经给你点过很多赞啦,QQ 每日点赞有上限,明天再来吧~")

        # Send 20 likes: two calls of 10 (OneBot limit: max 10 per friend per day).
        # Wait for each response and report the REAL result — do not claim
        # success when the platform rejects it (e.g. daily like limit reached).
        errors: list[str] = []
        ok_count = 0
        try:
            for i in range(2):
                resp = await ws_server.send_to_bot(
                    bot_id, "send_like",
                    {"user_id": int(user_id), "times": 10},
                    wait_response=True,
                    timeout=5.0,
                )
                if resp is None:
                    errors.append(f"第 {i + 1} 次无响应(超时)")
                elif resp.get("status") != "ok" or resp.get("retcode") != 0:
                    wording = resp.get("wording") or resp.get("message") or "未知错误"
                    errors.append(f"第 {i + 1} 次失败: {wording}")
                    # First failure already tells the story (e.g. daily limit) —
                    # no point sending the second batch.
                    break
                else:
                    ok_count += 1
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"send_like failed: {e}")
            return (True, f"❌ 点赞失败: {e}")

        if errors:
            detail = "；".join(errors)
            logger.warning(f"Praise failed for {user_id}: {detail}")
            # 平台拒绝(如已达上限) → 缓存当天, 之后不再重复调用 API
            if any(hint in detail for hint in self._LIMIT_HINTS):
                self._mark_like_limit(cache_key, today)
            return (True, f"❌ 点赞失败: {detail}")
        return (True, f"✅ 已给你点了 {ok_count * 10} 个赞,去名片看看吧~")