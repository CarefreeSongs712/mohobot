"""A plugin's cancellation domain and unfinished cleanup remain observable."""

from __future__ import annotations

import asyncio

import pytest

from mohobot.services.task_supervisor import TaskSupervisor, TaskSupervisorClosed


def test_plugin_owner_tree_leaves_prefix_neighbor_running():
    async def run():
        supervisor = TaskSupervisor()

        async def idle():
            await asyncio.Event().wait()

        root = supervisor.create_task(idle(), name="root", owner="plugin:demo")
        child = supervisor.create_task(idle(), name="child", owner="plugin:demo:worker")
        neighbor = supervisor.create_task(idle(), name="neighbor", owner="plugin:demo_more")
        try:
            assert await supervisor.cancel_owner_tree("plugin:demo") == set()
            assert root.cancelled() and child.cancelled()
            assert not neighbor.done()
            assert await supervisor.cancel_owner_prefix("plugin:demo") == set()
            assert neighbor.cancelled()
        finally:
            await supervisor.shutdown()

    asyncio.run(run())


def test_shutdown_retains_pending_task_without_interrupting_cancellation_cleanup():
    async def run():
        supervisor = TaskSupervisor()
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release.wait()

        task = supervisor.create_task(worker(), name="worker", owner="plugin:demo:worker")
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            pending = await supervisor.shutdown(timeout=0)
            assert pending == {task}
            assert cleanup_started.is_set() and not task.done()
            assert task.cancelling() == 1
            with pytest.raises(RuntimeError, match="active tasks"):
                supervisor.reset()
            with pytest.raises(TaskSupervisorClosed):
                supervisor.create_task(worker(), name="rejected")
            assert await supervisor.cancel_owner_tree("plugin:demo", timeout=0) == {task}
            assert task.cancelling() == 1
            release.set()
            assert await supervisor.shutdown(timeout=5) == set()
            assert task.cancelled()
            supervisor.reset()
            assert not supervisor.stopping
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_cancelled_drain_waiter_keeps_task_tracked_for_retry():
    async def run():
        supervisor = TaskSupervisor()
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release.wait()

        task = supervisor.create_task(worker(), name="worker", owner="plugin:demo")
        drain = None
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            drain = asyncio.create_task(supervisor.cancel_owner("plugin:demo"))
            await asyncio.wait_for(cleanup_started.wait(), timeout=5)
            drain.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain
            assert await supervisor.cancel_owner("plugin:demo", timeout=0) == {task}
            assert task.cancelling() == 1
        finally:
            release.set()
            if drain is not None:
                await asyncio.gather(drain, return_exceptions=True)
            assert await supervisor.shutdown() == set()

    asyncio.run(run())


def test_supervised_drain_caller_is_returned_without_self_cancellation():
    async def run():
        supervisor = TaskSupervisor()

        async def stop_self():
            return await supervisor.shutdown(timeout=0)

        task = supervisor.create_task(stop_self(), name="caller")
        assert await asyncio.wait_for(task, timeout=5) == {task}
        assert not task.cancelled()
        assert await supervisor.shutdown() == set()

    asyncio.run(run())
