"""Song-sync ownership through reload, submit failure, and worker failure."""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from _test_helpers import patched as _patched

from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.music_knowledge import sync
from mohobot.services.task_supervisor import TaskSupervisor, TaskSupervisorClosed

_TIMEOUT = 5


class _Supervisor(TaskSupervisor):
    def __init__(self) -> None:
        super().__init__()
        self.workers: list[asyncio.Task] = []

    def create_task(self, coro, *, name, owner="application"):
        task = super().create_task(coro, name=name, owner=owner)
        if name == "song-sync":
            self.workers.append(task)
        return task


async def _load(tmp_path, supervisor):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    source = Path.cwd() / "plugins" / "song_sync.py"
    shutil.copyfile(source, plugins / source.name)
    system = PluginSystem(str(plugins), str(tmp_path / "data"))
    system.set_task_supervisor(supervisor)
    assert await system.load_plugins() == 1
    return system


def _configure(system, folder):
    plugin = system._plugins[0]["instance"]
    plugin._config = SimpleNamespace(
        admins=[1],
        music_knowledge={
            "song_database": {"db_folder": str(folder), "db_file": "fake.db"},
            "sync": {"enabled": True},
        },
    )
    plugin._matcher = Mock()
    plugin._send_event = AsyncMock()
    # These spies stand in for checks that can initialize the shared pool.
    plugin._due = Mock(return_value=True)
    plugin._status_text = Mock(return_value="status")
    return plugin


async def _command(plugin, args="fill"):
    event = SimpleNamespace(user_id=1, message=f"/sync-songs {args}")
    handled, reply = await plugin.on_message("test-bot", event, {})
    assert handled
    return reply


def test_cancelled_waiter_retains_database_reservation_through_reload(tmp_path):
    change_db = Mock()
    adapters = Mock(return_value=[object()])
    started = asyncio.Event()
    unblock = threading.Event()
    exited = threading.Event()
    loop_holder: list[asyncio.AbstractEventLoop] = []

    def worker(_adapters, *, mode, limit):
        if mode == "fill_missing":
            loop_holder[0].call_soon_threadsafe(started.set)
            assert unblock.wait(_TIMEOUT * 3)
            exited.set()
        return SimpleNamespace(summary_text=lambda: "fake report")

    async def run():
        loop_holder.append(asyncio.get_running_loop())

        supervisor = _Supervisor()
        system = await _load(tmp_path, supervisor)
        try:
            old = _configure(system, tmp_path / "old")
            assert "已开始同步" in await _command(old)
            await asyncio.wait_for(started.wait(), _TIMEOUT)
            first, = supervisor.workers
            assert await asyncio.wait_for(system.reload_plugin("song_sync"), _TIMEOUT)
            assert first.cancelled() and not exited.is_set()
            current = _configure(system, tmp_path / "current")
            assert current is not old

            assert "已有同步任务" in await _command(current, "refresh")
            assert "已有同步任务" in await _command(current, "status")
            await current.on_tick()
            current._status_text.assert_not_called()
            current._due.assert_not_called()
            assert supervisor.workers == [first]
            change_db.assert_called_once_with(str(tmp_path / "old"), "fake.db")
            adapters.assert_called_once()

            from mohobot.music_knowledge.sync_worker import wait_until_idle

            assert not await wait_until_idle(timeout=0)
            unblock.set()
            assert await wait_until_idle(timeout=_TIMEOUT)
            assert exited.is_set()
            assert await _command(current, "status") == "status"
            assert "已开始同步" in await _command(current, "refresh")
            await asyncio.wait_for(supervisor.workers[-1], _TIMEOUT)
            assert change_db.call_count == 2
            change_db.assert_called_with(str(tmp_path / "current"), "fake.db")
            old._matcher.request_rebuild.assert_not_called()
            old._send_event.assert_not_awaited()
            current._matcher.request_rebuild.assert_called_once()
            current._send_event.assert_awaited_once()
        finally:
            unblock.set()
            await system.shutdown_plugins()
            await supervisor.shutdown()

    with _patched(
        (sync, "set_db_location", change_db),
        (sync, "build_adapters", adapters),
        (sync, "sync_all", worker),
    ):
        asyncio.run(run())


def test_sync_reserves_at_command_and_prestart_cancellation_allows_retry(tmp_path):
    change_db = Mock()

    async def run():
        supervisor = _Supervisor()
        system = await _load(tmp_path, supervisor)
        try:
            plugin = _configure(system, tmp_path / "db")
            assert "已开始同步" in await _command(plugin)
            assert "已有同步任务" in await _command(plugin)
            first, = supervisor.workers
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            change_db.assert_not_called()

            for _ in range(2):
                assert "已开始同步" in await _command(plugin)
                await asyncio.wait_for(supervisor.workers[-1], _TIMEOUT)
                assert "fake worker error" in plugin._send_event.await_args.args[-1]
            assert change_db.call_count == 2
        finally:
            await system.shutdown_plugins()
            await supervisor.shutdown()

    with _patched(
        (sync, "set_db_location", change_db),
        (sync, "build_adapters", Mock(return_value=[object()])),
        (sync, "sync_all", Mock(side_effect=RuntimeError("fake worker error"))),
    ):
        asyncio.run(run())


def test_sync_task_rejection_and_failed_due_check_release_reservation(tmp_path):
    async def run():
        supervisor = _Supervisor()
        system = await _load(tmp_path, supervisor)
        try:
            plugin = _configure(system, tmp_path / "db")
            plugin._due.side_effect = ValueError("fake due check failure")
            with pytest.raises(ValueError, match="fake due check failure"):
                await plugin.on_tick()
            with _patched(
                (supervisor, "create_task", Mock(
                    side_effect=TaskSupervisorClosed("rejected"),
                )),
            ):
                with pytest.raises(TaskSupervisorClosed):
                    await _command(plugin)
            plugin._due.side_effect = None
            await plugin.on_tick()
            task, = supervisor.workers
            await asyncio.wait_for(task, _TIMEOUT)
            assert "已开始同步" in await _command(plugin)
            await asyncio.wait_for(supervisor.workers[-1], _TIMEOUT)
        finally:
            await system.shutdown_plugins()
            await supervisor.shutdown()

    with _patched(
        (sync, "set_db_location", Mock()),
        (sync, "build_adapters", Mock(return_value=[])),
    ):
        asyncio.run(run())
