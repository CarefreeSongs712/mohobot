"""Process-wide song sync reservation shared by dynamically loaded plugin instances."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from typing import TypeVar

_T = TypeVar("_T")
_lock = Lock()
_active: SyncReservation | None = None


class SyncReservation:
    """Own the database selection until setup fails or the submitted worker exits."""

    def __init__(self) -> None:
        self._submitted = False
        self._finished: Future[None] = Future()

    def release_if_unsubmitted(self) -> None:
        # Called on the owner loop, including when its task never started.
        if not self._submitted:
            self._release()

    async def run(self, work: Callable[[], _T]) -> _T:
        def run_and_release() -> _T:
            try:
                return work()
            finally:
                self._release()

        worker = asyncio.get_running_loop().run_in_executor(None, run_and_release)
        self._submitted = True
        # The waiter can disappear before the executor starts or reports an error.
        # Shield the submission and always retrieve its outcome.
        worker.add_done_callback(_retrieve_result)
        return await asyncio.shield(worker)

    def _release(self) -> None:
        global _active
        with _lock:
            if _active is not self:
                return
            _active = None
        self._finished.set_result(None)


def _retrieve_result(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


def try_reserve() -> SyncReservation | None:
    global _active
    with _lock:
        if _active is not None:
            return None
        _active = SyncReservation()
        return _active


async def wait_until_idle(timeout: float) -> bool:
    """Wait for the current reservation after closing new sync admission."""
    with _lock:
        active = _active
    if active is None:
        return True
    finished = asyncio.wrap_future(active._finished)
    done, _ = await asyncio.wait({finished}, timeout=timeout)
    return bool(done)
