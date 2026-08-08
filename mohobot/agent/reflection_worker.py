"""反思工作器 — 移植自 Agent-LuoTianyi (src/chat_session/chat_pipeline/reflection_worker.py)。

每回合结束后的串行后处理队列: 记忆写入、上下文压缩+画像更新。
不阻塞回复发送。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

from loguru import logger

from mohobot.agent.domain import ExtractedTopic, OneResponseLine, TopicAttentionPlan


@dataclass
class CompletedTurn:
    user_id: str
    character_id: str
    topic: ExtractedTopic
    reply_items: List[OneResponseLine]
    attention_plan: Optional[TopicAttentionPlan] = None
    conversation_history: str = ""


class ReflectionWorker:
    """串行反思 worker: 记忆写入 + 画像更新。"""

    def __init__(
        self,
        config: dict,
        character_id: str = "bot",
    ):
        self.config = config
        self.character_id = character_id
        self.logger = logger.bind(agent="ReflectionWorker")
        self.reflection_queue: asyncio.Queue[CompletedTurn] = asyncio.Queue()
        self.processor_task: Optional[asyncio.Task] = None

        # 注入的回调
        self._write_memories_cb: Optional[Callable[[CompletedTurn], Awaitable[None]]] = None
        self._update_profile_cb: Optional[Callable[[CompletedTurn], Awaitable[None]]] = None

    def set_write_memories_callback(self, cb) -> None:
        self._write_memories_cb = cb

    def set_update_profile_callback(self, cb) -> None:
        self._update_profile_cb = cb

    def start_processing(self) -> None:
        if self.processor_task is None or self.processor_task.done():
            self.processor_task = asyncio.create_task(self._reflection_processor())
            self.logger.info("Reflection worker processor task started")

    async def submit_completed_turn(self, turn: CompletedTurn) -> None:
        await self.reflection_queue.put(turn)

    async def _reflection_processor(self) -> None:
        while True:
            turn: CompletedTurn | None = None
            try:
                turn = await self.reflection_queue.get()
                await self._reflect_completed_turn(turn)
            except asyncio.CancelledError:
                self.logger.info("Reflection worker task cancelled")
                break
            except Exception as e:
                self.logger.exception(f"Reflection worker error: {e}")
                await asyncio.sleep(0.1)
            finally:
                if turn is not None:
                    self.reflection_queue.task_done()

    async def _reflect_completed_turn(self, turn: CompletedTurn) -> None:
        await self._write_topic_memories(turn)
        await self._update_user_profile(turn)

    async def _write_topic_memories(self, turn: CompletedTurn) -> None:
        if self._write_memories_cb is None:
            return
        if len(getattr(turn.topic, "source_messages", []) or []) == 0:
            self.logger.info("No source messages for topic, skip memory write")
            return
        try:
            start = time.perf_counter()
            await self._write_memories_cb(turn)
            duration_ms = (time.perf_counter() - start) * 1000
            self.logger.debug(f"Topic memory write done in {duration_ms:.0f}ms")
        except Exception as e:
            self.logger.warning(f"Topic memory write failed: {e}")

    async def _update_user_profile(self, turn: CompletedTurn) -> None:
        if self._update_profile_cb is None:
            return
        try:
            await self._update_profile_cb(turn)
        except Exception as e:
            self.logger.warning(f"User profile update failed: {e}")

    @staticmethod
    def build_current_dialogue(topic: ExtractedTopic, reply_items: List[OneResponseLine]) -> str:
        """构造 "user: ...\nagent: ..." 形式的当前对话。"""
        lines: List[str] = []
        for msg in getattr(topic, "source_messages", []) or []:
            content = (getattr(msg, "content", "") or "").strip()
            if content:
                lines.append(f"user: {content}")
        for item in reply_items:
            lines.append(f"agent: {item.get_content()}")
        return "\n".join(lines)
