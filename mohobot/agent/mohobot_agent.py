"""意识层 Agent — 移植自 Agent-LuoTianyi (src/agent/luotianyi_agent.py)。

持有 MainChat(风格化回复后端),向流水线暴露与潜意识(mind)的桥接方法。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from mohobot.agent.character_mind import CharacterSubconscious
from mohobot.agent.domain import (
    ChatInputEvent, ExtractedTopic, OneResponseLine, TopicAttentionPlan,
    UnreadMessageSnapshot,
)
from mohobot.agent.main_chat import MainChat


class MohobotAgent:
    """一个 bot 的意识层 Agent。"""

    def __init__(
        self,
        config: Dict[str, Any],
        database_manager,
        main_chat: MainChat,
        mind: CharacterSubconscious | None = None,
        character_id: str = "bot",
        character_name: str = "",
    ):
        self.config = config
        self.logger = logger.bind(agent=character_id)
        self.database_manager = database_manager
        self.character_id = character_id
        self.character_name = character_name or character_id
        self.main_chat = main_chat
        self.mind = mind

    def ensure_dependencies(self) -> None:
        required = {
            "database_manager": self.database_manager,
            "main_chat": self.main_chat,
            "mind": self.mind,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise RuntimeError(f"MohobotAgent dependencies missing: {missing}")

    # ── 与潜意识(mind)的桥接 ─────────────────────────────────

    async def extract_topic(
        self,
        user_id: str,
        unread_snapshot: UnreadMessageSnapshot,
        force_complete: bool = False,
        conversation_history: str | None = None,
    ) -> Tuple[Optional[ExtractedTopic], List[Any]]:
        if self.mind is None:
            return None, []
        return await self.mind.extract_topics(
            user_id=user_id,
            unread_snapshot=unread_snapshot,
            force_complete=force_complete,
            conversation_history=conversation_history,
        )

    async def plan_topic_turn(
        self,
        user_id: str,
        topic: ExtractedTopic,
        conversation_history: str,
        external_context: Optional[str] = None,
    ) -> TopicAttentionPlan:
        if self.mind is None:
            raise RuntimeError("mind is None")
        return await self.mind.plan_topic_turn(
            user_id=user_id,
            topic=topic,
            conversation_history=conversation_history,
            external_context=external_context,
        )

    async def realize_topic_plan(
        self,
        user_id: str,
        plan: TopicAttentionPlan,
        song_annotation: str = "",
    ) -> List[OneResponseLine]:
        """把注意力计划实现为回复行。"""
        user_context = await self._load_user_expression_context(user_id)
        return await self.main_chat.generate_response(
            reply_topic=plan.topic_content,
            user_nickname=user_context["nickname"],
            user_description=user_context["description"],
            preference_context=user_context["preference_context"],
            conversation_history=plan.conversation_history,
            fact_hits=plan.fact_hits,
            memory_hits=plan.memory_hits,
            song_annotation=song_annotation,
        )

    async def generate_topic_reply(
        self,
        user_id: str,
        topic_content: str,
        memory_hits: Optional[List[str]] = None,
        fact_hits: Optional[List[str]] = None,
        conversation_history: Optional[str] = None,
    ) -> List[OneResponseLine]:
        user_context = await self._load_user_expression_context(user_id)
        return await self.main_chat.generate_response(
            reply_topic=topic_content,
            user_nickname=user_context["nickname"],
            user_description=user_context["description"],
            preference_context=user_context["preference_context"],
            conversation_history=conversation_history or "",
            fact_hits=fact_hits or [],
            memory_hits=memory_hits or [],
        )

    async def write_topic_memories(
        self,
        user_id: str,
        current_dialogue: str,
        related_memories: Optional[List[str]] = None,
        conversation_history: Optional[str] = None,
    ) -> dict:
        if self.mind is None:
            return {}
        return await self.mind.write_topic_memories(
            user_id=user_id,
            current_dialogue=current_dialogue,
            related_memories=related_memories,
            conversation_history=conversation_history,
        )

    async def update_user_profile_by_context(
        self, user_id: str, context: dict[str, Any],
    ) -> str | None:
        if self.mind is None:
            return None
        return await self.mind.update_user_profile_by_context(
            user_id=user_id, context=context,
        )

    # ── 用户上下文 ───────────────────────────────────────────

    async def _load_user_expression_context(self, user_id: str) -> dict[str, str]:
        """读取用户画像/偏好(SQLite 共享库,丢线程池防阻塞)。"""
        import asyncio
        description = await asyncio.to_thread(
            self.database_manager.get_user_description, user_id,
        ) or ""
        preferences = await asyncio.to_thread(
            self.database_manager.get_user_preferences, user_id,
        ) or {}
        return {
            "nickname": "你",
            "description": description,
            "preference_context": self._build_preference_context(preferences),
        }

    def _build_preference_context(self, preferences: Any) -> str:
        if not preferences:
            return ""
        try:
            prefs = json.loads(preferences) if isinstance(preferences, str) else preferences
            while isinstance(prefs, str):
                prefs = json.loads(prefs)
            if not isinstance(prefs, dict):
                return ""
            parts = []
            if prefs.get("relationship"):
                parts.append(f"用户希望你是他的：{prefs['relationship']}")
            if prefs.get("speaking_style"):
                parts.append(f"用户希望你的表达风格偏向：{prefs['speaking_style']}")
            if prefs.get("personality_traits"):
                traits = "、".join(prefs["personality_traits"])
                parts.append(f"用户希望你的性格特点：{traits}")
            if prefs.get("custom_context"):
                custom = prefs["custom_context"].replace("我", "用户")
                parts.append(f"用户补充的上下文：{custom}")
            if parts:
                return "用户偏好设置：" + "；".join(parts)
        except Exception as e:
            self.logger.debug(f"Skip invalid preferences context: {e}")
        return ""
