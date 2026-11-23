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

    async def cancel_owner(self, owner: str, timeout: float = 5.0) -> set[asyncio.Task[Any]]:
        """Cancel tasks of one exact owner and return any that are still running."""
        return await self._cancel_tasks(set(self._owners.get(owner, ())), timeout)

    async def cancel_owner_prefix(
        self, prefix: str, timeout: float = 5.0,
    ) -> set[asyncio.Task[Any]]:
        """Cancel owners matching the raw string prefix and return unfinished tasks."""
        tasks: set[asyncio.Task[Any]] = set()
        for owner, owned in self._owners.items():
            if owner.startswith(prefix):
                tasks.update(owned)
        return await self._cancel_tasks(tasks, timeout)

    async def cancel_owner_tree(self, owner: str, timeout: float = 5.0) -> set[asyncio.Task[Any]]:
        """Cancel one owner and its colon-separated descendants in a single drain."""
        tasks = {
            task
            for key, owned in self._owners.items()
            if key == owner or key.startswith(f"{owner}:")
            for task in owned
        }
        return await self._cancel_tasks(tasks, timeout)

    async def shutdown(self, timeout: float = 10.0) -> set[asyncio.Task[Any]]:
        """Close submission and drain once, retaining ownership until each task exits."""
        self._stopping = True
        return await self._cancel_tasks(set(self._tasks), timeout)

    async def _cancel_tasks(
        self, tasks: set[asyncio.Task[Any]], timeout: float,
    ) -> set[asyncio.Task[Any]]:
        current = asyncio.current_task()
        pending = {task for task in tasks if task is not current and not task.done()}
        for task in pending:
            if not task.cancelling():
                # A retry must not interrupt cancellation cleanup already in progress.
                task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=timeout)
        if pending:
            names = ", ".join(sorted(task.get_name() for task in pending))
            logger.warning("Background tasks did not stop within {}s: {}", timeout, names)
        if current is not None and current in tasks:
            # A supervised caller cannot await its own exit.
            pending.add(current)
        return pending

    def reset(self) -> None:
        if self._tasks:
            raise RuntimeError("cannot reset a supervisor with active tasks")
        self._stopping = False
