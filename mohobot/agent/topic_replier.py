"""话题回复器 — 移植自 Agent-LuoTianyi (src/chat_session/chat_pipeline/topic_replier.py)。

消费 ExtractedTopic 队列,执行"规划 → 实现 → 持久化 → 发送 → 提交反思"。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, List, Optional

from loguru import logger

from mohobot.agent.domain import ExtractedTopic, OneResponseLine


class TopicReplier:
    def __init__(
        self,
        config: dict,
        character_id: str = "bot",
        context_provider: Optional[Callable[..., Awaitable[str]]] = None,
        reflection_submitter: Optional[Callable[[Any], Awaitable[None]]] = None,
    ):
        self.config = config
        self.character_id = character_id
        self.logger = logger.bind(agent="TopicReplier")
        self.topic_queue: asyncio.Queue = asyncio.Queue()
        self.processor_task: Optional[asyncio.Task] = None
        self.is_processing: bool = False

        self.send_reply_callback: Optional[Callable[[List[OneResponseLine]], Awaitable[None]]] = None
        self.context_provider = context_provider
        self.reflection_submitter = reflection_submitter
        self._reply_one_callback: Optional[Callable[[ExtractedTopic], Awaitable[List[OneResponseLine]]]] = None

    def set_send_reply_callback(self, cb) -> None:
        self.send_reply_callback = cb

    def set_context_provider(self, provider) -> None:
        self.context_provider = provider

    def set_reflection_submitter(self, submitter) -> None:
        self.reflection_submitter = submitter

    def set_reply_one_callback(self, cb) -> None:
        """注入单话题回复回调 (runtime.reply_one_topic)。"""
        self._reply_one_callback = cb

    def start_processing(self) -> None:
        if self.processor_task is None or self.processor_task.done():
            self.processor_task = asyncio.create_task(self.topic_processor())
            self.logger.info("TopicReplier processor task started")

    async def add_topic(self, topic: ExtractedTopic) -> None:
        await self.topic_queue.put(topic)

    async def topic_processor(self) -> None:
        while True:
            topic: ExtractedTopic | None = None
            try:
                topic = await self.topic_queue.get()
                self.is_processing = True
                await self._reply_one_topic(topic)
            except asyncio.CancelledError:
                self.logger.info("TopicReplier processor task cancelled")
                break
            except Exception as e:
                self.logger.error(f"Error in topic_processor: {e}")
                await asyncio.sleep(0.1)
            finally:
                if topic is not None:
                    self.topic_queue.task_done()
                self.is_processing = False

    async def _reply_one_topic(self, topic: ExtractedTopic) -> None:
        if self._reply_one_callback is None:
            self.logger.error("No reply_one_callback set, skip topic")
            return

        reply_items = await self._reply_one_callback(topic)

        # 持久化 agent 回复到数据库
        persist = getattr(self, "_persist_replies", None)
        if persist is not None:
            try:
                await persist(reply_items)
            except Exception as e:
                self.logger.warning(f"Persist replies failed: {e}")

        # 发送
        if self.send_reply_callback is not None and reply_items:
            await self.send_reply_callback(reply_items)

        # 提交反思
        if self.reflection_submitter is not None:
            try:
                await self.reflection_submitter(topic, reply_items)
            except Exception as e:
                self.logger.warning(f"Reflection submit failed: {e}")

    def set_persist_replies(self, persist: Callable[[List[OneResponseLine]], Awaitable[None]]) -> None:
        self._persist_replies = persist
