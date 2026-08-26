"""Application-wide background task ownership and graceful shutdown."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Coroutine
from typing import Any

from loguru import logger


class TaskSupervisorClosed(RuntimeError):
    """Raised when a task is submitted while the supervisor is stopping."""


class TaskSupervisor:
    """Tracks background tasks, reports failures, and shuts them down by owner."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._owners: dict[str, set[asyncio.Task[Any]]] = defaultdict(set)
        self._stopping = False

    @property
    def stopping(self) -> bool:
        return self._stopping

    def create_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
        owner: str = "application",
    ) -> asyncio.Task[Any]:
        if self._stopping:
            coro.close()
            raise TaskSupervisorClosed("background task supervisor is stopping")
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        self._owners[owner].add(task)
        task.add_done_callback(lambda done: self._task_done(done, owner))
        return task

    def _task_done(self, task: asyncio.Task[Any], owner: str) -> None:
        self._tasks.discard(task)
        owner_tasks = self._owners.get(owner)
        if owner_tasks is not None:
            owner_tasks.discard(task)
            if not owner_tasks:
                self._owners.pop(owner, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "Background task failed: owner={} name={}: {}",
                owner,
                task.get_name(),
                error,
            )

    async def cancel_owner(self, owner: str, timeout: float = 5.0) -> None:
        await self._cancel_tasks(set(self._owners.get(owner, ())), timeout)

    async def shutdown(self, timeout: float = 10.0) -> None:
        self._stopping = True
        await self._cancel_tasks(set(self._tasks), timeout)
        for task in list(self._tasks):
            task.cancel()
        pending = {task for task in self._tasks if not task.done()}
        if pending:
            done, still_pending = await asyncio.wait(pending, timeout=timeout)
            for task in done:
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
            if still_pending:
                names = ", ".join(sorted(task.get_name() for task in still_pending))
                logger.warning(
                    "Background tasks did not stop within {}s: {}", timeout, names
                )
        self._tasks.clear()
        self._owners.clear()

    async def _cancel_tasks(
        self, tasks: set[asyncio.Task[Any]], timeout: float
    ) -> None:
        current = asyncio.current_task()
        pending = {task for task in tasks if task is not current and not task.done()}
        if not pending:
            return
        for task in pending:
            task.cancel()
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        for task in done:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
        if still_pending:
            names = ", ".join(sorted(task.get_name() for task in still_pending))
            logger.warning("Background tasks did not stop within {}s: {}", timeout, names)

    def reset(self) -> None:
        if self._tasks:
            raise RuntimeError("cannot reset a supervisor with active tasks")
        self._stopping = False
