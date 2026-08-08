"""话题规划器 — 移植自 Agent-LuoTianyi (src/chat_session/chat_pipeline/topic_planner.py)。

缓冲未读消息,通过"监听计时器 + 唤醒事件"判断用户是否说完,
批量调 LLM 提取话题,再把 ExtractedTopic 交给 consumer(TopicReplier)。

简化: 无 WebSocket 专属事件(typing / image_selecting),只处理普通消息。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, List, Optional, Tuple
from uuid import uuid4

from loguru import logger

from mohobot.agent.domain import (
    ChatInputEvent, ChatInputEventType, ExtractedTopic, UnreadMessage,
    UnreadMessageSnapshot,
)


class ListenTimer:
    """监听计时器: 记录等待截止时间。"""

    def __init__(self, config: dict, timeout: float = 1.0):
        self.config = config
        self.timeout = float(config.get("timeout", timeout))
        self._deadline: float | None = None

    @property
    async def deadline(self) -> float | None:
        return self._deadline

    async def set_deadline(self, timeout: float | None = None) -> None:
        self._deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)

    async def remove_deadline(self) -> None:
        self._deadline = None


class UnreadStore:
    """内存未读消息缓冲(与洛天依一致: snapshot 时清空,commit 时回填剩余)。"""

    def __init__(self, config: dict):
        self.config = config
        self._messages: List[UnreadMessage] = []
        self._version = 0

    async def append(self, msg: UnreadMessage) -> None:
        self._messages.append(msg)
        self._version += 1

    async def has_unread(self) -> bool:
        return bool(self._messages)

    async def snapshot(self) -> UnreadMessageSnapshot:
        """取快照并清空缓冲;提取期间新到的消息留在 _messages 中。"""
        messages = list(self._messages)
        self._messages.clear()
        self._version += 1
        return UnreadMessageSnapshot(messages=messages, version=self._version)

    async def clear(self) -> None:
        self._messages.clear()
        self._version += 1

    async def update_unread_message(
        self, snapshot: UnreadMessageSnapshot, remaining: List[UnreadMessage],
    ) -> None:
        """提取后: 剩余消息 + 提取期间新到的消息 重新入缓冲。"""
        self._messages = list(remaining) + self._messages
        self._version += 1


class TopicPlanner:
    def __init__(
        self,
        config: dict,
        character_id: str = "bot",
        context_provider: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        self.config = config
        self.character_id = character_id
        self.context_provider = context_provider
        self.logger = logger.bind(agent="TopicPlanner")

        self.unread_store = UnreadStore(config.get("unread_store", {}))
        self.listen_timer = ListenTimer(config.get("listen_timer", {}))
        self.processor_task: Optional[asyncio.Task] = None
        self.topic_consumer: Optional[Callable[[ExtractedTopic], Awaitable[None]]] = None
        self._wake_event = asyncio.Event()
        self._extraction_in_progress = False
        self._consecutive_failures = 0  # 连续提取失败计数(退避用)

    def _retry_timeout(self) -> float:
        """连续失败时逐渐拉长等待,避免 LLM 故障时每 1.5s 疯狂重试。"""
        base = float((self.config.get("listen_timer", {}) or {}).get("timeout", 1.5))
        return base + min(self._consecutive_failures, 10) * 2.0

    def set_topic_consumer(self, consumer) -> None:
        self.topic_consumer = consumer

    async def feed_unread_message(self, message: ChatInputEvent) -> None:
        """接收一条用户消息: 入缓冲 + 重置等待超时 + 唤醒处理循环。"""
        unread = UnreadMessage(
            message_id=message.message_id or str(uuid4()),
            content=message.content or "",
            message_type=message.message_type,
            target_character_ids=(message.character_id or self.character_id,),
            terms=message.terms or [],
            timestamp=message.timestamp or time.time(),
            speaker=message.payload.get("speaker", ""),
        )
        await self.unread_store.append(unread)
        await self.listen_timer.set_deadline()
        self._wake_event.set()

    def start_processing(self) -> None:
        if self.processor_task is None or self.processor_task.done():
            self.processor_task = asyncio.create_task(self._message_processor())
            self.logger.info("TopicPlanner processor task started")

    async def _message_processor(self) -> None:
        while True:
            try:
                should_force_extract = False
                deadline = await self.listen_timer.deadline
                has_unread = await self.unread_store.has_unread()

                if has_unread and deadline is not None:
                    timeout = max(0.0, deadline - time.monotonic())
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
                        self._wake_event.clear()
                        continue
                    except asyncio.TimeoutError:
                        should_force_extract = True
                elif has_unread:
                    pass  # 有未读且无需等待: 直接提取
                else:
                    await self._wake_event.wait()
                    self._wake_event.clear()
                    continue

                snapshot = await self.unread_store.snapshot()
                await self.listen_timer.remove_deadline()
                self._extraction_in_progress = True
                try:
                    extracted_topic, remaining_unread = await self._extract_topics(
                        snapshot, force_complete=should_force_extract,
                    )
                    topics = await self._commit_extraction_result(
                        snapshot=snapshot,
                        extracted_topics=[extracted_topic] if extracted_topic else [],
                        remaining_unread=remaining_unread,
                    )
                finally:
                    self._extraction_in_progress = False

                if topics:
                    await self._consume_topics(topics)
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1

            except asyncio.CancelledError:
                self.logger.info("TopicPlanner processor task cancelled")
                await self.unread_store.clear()
                break
            except Exception as e:
                self.logger.exception(f"TopicPlanner processor error: {e}")
                await asyncio.sleep(0.1)

    async def _extract_topics(
        self, unread_snapshot: UnreadMessageSnapshot, force_complete: bool,
    ) -> Tuple[Optional[ExtractedTopic], List[UnreadMessage]]:
        """调用 agent 话题提取接口;失败时降级为简单规则提取。"""
        if unread_snapshot is None or not unread_snapshot.messages:
            return None, []

        try:
            conversation_history = await self._get_conversation_context()
            if self.topic_consumer is None:
                # 没有 consumer 时不提取,保留消息
                return None, unread_snapshot.messages

            topic, remaining = await self._extract_via_agent(
                unread_snapshot, force_complete, conversation_history,
            )
            return topic, remaining or []
        except Exception as e:
            self.logger.exception(f"Topic extraction failed, use fallback: {e}")
            topic, remaining = self._fallback_extract(unread_snapshot, force_complete=True)
            return topic, remaining

    async def _extract_via_agent(
        self, unread_snapshot: UnreadMessageSnapshot, force_complete: bool, conversation_history: str,
    ) -> Tuple[Optional[ExtractedTopic], List[UnreadMessage]]:
        """由 AgentRuntime 提供的话题提取(由外部注入到 self._extractor)。"""
        extractor = getattr(self, "_extractor", None)
        if extractor is None:
            return self._fallback_extract(unread_snapshot, force_complete)
        return await extractor(
            user_id="",
            unread_snapshot=unread_snapshot,
            force_complete=force_complete,
            conversation_history=conversation_history,
        )

    def set_extractor(self, extractor: Callable) -> None:
        """注入话题提取回调 (runtime.extract_topic)。"""
        self._extractor = extractor

    async def _commit_extraction_result(
        self,
        snapshot: UnreadMessageSnapshot,
        extracted_topics: List[ExtractedTopic],
        remaining_unread: List[UnreadMessage],
    ) -> List[ExtractedTopic]:
        has_new_message = await self.unread_store.has_unread()
        has_new_waiting_signal = (await self.listen_timer.deadline) is not None
        should_restart_waiting = has_new_message or has_new_waiting_signal

        if should_restart_waiting:
            # 提取期间有新消息: 丢弃提取结果,保留原消息继续等待补全
            remaining_unread = snapshot.messages.copy()
            new_extracted_topics: List[ExtractedTopic] = []
        else:
            new_extracted_topics = extracted_topics

        await self.unread_store.update_unread_message(snapshot, remaining_unread)

        if should_restart_waiting:
            self._wake_event.set()
            if has_new_message and not has_new_waiting_signal:
                await self.listen_timer.set_deadline()
        else:
            if remaining_unread:
                # 失败退避: 连续失败时拉长等待时间
                await self.listen_timer.set_deadline(timeout=self._retry_timeout())
            else:
                await self.listen_timer.remove_deadline()
        return new_extracted_topics

    async def _get_conversation_context(self) -> str:
        if self.context_provider is not None:
            context = await self.context_provider()
            return context if isinstance(context, str) else ""
        return ""

    def _fallback_extract(
        self, unread_snapshot: UnreadMessageSnapshot, force_complete: bool,
    ) -> Tuple[Optional[ExtractedTopic], List[UnreadMessage]]:
        """最小兜底策略: 整批消息作为一个话题。"""
        messages = unread_snapshot.messages
        if not messages:
            return None, []

        latest_content = (messages[-1].content or "").strip()
        terminal_tokens = ("。", "！", "？", ".", "!", "?", "~")
        likely_complete = (
            len(messages) >= 2
            or messages[-1].message_type == "image"
            or latest_content.endswith(terminal_tokens)
            or len(latest_content) >= 16
        )

        if not force_complete and not likely_complete:
            return None, messages

        topic = ExtractedTopic(
            topic_id=str(uuid4()),
            source_messages=messages,
            topic_content="\n".join(
                f"{m.speaker}: {m.content}" if m.speaker else m.content
                for m in messages if m.content
            ),
            memory_attempts=[],
            fact_constraints=[],
            sing_attempts=[],
            target_character_ids=(self.character_id,),
            is_forced_from_incomplete=force_complete,
        )
        return topic, []

    async def _consume_topics(self, topics: List[ExtractedTopic]) -> None:
        if self.topic_consumer is None:
            self.logger.error("No topic_consumer set, skip topics")
            return
        for topic in topics:
            await self.topic_consumer(topic)
