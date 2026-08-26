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
class _OutboundItem:
    operation: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]
    priority: int
    sequence: int
    rate_limited: bool
    label: str


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
        )
        if queue.full():
            # Fail fast instead of holding the caller only to hit the same
            # timeout later; the worker is saturated for this bot.
            raise OutboundQueueFullError(
                f"outbound queue full for bot {bot_id} ({label})"
            )
        try:
            await asyncio.wait_for(
                queue.put((item.priority, item.sequence, item)), timeout=self._enqueue_timeout
            )
        except asyncio.TimeoutError as exc:
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
                    if self._interval:
                        delay = self._interval - (time.monotonic() - last_message_at)
                        if delay > 0:
                            await asyncio.sleep(delay)
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
    ) -> Any:
        return await self.scheduler.submit(address.bot_id, operation, control=False, label=label)

    async def call(
        self,
        bot_id: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        label: str = "api",
    ) -> Any:
        return await self.scheduler.submit(bot_id, operation, control=True, label=label)
