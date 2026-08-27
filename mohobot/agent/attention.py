"""注意力规划 — 移植自 Agent-LuoTianyi (src/subconscious/attention.py)。

对一个话题并行执行: 记忆检索、事实检索,产出 TopicAttentionPlan。
歌曲属性已移除(唱歌回复机制删除; 歌曲信息改由消息前注入)。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

from mohobot.agent.domain import (
    ActionPlan, ActionType, AgentState, MemoryContext, PlannedAction,
    TopicAttentionPlan,
)


class TopicLike(Protocol):
    topic_id: str
    topic_content: str
    memory_attempts: list[str]
    fact_constraints: list[str]


MemorySearch = Callable[[list[str]], Awaitable[MemoryContext]]
FactSearch = Callable[[list[str]], Awaitable[list[str]]]


class AttentionPlanner:
    """选择注意力素材与粗略动作。"""

    def __init__(self, config: dict, target_character_id: str = "bot") -> None:
        self.config = config
        self.target_character_id = target_character_id

    async def plan_topic_turn(
        self,
        *,
        user_id: str,
        topic: TopicLike,
        conversation_history: str,
        memory_search: MemorySearch,
        fact_search: FactSearch,
        external_context: str | None = None,
        agent_state: AgentState | None = None,
    ) -> TopicAttentionPlan:
        topic_content, attention_notes = self._merge_external_context(
            topic.topic_content, external_context,
        )

        memory_task = asyncio.create_task(
            self._timed(memory_search(topic.memory_attempts or []))
        )
        fact_task = asyncio.create_task(
            self._timed(fact_search(topic.fact_constraints or []))
        )
        (memory_context, memory_duration), (fact_hits, _) = (
            await asyncio.gather(memory_task, fact_task)
        )
        memory_hits = memory_context.render_for_prompt()

        actions = [PlannedAction(ActionType.SAY, {"topic_id": topic.topic_id})]

        action_plan = ActionPlan(
            target_character_id=self.target_character_id,
            actions=tuple(actions),
            attention_notes=tuple(attention_notes),
        )
        return TopicAttentionPlan(
            user_id=user_id,
            topic_id=topic.topic_id,
            target_character_id=self.target_character_id,
            topic_content=topic_content,
            conversation_history=conversation_history,
            memory_context=memory_context,
            agent_state=agent_state,
            memory_hits=memory_hits,
            fact_hits=fact_hits,
            attention_notes=tuple(attention_notes),
            action_plan=action_plan,
        )

    async def _timed(self, coro) -> tuple[Any, float]:
        start = time.perf_counter()
        result = await coro
        return result, (time.perf_counter() - start) * 1000

    def _merge_external_context(self, topic_content: str, external_context: str | None) -> tuple[str, list[str]]:
        context = (external_context or "").strip()
        if not context:
            return topic_content, []
        merged = f"{topic_content}\n\n{context}" if topic_content else context
        return merged, ["external_context"]
