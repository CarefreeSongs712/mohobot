"""话题提取 — 移植自 Agent-LuoTianyi (src/subconscious/topic_extractor.py)。

从一批未读消息中用 LLM 提取一个回复话题,同时产出记忆检索线索。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from loguru import logger

from mohobot.agent.domain import ExtractedTopic, UnreadMessage, UnreadMessageSnapshot
from mohobot.agent.llm_module import LLMModule, parse_json_response


class TopicExtractor:
    def __init__(self, config: Dict[str, Any], character_id: str, llm_module: LLMModule | None):
        self.config = config
        self.character_id = character_id
        self.llm = llm_module

    async def extract_topics(
        self,
        unread_snapshot: Optional[UnreadMessageSnapshot],
        conversation_history: str = "",
        force_complete: bool = False,
    ) -> Tuple[Optional[ExtractedTopic], List[UnreadMessage]]:
        """返回 (topic_or_None, remaining_unread)。"""
        if unread_snapshot is None or not unread_snapshot.messages:
            return None, []
        if self.llm is None:
            # 无 LLM 配置: 退化为整批一个话题
            return self._fallback_topic(unread_snapshot.messages, force_complete)

        message_lines = [
            f"[{idx}] {self._format_speaker(msg)}: {msg.content}"
            for idx, msg in enumerate(unread_snapshot.messages)
        ]
        message_content = "\n".join(message_lines)

        terms: List[str] = []
        for msg in unread_snapshot.messages:
            terms.extend(msg.terms or [])
        terms_str = ", ".join(terms) if terms else "None"

        try:
            response = await self.llm.generate_response(
                use_json=True,
                conversation_history=conversation_history or "无",
                message_content=message_content,
                terms=terms_str,
            )
        except Exception as e:
            logger.warning(f"Topic extraction LLM failed ({e}), use fallback")
            return self._fallback_topic(unread_snapshot.messages, force_complete)

        if not response:
            return None, unread_snapshot.messages

        item = parse_json_response(response)
        if item is None or not isinstance(item, dict):
            return None, unread_snapshot.messages

        source_indexes = self._resolve_source_indexes(
            item.get("source_message_ids", []), unread_snapshot.messages
        )
        if not source_indexes:
            return None, unread_snapshot.messages

        selected_messages = [unread_snapshot.messages[i] for i in source_indexes]
        topic_type = str(item.get("topic_type") or item.get("topic_types") or "chat").lower()

        if topic_type == "incomplete" and not force_complete:
            return None, unread_snapshot.messages

        topic_content = str(item.get("topic_content") or "").strip()
        if not topic_content:
            topic_content = "\n".join(m.content for m in selected_messages if m.content)

        topic = ExtractedTopic(
            topic_id=str(uuid4()),
            source_messages=selected_messages,
            topic_content=topic_content,
            memory_attempts=self._normalize_str_list(item.get("memory_attempts")),
            fact_constraints=self._normalize_str_list(item.get("fact_constraints")),
            sing_attempts=self._normalize_str_list(item.get("sing_attempts")),
            is_forced_from_incomplete=(topic_type == "incomplete" and force_complete),
        )
        max_index = max(source_indexes)
        remaining = (
            unread_snapshot.messages[max_index + 1:]
            if max_index + 1 < len(unread_snapshot.messages)
            else []
        )
        return topic, remaining

    def _fallback_topic(
        self, messages: List[UnreadMessage], force_complete: bool,
    ) -> Tuple[Optional[ExtractedTopic], List[UnreadMessage]]:
        """最小兜底: 整批消息作为一个话题。"""
        if not messages:
            return None, []
        topic = ExtractedTopic(
            topic_id=str(uuid4()),
            source_messages=messages,
            topic_content="\n".join(
                self._render_message(m) for m in messages if m.content
            ),
            memory_attempts=[],
            fact_constraints=[],
            sing_attempts=[],
            is_forced_from_incomplete=force_complete,
        )
        return topic, []

    @staticmethod
    def _format_speaker(msg: UnreadMessage) -> str:
        return msg.speaker if msg.speaker else f"用户{msg.message_id[:8]}"

    @classmethod
    def _render_message(cls, msg: UnreadMessage) -> str:
        if msg.speaker:
            return f"{msg.speaker}: {msg.content}"
        return msg.content

    def _resolve_source_indexes(self, source_ids: Any, messages: List[UnreadMessage]) -> List[int]:
        if not isinstance(source_ids, list):
            return []
        indexes: List[int] = []
        for sid in source_ids:
            idx: Optional[int] = None
            if isinstance(sid, int):
                idx = sid
            elif isinstance(sid, str):
                if sid.isdigit():
                    idx = int(sid)
                else:
                    for i, msg in enumerate(messages):
                        if msg.message_id == sid:
                            idx = i
                            break
            if idx is None or idx < 0 or idx >= len(messages) or idx in indexes:
                continue
            indexes.append(idx)
        return indexes

    @staticmethod
    def _normalize_str_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            normalized = []
            for item in value:
                if item is None:
                    continue
                s = str(item).strip()
                if s:
                    normalized.append(s)
            return normalized
        s = str(value).strip()
        return [s] if s else []
