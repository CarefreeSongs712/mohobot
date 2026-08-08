"""角色反射 — 移植自 Agent-LuoTianyi (src/agent/reflex/character_reflex.py)。

低延迟反射通道,在进入话题流水线之前拦截特定事件(如戳一戳),
直接返回预设回复,不消耗 LLM 话题规划。
简化: 无音频资源,使用文本回复。
"""

from __future__ import annotations

import random
from typing import Awaitable, Callable, Optional

from loguru import logger

from mohobot.agent.domain import ChatInputEvent, ChatInputEventType

DEFAULT_TOUCH_REPLIES = [
    "呜哇！吓我一跳～",
    "嘿嘿，别戳啦～",
    "唔……怎么了？",
    "戳我干嘛呀～",
]


class CharacterReflex:
    """角色级反射处理入口。"""

    def __init__(
        self,
        config: Optional[dict] = None,
        character_id: str = "bot",
        touch_replies: Optional[list[str]] = None,
    ):
        self.config = config or {}
        self.character_id = character_id
        self.touch_replies = touch_replies or DEFAULT_TOUCH_REPLIES

    async def try_handle(
        self,
        event: ChatInputEvent,
        send_reply_callback: Callable[[str], Awaitable[None]],
    ) -> bool:
        """尝试处理低延迟反射事件,成功时返回 True。"""
        if event.event_type == ChatInputEventType.USER_TOUCH:
            reply = random.choice(self.touch_replies)
            try:
                await send_reply_callback(reply)
            except Exception as e:
                logger.error(f"Reflex send reply failed: {e}")
                return False
            return True
        return False
