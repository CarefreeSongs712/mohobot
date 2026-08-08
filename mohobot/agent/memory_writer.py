"""记忆写入 — 移植自 Agent-LuoTianyi (src/subconscious/memory/memory_write.py)。

把非结构化对话流转成结构化记忆: LLM 抽取 user_memory / event_memory,
向量去重后双写(向量库 + 数据库正本)。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

from loguru import logger

from mohobot.agent.domain import (
    MemoryRecord, MemoryType, MemoryUpdateCommand, MemoryVisibility,
)
from mohobot.agent.llm_module import LLMModule, parse_json_response
from mohobot.agent.vector_store import Document, VectorStore


class MemoryWriter:
    def __init__(self, config: Dict[str, Any], llm_module: LLMModule | None):
        self.config = config or {}
        self.llm = llm_module

    async def process_interaction(
        self,
        vector_store: VectorStore,
        memory_store,  # DatabaseManager
        user_id: str,
        history: str,
        current_dialogue: str = "",
        related_memories: List[str] | None = None,
        owner_character_id: str = "bot",
        commit: bool = True,
    ) -> Dict[str, Any]:
        """分析最近的交互,提取有价值的信息存入记忆库。"""
        if self.llm is None:
            return {"payload": {"user_memory": [], "event_memory": []}, "items": []}

        memory_payload = await self._extract_knowledge(
            history,
            current_dialogue=current_dialogue,
            related_memories=related_memories or [],
        )

        user_items = memory_payload.get("user_memory", [])
        event_items = memory_payload.get("event_memory", [])
        result: Dict[str, Any] = {"payload": memory_payload, "items": []}

        if user_items:
            seen_texts = await self._batch_check_user_memory_dups(
                vector_store, user_id, user_items
            )
            for content in user_items:
                text = (content or "").strip()
                if not text or text in seen_texts:
                    result["items"].append({
                        "memory_type": "user_memory", "content": text,
                        "status": "skipped_duplicate_or_empty",
                    })
                    continue
                seen_texts.add(text)
                written = await self.write_user_memory(
                    vector_store=vector_store, memory_store=memory_store,
                    user_id=user_id, content=content,
                    owner_character_id=owner_character_id, commit=commit,
                )
                result["items"].append({
                    "memory_type": "user_memory", "content": text,
                    "status": "written" if written else "skipped",
                })

        if event_items:
            today = time.strftime("%Y-%m-%d")
            seen_texts = await self._batch_check_event_memory_dups(
                vector_store, user_id, event_items, today
            )
            for content in event_items:
                text = (content or "").strip()
                normalized_text = self._normalize_text(text)
                if not text or normalized_text in seen_texts:
                    result["items"].append({
                        "memory_type": "event_memory", "content": text,
                        "status": "skipped_duplicate_or_empty", "event_date": today,
                    })
                    continue
                seen_texts.add(normalized_text)
                written = await self.write_event_memory(
                    vector_store=vector_store, memory_store=memory_store,
                    user_id=user_id, content=content,
                    owner_character_id=owner_character_id, commit=commit,
                )
                result["items"].append({
                    "memory_type": "event_memory", "content": text,
                    "status": "written" if written else "skipped", "event_date": today,
                })
        return result

    async def _extract_knowledge(
        self,
        history: str,
        current_dialogue: str,
        related_memories: List[str],
    ) -> Dict[str, Any]:
        empty = {"user_memory": [], "event_memory": []}
        try:
            response = await self.llm.generate_response(
                use_json=True,
                history=history or "无",
                current_dialogue=current_dialogue or "无",
                related_memories="；".join(related_memories) if related_memories else "无",
            )
            payload = self._parse_memory_json_response(response)
            logger.debug(f"Memory extraction payload: {payload}")
            return payload
        except Exception as e:
            logger.warning(f"Memory extraction failed: {e}")
            return empty

    def _parse_memory_json_response(self, response: str) -> Dict[str, List[str]]:
        data = parse_json_response(response)
        if data is None:
            raise ValueError("memory payload must be a JSON object")

        user_memory = data.get("user_memory", [])
        event_memory = data.get("event_memory", [])
        if not isinstance(user_memory, list) or not isinstance(event_memory, list):
            raise ValueError("user_memory/event_memory must be lists")

        def _clean(items: Any) -> List[str]:
            return [str(i or "").strip() for i in items if str(i or "").strip()]

        return {"user_memory": _clean(user_memory), "event_memory": _clean(event_memory)}

    async def write_user_memory(
        self,
        vector_store: VectorStore,
        memory_store,
        user_id: str,
        content: str,
        owner_character_id: str = "bot",
        commit: bool = True,
    ) -> bool:
        text = (content or "").strip()
        if not text:
            return False

        threshold = float(self.config.get("user_memory_dedup_threshold", 0.72))
        if await self._has_similar_user_memory(vector_store, user_id, text, threshold):
            logger.debug(f"Skip duplicate user_memory: {text[:50]}")
            return False

        today = time.strftime("%Y-%m-%d")
        doc = Document(
            content=text,
            metadata={
                "source": "memory_writer",
                "timestamp": today,
                "event_date": today,
                "memory_type": "user_memory",
                "user_id": user_id,
            },
        )
        ids = await asyncio.to_thread(vector_store.add_documents, [doc])
        cmd = MemoryUpdateCommand(type="write_user_memory", content=text, uuid=ids[0] if ids else None)
        await asyncio.to_thread(memory_store.write_memory_update, user_id, cmd.type, cmd.content, cmd.uuid)
        await asyncio.to_thread(
            memory_store.write_agent_memory_record,
            MemoryRecord(
                owner_character_id=owner_character_id,
                subject_user_id=user_id,
                memory_type=MemoryType.USER_FACT,
                visibility=MemoryVisibility.PRIVATE,
                source="chat",
                content=text,
                metadata={"legacy_update_type": cmd.type, "legacy_vector_ids": ids or []},
            ),
            embedding_ids=ids or [],
            commit=commit,
        )
        return True

    async def write_event_memory(
        self,
        vector_store: VectorStore,
        memory_store,
        user_id: str,
        content: str,
        owner_character_id: str = "bot",
        commit: bool = True,
    ) -> bool:
        text = (content or "").strip()
        if not text:
            return False

        today = time.strftime("%Y-%m-%d")
        if await self._is_same_day_duplicate_event_memory(vector_store, user_id, text, today):
            logger.debug(f"Skip same-day duplicate event_memory: {text[:50]}")
            return False

        doc = Document(
            content=text,
            metadata={
                "source": "memory_writer",
                "timestamp": today,
                "event_date": today,
                "memory_type": "event_memory",
                "user_id": user_id,
            },
        )
        ids = await asyncio.to_thread(vector_store.add_documents, [doc])
        cmd = MemoryUpdateCommand(type="write_event_memory", content=text, uuid=ids[0] if ids else None)
        await asyncio.to_thread(memory_store.write_memory_update, user_id, cmd.type, cmd.content, cmd.uuid)
        await asyncio.to_thread(
            memory_store.write_agent_memory_record,
            MemoryRecord(
                owner_character_id=owner_character_id,
                subject_user_id=user_id,
                memory_type=MemoryType.INTERACTION_EVENT,
                visibility=MemoryVisibility.PRIVATE,
                source="chat",
                content=text,
                metadata={"event_date": today, "legacy_update_type": cmd.type,
                          "legacy_vector_ids": ids or []},
            ),
            embedding_ids=ids or [],
            commit=commit,
        )
        return True

    async def _has_similar_user_memory(
        self, vector_store: VectorStore, user_id: str, content: str, threshold: float,
    ) -> bool:
        results = await vector_store.search(user_id, content, k=5)
        for doc, score in results:
            metadata = doc.get_metadata()
            if metadata.get("memory_type") != "user_memory":
                continue
            if score >= threshold:
                return True
        return False

    async def _is_same_day_duplicate_event_memory(
        self, vector_store: VectorStore, user_id: str, content: str, event_date: str,
    ) -> bool:
        results = await vector_store.search(user_id, content, k=10)
        target = self._normalize_text(content)
        for doc, _ in results:
            metadata = doc.get_metadata()
            if metadata.get("memory_type") != "event_memory":
                continue
            if str(metadata.get("event_date") or metadata.get("timestamp") or "") != event_date:
                continue
            existing = self._normalize_text(doc.get_content())
            if existing and existing == target:
                return True
        return False

    async def _batch_check_user_memory_dups(
        self, vector_store: VectorStore, user_id: str, items: List[str],
    ) -> set:
        seen: set[str] = set()
        if not items:
            return seen
        threshold = float(self.config.get("user_memory_dedup_threshold", 0.72))
        results = await vector_store.search(user_id, items[0], k=20)
        for doc, score in results:
            metadata = doc.get_metadata()
            if metadata.get("memory_type") != "user_memory":
                continue
            if score >= threshold:
                content = doc.get_content()
                if content:
                    seen.add(content.strip())
        return seen

    async def _batch_check_event_memory_dups(
        self, vector_store: VectorStore, user_id: str, items: List[str], event_date: str,
    ) -> set:
        seen: set[str] = set()
        if not items:
            return seen
        results = await vector_store.search(user_id, items[0], k=20)
        for doc, _ in results:
            metadata = doc.get_metadata()
            if metadata.get("memory_type") != "event_memory":
                continue
            doc_date = str(metadata.get("event_date") or metadata.get("timestamp") or "")
            if doc_date != event_date:
                continue
            existing = doc.get_content()
            if existing:
                seen.add(self._normalize_text(existing))
        return seen

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join((text or "").strip().split())
