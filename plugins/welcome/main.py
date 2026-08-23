"""Mohobot 欢迎插件 — 新加好友 / 新入群时自动发送欢迎消息。

触发:
- friend_add 通知(新加好友) → 私聊发送欢迎(随机延迟 3~5s)
- group_increase 通知(被拉入新群) → 群聊发送欢迎(随机延迟 3~5s;
  发送前检查群仍存在, 已自动退群的群不发送)

配置(WebUI 插件页可编辑): welcome_friend_enabled/welcome_friend_msg/
welcome_group_enabled/welcome_group_msg — 开关关闭或模板为空则不发送。
模板占位符「xxx（bot昵称）」(兼容旧「【此处替换为 bot 的昵称】」)
发送时自动替换为该 bot 昵称(取不到回退 bot_id)。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from loguru import logger

_NEW_FRIEND_MSG = (
    "这里是xxx（bot昵称）！\n"
    "这是一个QQ机器人，具有AI对话等多种功能~\n"
    "介绍+使用须知（浏览器打开）：http://103.236.70.18:712/ 或 https://7121099.xyz/ \n"
    "交流群：398870315"
)


class Plugin:
    """欢迎消息: 新好友/新入群自动发送(可配置开关与文案)。"""

    info = {
        "description": "新加好友/新入群时自动发送欢迎消息(可配置开关与文案)",
    }

    _ws_server = None
    _data_dir = "./data"

    _DEFAULTS = {
        "welcome_friend_enabled": True,
        "welcome_friend_msg": _NEW_FRIEND_MSG,
        "welcome_group_enabled": True,
        "welcome_group_msg": _NEW_FRIEND_MSG,
        "delay_min": 3,
        "delay_max": 5,
    }

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    def _cfg(self, key: str, default):
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None else default

    async def on_notice(self, bot_id: str, event: Any, raw: dict) -> None:
        if not raw or not isinstance(raw, dict):
            return
        notice_type = raw.get("notice_type", "")
        if notice_type == "friend_add":
            await self._send_welcome(
                bot_id, target_type="friend",
                target_id=str(raw.get("user_id", "")),
            )
        elif notice_type == "group_increase":
            await self._send_welcome(
                bot_id, target_type="group",
                target_id=str(raw.get("group_id", "")),
            )

    # ── 发送 ─────────────────────────────────────────────────

    async def _send_welcome(
        self, bot_id: str, *, target_type: str, target_id: str,
    ) -> None:
        ws = self._ws_server
        if ws is None or not target_id:
            return
        if target_type == "friend":
            enabled_key, msg_key = "welcome_friend_enabled", "welcome_friend_msg"
        else:
            enabled_key, msg_key = "welcome_group_enabled", "welcome_group_msg"
        if not self._cfg(enabled_key, True):
            return
        template = str(self._cfg(msg_key, "") or "").strip()
        if not template:
            return

        nickname = await self._get_bot_nickname(bot_id)
        text = template.replace("【此处替换为 bot 的昵称】", nickname)
        text = text.replace("xxx（bot昵称）", nickname)

        delay_min = max(0, int(self._cfg("delay_min", 3)))
        delay_max = max(delay_min, int(self._cfg("delay_max", 5)))
        await asyncio.sleep(random.uniform(delay_min, delay_max))

        try:
            if target_type == "group":
                # 发送前确认群仍存在(黑名单/小群/互斥群自动退群的场景不发)
                try:
                    resp = await ws.send_to_bot(
                        bot_id, "get_group_info",
                        {"group_id": int(target_id)},
                        wait_response=True, timeout=8.0,
                    )
                    if resp is None or resp.get("status") != "ok" or resp.get("retcode") != 0:
                        logger.debug(f"群 {target_id} 已不存在, 跳过欢迎")
                        return
                except Exception:
                    return
                await ws.send_group_msg(bot_id, int(target_id), text)
            else:
                await ws.send_private_msg(bot_id, int(target_id), text)
            logger.info(f"欢迎消息已发送: {target_type}={target_id} (bot {bot_id})")
        except Exception as e:
            logger.warning(f"欢迎消息发送失败({target_type}={target_id}): {e}")

    async def _get_bot_nickname(self, bot_id: str) -> str:
        """取 bot 配置的昵称; 无 bot_manager 引用时回退 bot_id。"""
        ws = self._ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        if bm is None:
            return bot_id
        try:
            instance = bm.get(bot_id)
            if instance is not None and getattr(instance, "nickname", ""):
                return instance.nickname
        except Exception:
            pass
        return bot_id
