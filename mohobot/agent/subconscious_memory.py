"""潜意识记忆门面 — 移植自 Agent-LuoTianyi (src/subconscious/memory/facade.py)。

只接收业务参数;底层数据库连接由 DatabaseManager 统一管理。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

from loguru import logger

from mohobot.agent.domain import MemoryContext, MemoryHit, MemoryRecord
from mohobot.agent.llm_module import LLMModule
from mohobot.agent.memory_writer import MemoryWriter
from mohobot.agent.user_profile_updater import UserProfileUpdater
from mohobot.agent.vector_store import VectorStore


class SubconsciousMemory:
    """角色潜意识的记忆入口: 召回、写入、用户画像更新。"""

    def __init__(
        self,
        config: Dict[str, Any],
        llm_modules: Dict[str, Any],
        *,
        database_manager,
        vector_store: VectorStore,
        owner_character_id: str = "bot",
    ):
        self.config = config
        self.owner_character_id = owner_character_id
        self.database_manager = database_manager
        self.vector_store = vector_store

        memory_writer_cfg = config.get("memory_writer", {}) if isinstance(config, dict) else {}
        profile_cfg = config.get("user_profile", {}) if isinstance(config, dict) else {}

        self.memory_writer = MemoryWriter(
            memory_writer_cfg,
            llm_modules.get("memory_writer"),
        )
        self.user_profile_updater = UserProfileUpdater(
            profile_cfg,
            llm_modules.get("user_profile_updater"),
        )

    def ensure_dependencies(self) -> None:
        required = {
            "database_manager": self.database_manager,
            "vector_store": self.vector_store,
            "memory_writer": self.memory_writer,
            "user_profile_updater": self.user_profile_updater,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise RuntimeError(f"SubconsciousMemory dependencies missing: {missing}")

    async def search_memory_context_for_topic(
        self,
        user_id: str,
        queries: List[str],
        similarity_threshold: float = 0.8,
        k: int = 3,
    ) -> MemoryContext:
        """按话题线索召回记忆: 向量命中 → 批量回查数据库正本。"""
        if not queries:
            return MemoryContext()

        candidate_hits: List[Tuple[float, str, str, Any, str]] = []
        vector_ids: List[str] = []
        for query in queries:
            q = (query or "").strip()
            if not q:
                continue
            results = await self.vector_store.search(user_id, q, k=max(1, k))
            for doc, score in results:
                if score < similarity_threshold:
                    continue
                content = doc.get_content().strip()
                if not content:
                    continue
                vector_id = str(getattr(doc, "id", "") or "")
                if vector_id:
                    vector_ids.append(vector_id)
                candidate_hits.append((score, q, vector_id, doc, content))

        records_by_vector_id = await asyncio.to_thread(
            self.database_manager.get_agent_memory_records_by_embedding_ids,
            vector_ids,
        )

        scored_hits: List[Tuple[float, str, MemoryHit]] = []
        for score, query, vector_id, doc, content in candidate_hits:
            record = records_by_vector_id.get(vector_id) if vector_id else None
            rendered = self._render_memory_hit(record, content, doc)
            dedup_key = record.id if record else (vector_id or rendered)
            scored_hits.append((
                score, dedup_key,
                MemoryHit(
                    rendered_text=rendered,
                    score=score,
                    query=query,
                    source="canonical_vector" if record else "legacy_vector",
                    record=record,
                    vector_id=vector_id or None,
                ),
            ))

        scored_hits.sort(key=lambda item: item[0], reverse=True)
        hits: List[MemoryHit] = []
        seen_keys: set[str] = set()
        seen_text: set[str] = set()
        for _, dedup_key, hit in scored_hits:
            if dedup_key in seen_keys or hit.rendered_text in seen_text:
                continue
            seen_keys.add(dedup_key)
            seen_text.add(hit.rendered_text)
            hits.append(hit)
            if len(hits) >= k:
                break
        return MemoryContext(tuple(hits))

    async def write_topic_memories(
        self,
        user_id: str,
        history: str,
        current_dialogue: str = "",
        related_memories: List[str] | None = None,
        commit: bool = True,
    ) -> Dict[str, Any]:
        return await self.memory_writer.process_interaction(
            vector_store=self.vector_store,
            memory_store=self.database_manager,
            user_id=user_id,
            history=history,
            current_dialogue=current_dialogue,
            related_memories=related_memories or [],
            owner_character_id=self.owner_character_id,
            commit=commit,
        )

    async def write_user_memory(self, user_id: str, content: str, commit: bool = True) -> bool:
        return await self.memory_writer.write_user_memory(
            vector_store=self.vector_store,
            memory_store=self.database_manager,
            user_id=user_id,
            content=content,
            owner_character_id=self.owner_character_id,
            commit=commit,
        )

    async def write_event_memory(self, user_id: str, content: str, commit: bool = True) -> bool:
        return await self.memory_writer.write_event_memory(
            vector_store=self.vector_store,
            memory_store=self.database_manager,
            user_id=user_id,
            content=content,
            owner_character_id=self.owner_character_id,
            commit=commit,
        )

    async def update_user_profile_by_context(
        self,
        user_id: str,
        context: Dict[str, Any],
        character_name: str = "",
        commit: bool = True,
    ) -> str | None:
        current_profile = self.database_manager.get_user_description(user_id) or ""
        new_profile = await self.user_profile_updater.update_profile(
            history=context,
            current_profile=current_profile,
            character_name=character_name,
        )
        if not new_profile:
            return None
        await asyncio.to_thread(
            self.database_manager.update_user_description,
            user_id, new_profile, commit,
        )
        return new_profile

    def _render_memory_hit(self, record, fallback_content: str, doc) -> str:
        if record is not None:
            timestamp = ""
            if record.happened_at:
                timestamp = record.happened_at.strftime("%Y-%m-%d")
            elif record.created_at:
                timestamp = record.created_at.strftime("%Y-%m-%d")
            content = (record.summary or record.content or fallback_content).strip()
            return f"在{timestamp}, {content}" if timestamp else content

        metadata = doc.get_metadata() if hasattr(doc, "get_metadata") else {}
        timestamp = ""
        if isinstance(metadata, dict):
            timestamp = str(metadata.get("timestamp") or metadata.get("event_date") or "").strip()
        return f"在{timestamp}, {fallback_content}" if timestamp else fallback_content
