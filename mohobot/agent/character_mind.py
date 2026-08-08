"""角色潜意识 — 移植自 Agent-LuoTianyi (src/subconscious/character_mind.py)。

每角色一个实例;拥有召回、话题抽取、注意力规划、状态、记忆/画像写入。
意识层(agent)只消费这里产出的 plan 与 context。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from mohobot.agent.attention import AttentionPlanner
from mohobot.agent.domain import (
    AgentState, ExtractedTopic, MemoryContext, UnreadMessage, UnreadMessageSnapshot,
)
from mohobot.agent.subconscious_memory import SubconsciousMemory
from mohobot.agent.topic_extractor import TopicExtractor


class SubconsciousState:
    """极薄的状态壳,持有 AgentState 快照。"""

    def __init__(self, owner_character_id: str):
        self.owner_character_id = owner_character_id
        self._snapshot = AgentState(owner_character_id=owner_character_id)

    def get_snapshot(self) -> AgentState:
        return self._snapshot

    def update(self, **kwargs) -> AgentState:
        self._snapshot = self._snapshot.with_updates(**kwargs)
        return self._snapshot


class CharacterSubconscious:
    """角色潜意识门面。"""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        database_manager,
        memory: SubconsciousMemory,
        llm_modules: dict[str, Any],
        character_id: str = "bot",
        character_name: str = "",
        anysearch_client=None,
    ):
        self.config = config
        self.database_manager = database_manager
        self.character_id = character_id
        self.character_name = character_name or character_id
        self.logger = logger.bind(agent=f"{character_id}Subconscious")
        self.anysearch = anysearch_client  # 实时联网搜索(可选)

        self.memory = memory
        self.state = SubconsciousState(owner_character_id=character_id)

        # 兼容两种传入方式: 直接传 agent 段,或包在 {"agent": {...}} 里
        agent_cfg = config
        if isinstance(config, dict) and "topic_extractor" not in config and "attention_planner" not in config:
            agent_cfg = config.get("agent", {}) or {}
        topic_extractor_cfg = agent_cfg.get("topic_extractor", {})
        attention_cfg = agent_cfg.get("attention_planner", {})

        self.topic_extractor = TopicExtractor(
            topic_extractor_cfg,
            character_id=character_id,
            llm_module=llm_modules.get("topic_extractor"),
            character_name=self.character_name,
        )
        self.attention_planner = AttentionPlanner(
            attention_cfg,
            target_character_id=character_id,
        )

    def get_state(self) -> AgentState:
        return self.state.get_snapshot()

    async def extract_topics(
        self,
        user_id: str,
        unread_snapshot: UnreadMessageSnapshot,
        force_complete: bool = False,
        conversation_history: str | None = None,
    ) -> Tuple[Optional[ExtractedTopic], List[UnreadMessage]]:
        if unread_snapshot is None or not unread_snapshot.messages:
            return None, []
        return await self.topic_extractor.extract_topics(
            unread_snapshot=unread_snapshot,
            conversation_history=conversation_history or "",
            force_complete=force_complete,
        )

    async def search_fact_constraints_for_topic(self, fact_constraints: List[str]) -> List[str]:
        """事实约束检索: 通过 Anysearch 实时联网搜索外部信息。

        fact_constraints 由话题提取器产出(需实时信息的问题);
        每话题最多搜 2 个查询, 结果注入回复提示词。
        未配置 key / 搜索失败 → 返回空列表(降级, 不阻断回复)。
        """
        if not fact_constraints or self.anysearch is None:
            return []

        queries = [q.strip() for q in fact_constraints if q and q.strip()][:2]
        if not queries:
            return []

        import asyncio

        results = await asyncio.gather(
            *[self.anysearch.safe_search(q, max_results=5) for q in queries],
            return_exceptions=True,
        )
        hits: List[str] = []
        for q, r in zip(queries, results):
            if isinstance(r, str) and r.strip():
                hits.append(f"[搜索: {q}]\n{r[:2000]}")
            else:
                self.logger.debug(f"Fact search no result for: {q}")
        return hits

    async def search_memory_context_for_topic(
        self,
        user_id: str,
        queries: List[str],
        similarity_threshold: float = 0.8,
        k: int = 3,
    ) -> MemoryContext:
        if not queries:
            return MemoryContext()
        return await self.memory.search_memory_context_for_topic(
            user_id=user_id,
            queries=queries,
            similarity_threshold=similarity_threshold,
            k=k,
        )

    async def plan_topic_turn(
        self,
        user_id: str,
        topic: ExtractedTopic,
        conversation_history: str,
        external_context: Optional[str] = None,
    ) -> Any:
        return await self.attention_planner.plan_topic_turn(
            user_id=user_id,
            topic=topic,
            conversation_history=conversation_history,
            memory_search=lambda queries: self.search_memory_context_for_topic(
                user_id=user_id, queries=queries, similarity_threshold=0.8,
            ),
            fact_search=self.search_fact_constraints_for_topic,
            sing_planner=self._plan_sing_attempts_for_topic,
            external_context=external_context,
            agent_state=self.state.get_snapshot(),
        )

    async def write_topic_memories(
        self,
        user_id: str,
        current_dialogue: str,
        related_memories: Optional[List[str]] = None,
        conversation_history: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self.memory.write_topic_memories(
            user_id=user_id,
            history=conversation_history or "",
            current_dialogue=current_dialogue,
            related_memories=related_memories or [],
            character_name=self.character_name,
            commit=True,
        )

    async def update_user_profile_by_context(
        self, user_id: str, context: dict[str, Any],
    ) -> str | None:
        return await self.memory.update_user_profile_by_context(
            user_id=user_id,
            context=context,
            character_name=self.character_name,
        )

    async def _plan_sing_attempts_for_topic(
        self, sing_attempts: List[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        """简化: mohobot 不支持唱歌,返回 (None, None)。"""
        return (None, None)
