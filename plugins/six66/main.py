"""Mohobot 数字梗插件 — 群聊发送 6 / 66 / 666 时概率触发梗回复。

规则(按用户确认):
- 触发词: 文本去除首尾空白后完全匹配 "6" / "66" / "666"(无需 @bot, 仅群聊)
- 触发概率: 20%(配置 probability, 可调); 未命中时消息继续走正常流程
- 多 bot 群内: 只由 bot_id 最小者回复(与全局指令去重一致)
- 私聊不触发
"""

from __future__ import annotations

import random
from typing import Any

from loguru import logger

# 触发词 → 回复
REPLIES = {
    "6": "6=5+2+0+1+3-1-4\n你是不是在喜欢我呀(偷笑)",
    "66": "66=52+0×13+14\n你是不是在喜欢我呀(偷笑)",
    "666": "666=(52-0!)×13-1+4\n你是不是在喜欢我呀(偷笑)",
}

_DEFAULTS = {
    "probability": 20,  # 触发概率(百分比)
}


class Plugin:
    """数字梗: 群聊 6/66/666 概率触发梗回复(无需 @, 多 bot 只最小 bot 回复)。"""

    info = {
        "description": "群聊发送 6/66/666 时按概率触发梗回复(无需 @bot, 多 bot 只一个回复)",
    }

    _ws_server = None

    def __init__(self):
        self.plugin_config: dict = dict(_DEFAULTS)

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    def _cfg(self, key: str, default):
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None else default

    def _is_min_bot_in_group(self, bot_id: str, group_id) -> bool:
        """群内最小 bot 判断(无 bot_manager 引用时视为单 bot, 不去重)。"""
        ws = self._ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        if bm is None:
            return True
        min_bot = bm.min_bot_for_group(str(group_id))
        return min_bot is None or min_bot == bot_id

    async def on_message_observed(
        self, bot_id: str, event: Any, raw: dict,
    ) -> tuple[bool, str | None]:
        """观察钩子(群聊无需 @): 精确匹配 6/66/666 + 概率 + 最小 bot 去重。"""
        from mohobot.models.onebot import GroupMessageEvent

        if not isinstance(event, GroupMessageEvent):
            return (False, None)  # 仅群聊

        text = ""
        if isinstance(event.message, str):
            text = event.message.strip()
        elif isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
            text = text.strip()

        if text not in REPLIES:
            return (False, None)

        # 多 bot 群内: 只由最小 bot 回复
        if not self._is_min_bot_in_group(bot_id, event.group_id):
            logger.debug(f"数字梗 {text!r} 由群内最小 bot 回复, {bot_id} 跳过")
            return (False, None)

        # 概率触发(未命中 → 不消费, 消息继续正常流程)
        prob = max(0, min(100, int(self._cfg("probability", 20))))
        if random.random() * 100 >= prob:
            return (False, None)

        return (True, REPLIES[text])
