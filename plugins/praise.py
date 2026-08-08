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

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        """Inject the WS server for API calls (called from main.py)."""
        cls._ws_server = ws_server

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

        # Send 20 likes: two calls of 10 (OneBot limit: max 10 per friend per day)
        try:
            for _ in range(2):
                await ws_server.send_to_bot(bot_id, "send_like", {
                    "user_id": int(user_id),
                    "times": 10,
                })
                await asyncio.sleep(0.5)
            return (True, "✅ 已给你点了 20 个赞,去名片看看吧~")
        except Exception as e:
            logger.error(f"send_like failed: {e}")
            return (True, f"❌ 点赞失败: {e}")