"""推荐(名片) — 移植自 astrbot_plugin_relationship (core/contact.py)。

/推荐 <群号/@群友/@qq>: 发送 qq/group contact 段消息。
"""

from __future__ import annotations

import random
from typing import Any

from core.config import PluginConfig
from core.utils import api_call, get_ats


class ContactHandle:
    def __init__(self, config: PluginConfig):
        self.cfg = config

    async def contact(self, bot_id: str, event: Any, rest: str) -> str | None:
        """推荐 <群号/@群友/@qq>"""
        args = (rest or "").split()

        gids = [int(arg) for arg in args if arg.isdigit()]
        uids = get_ats(event)

        if not uids and not gids:
            uids, gids = await self._get_random_target(bot_id)

        if self.cfg.ws_server is None:
            return "❌ 无法发送推荐(ws 未连接)"

        if uids:
            for uid in uids:
                await self._send_contact(bot_id, event, uid=uid)
        if gids:
            for gid in gids:
                await self._send_contact(bot_id, event, gid=gid)
        return None

    async def _send_contact(self, bot_id: str, event: Any, *, uid=None, gid=None) -> None:
        """发送 qq/group contact 段(二选一)。"""
        if uid is not None:
            contact = {"type": "qq", "id": int(uid)}
        elif gid is not None:
            contact = {"type": "group", "id": int(gid)}
        else:
            return

        message = [{"type": "contact", "data": contact}]
        group_id = getattr(event, "group_id", 0)
        if group_id:
            await self.cfg.ws_server.send_group_msg(bot_id, group_id, message)
        else:
            await self.cfg.ws_server.send_private_msg(
                bot_id, getattr(event, "user_id", 0), message
            )

    async def _get_random_target(self, bot_id: str) -> tuple[list[int], list[int]]:
        """没有目标时随机补一个。"""
        if random.random() < 0.5:
            friend_list = await api_call(self.cfg.ws_server, bot_id, "get_friend_list")
            if isinstance(friend_list, list) and friend_list:
                return [friend_list[random.randrange(len(friend_list))].get("user_id")], []
        group_list = await api_call(self.cfg.ws_server, bot_id, "get_group_list")
        if isinstance(group_list, list) and group_list:
            return [], [group_list[random.randrange(len(group_list))].get("group_id")]
        return [], []
