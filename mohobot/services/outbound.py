"""Per-bot priority outbound queue and compatible reply facade."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger


class OutboundClosedError(RuntimeError):
    pass


class OutboundQueueFullError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatAddress:
    bot_id: str
    chat_type: str
    chat_id: int | str


@dataclass
class DispatchAttempt:
    """调用方可见的派发标记: worker 真正开始执行 operation 之前置位。

    取消语义依赖它。入队后调用方被取消(超时/父任务取消)时, 必须区分两种状态:

    - **未派发**: 排队中或仍在限速等待 —— 消息还没发出去, worker 应当直接丢弃,
      否则调用方以为已放弃, 用户却收到一条过时消息;
    - **已派发**: 已经进入传输层 —— 无法撤回, 归档/回执所有权必须转交后台,
      不能随调用方一起消失, 否则 history 与实际投递不一致。
    """

    dispatched: bool = False


@dataclass
class _OutboundItem:
    operation: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]
    priority: int
    sequence: int
    rate_limited: bool
    label: str
    attempt: DispatchAttempt | None = None


class OutboundScheduler:
    """Runs control calls before messages and rate-limits messages per bot.

    Messages are serialized per bot with a minimum send interval. Control
    calls (API queries with pending response futures) never wait behind a
    message: they execute immediately, bypassing the rate limiter.
    """

    CONTROL_PRIORITY = 0
    MESSAGE_PRIORITY = 10

    def __init__(
        self,
        *,
        send_interval_sec: float = 0.5,
        queue_maxsize: int = 100,
        enqueue_timeout_sec: float = 2.0,
    ) -> None:
        self._interval = max(0.0, float(send_interval_sec))
        self._maxsize = max(1, int(queue_maxsize))
        self._enqueue_timeout = max(0.01, float(enqueue_timeout_sec))
        # Messages only: priority queue per bot, drained by a rate-limiting worker.
        self._queues: dict[str, asyncio.PriorityQueue[tuple[int, int, _OutboundItem]]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._sequence = itertools.count()
        self._accepting = True

    async def submit(
        self,
        bot_id: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        control: bool = False,
        label: str = "outbound",
        attempt: DispatchAttempt | None = None,
    ) -> Any:
        if not self._accepting:
            raise OutboundClosedError("outbound scheduler is stopping")
        if control:
            # Control calls execute inline: they must not queue behind a long
            # message batch (API responses hold pending futures and timeouts).
            return await operation()
        queue = self._queues.get(bot_id)
        if queue is None:
            queue = asyncio.PriorityQueue(maxsize=self._maxsize)
            self._queues[bot_id] = queue
        worker = self._workers.get(bot_id)
        if worker is None or worker.done():
            self._workers[bot_id] = asyncio.create_task(
                self._worker(bot_id, queue), name=f"outbound:{bot_id}"
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        item = _OutboundItem(
            operation=operation,
            future=future,
            priority=self.MESSAGE_PRIORITY,
            sequence=next(self._sequence),
            rate_limited=True,
            label=label,
            attempt=attempt,
        )
        if queue.full():
            # Fail fast instead of holding the caller only to hit the same
            # timeout later; the worker is saturated for this bot.
            raise OutboundQueueFullError(
                f"outbound queue full for bot {bot_id} ({label})"
            )
        # The full check and non-blocking put deliberately form one fast-fail
        # boundary.  Awaiting queue.put() through asyncio.wait_for() lets Python
        # 3.10/3.11 swallow caller cancellation when the put completes in the
        # same loop turn; a cancelled caller would then wait on `future` forever.
        try:
            queue.put_nowait((item.priority, item.sequence, item))
        except asyncio.QueueFull as exc:
            raise OutboundQueueFullError(
                f"outbound queue full for bot {bot_id} ({label})"
            ) from exc
        return await future

    def _inflight_count(self, bot_id: str) -> int:
        """Messages currently queued or executing for a bot."""
        queue = self._queues.get(bot_id)
        return queue.qsize() if queue else 0


    async def _worker(
        self,
        bot_id: str,
        queue: asyncio.PriorityQueue[tuple[int, int, _OutboundItem]],
    ) -> None:
        last_message_at = 0.0
        inflight: _OutboundItem | None = None
        try:
            while True:
                _, _, item = await queue.get()
                inflight = item
                try:
                    # 调用方可能在入队后放弃(取消/超时): 未派发前不得发送。
                    if item.future.done():
                        logger.debug(
                            "Outbound item abandoned before dispatch: bot={} label={}",
                            bot_id, item.label,
                        )
                        continue
                    if self._interval:
                        delay = self._interval - (time.monotonic() - last_message_at)
                        if delay > 0:
                            await asyncio.sleep(delay)
                            # 限速等待期间被取消同样不得发送。
                            if item.future.done():
                                logger.debug(
                                    "Outbound item abandoned during rate limit: bot={} label={}",
                                    bot_id, item.label,
                                )
                                continue
                    if item.attempt is not None:
                        item.attempt.dispatched = True
                    result = await item.operation()
                    last_message_at = time.monotonic()
                    if not item.future.done():
                        item.future.set_result(result)
                except asyncio.CancelledError:
                    if inflight is not None and not inflight.future.done():
                        inflight.future.set_exception(OutboundClosedError("outbound send cancelled"))
                    raise
                except Exception as exc:
                    logger.warning(
                        "Outbound operation failed: bot={} label={}: {}",
                        bot_id,
                        item.label,
                        exc,
                    )
                    if not item.future.done():
                        item.future.set_exception(exc)
                finally:
                    inflight = None
                    queue.task_done()
        except asyncio.CancelledError:
            while not queue.empty():
                try:
                    _, _, pending = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not pending.future.done():
                    pending.future.set_exception(OutboundClosedError("outbound scheduler stopped"))
                queue.task_done()
            raise

    async def close(self, *, drain_timeout: float = 5.0) -> None:
        if not self._accepting and not self._workers:
            return
        self._accepting = False
        try:
            await asyncio.wait_for(
                asyncio.gather(*(queue.join() for queue in self._queues.values())),
                timeout=drain_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Outbound queues did not drain within {}s", drain_timeout)
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._queues.clear()


class ReplySender:
    """Normalized message sender used by core and compatible WSServer methods."""

    def __init__(self, scheduler: OutboundScheduler) -> None:
        self.scheduler = scheduler

    async def send(
        self,
        address: ChatAddress,
        operation: Callable[[], Awaitable[Any]],
        *,
        label: str = "message",
        attempt: DispatchAttempt | None = None,
    ) -> Any:
        return await self.scheduler.submit(
            address.bot_id, operation, control=False, label=label, attempt=attempt,
        )

    async def call(
        self,
        bot_id: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        label: str = "api",
    ) -> Any:
        return await self.scheduler.submit(bot_id, operation, control=True, label=label)
