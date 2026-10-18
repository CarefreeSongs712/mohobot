"""出站取消语义回归 — 调度器与传输层两层。

覆盖 Issue #3。

缺陷：``OutboundScheduler.submit`` 里 ``return await future`` 被取消后，排队项仍留在
队列中；worker 取出、做完限速等待、**照常调用 operation** —— 调用方以为已经放弃
(例如 A/B 15 秒发送超时)，用户却收到一条过时消息。传输层同样有问题：
``_send_tracked`` 只捕获 ``Exception``，而 ``asyncio.CancelledError`` 在 3.8+ 是
``BaseException``，于是取消时既不清理 ``_pending_sent``/回执 future，也不会派发归档
任务 —— 消息真发出去了，history 里却没有。

本文件用真实 OutboundScheduler / ReplySender / WSServer / BotManager，只把底层
socket 边界(``_send_guarded``)与归档派发换成可观测的替身。
"""
import asyncio
from pathlib import Path
import sys
import tempfile
import time

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mohobot.services.outbound import DispatchAttempt, OutboundScheduler  # noqa: E402


def _flaky_await(awaitable):
    """辅助: 断言协程对象可等待(避免 never-awaited 警告)。"""
    awaitable.close()
    return True


@pytest.mark.asyncio
async def test_cancelled_while_queued_is_not_sent():
    """排队中被取消: worker 必须直接丢弃, 不得发送。"""
    scheduler = OutboundScheduler(send_interval_sec=0.0)
    calls: list[str] = []
    gate = asyncio.Event()

    async def first():
        calls.append("first")
        await gate.wait()
        return "first"

    async def second():
        calls.append("second")
        return "second"

    try:
        running = asyncio.create_task(scheduler.submit("bot_001", first, label="first"))
        await asyncio.sleep(0.05)          # 让第一个真正进入派发
        attempt = DispatchAttempt()
        queued = asyncio.create_task(
            scheduler.submit("bot_001", second, label="second", attempt=attempt)
        )
        await asyncio.sleep(0.05)          # 第二个已入队且仍在等待
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued

        gate.set()
        assert await running == "first"
        await asyncio.sleep(0.05)
        assert calls == ["first"], f"被取消的排队项仍被发送: {calls}"
        assert attempt.dispatched is False
    finally:
        await scheduler.close(drain_timeout=0.5)


@pytest.mark.asyncio
async def test_cancelled_during_rate_limit_wait_is_not_sent():
    """限速等待期间被取消: 同样不得发送。

    间隔取生产默认 0.5s, 取消后等足 1.0s 再判定 —— 若用长间隔加短 drain_timeout,
    worker 会在醒来发送之前就被 close() 杀掉, 这条用例即使实现有 bug 也会通过。
    """
    scheduler = OutboundScheduler(send_interval_sec=0.5)
    calls: list[str] = []

    async def first():
        calls.append("first")
        return "first"

    async def second():
        calls.append("second")
        return "second"

    try:
        assert await scheduler.submit("bot_001", first, label="first") == "first"
        attempt = DispatchAttempt()
        queued = asyncio.create_task(
            scheduler.submit("bot_001", second, label="second", attempt=attempt)
        )
        await asyncio.sleep(0.1)           # worker 已取出, 正在限速 sleep
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        await asyncio.sleep(1.0)           # 远超间隔: 若仍发送, 这里必然发生
        assert calls == ["first"], f"限速期间被取消的项仍被发送: {calls}"
        assert attempt.dispatched is False
    finally:
        await scheduler.close(drain_timeout=0.5)


@pytest.mark.asyncio
async def test_full_queue_fails_fast_with_outbound_queue_full():
    """队列满 → 立刻 OutboundQueueFullError, 不等待、不改写队列状态。

    这是 submit() 里唯一的快速失败判定点（#28 删掉前置 queue.full() 预检后,
    put_nowait 的 QueueFull 是唯一出口), 之前全仓没有用例覆盖它。
    """
    from mohobot.services.outbound import OutboundQueueFullError

    scheduler = OutboundScheduler(send_interval_sec=0.0, queue_maxsize=1)
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return "sent"

    first = asyncio.create_task(scheduler.submit("bot_001", slow))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(scheduler.submit("bot_001", slow))  # 占满容量 1
    await asyncio.sleep(0.05)

    start = time.monotonic()
    with pytest.raises(OutboundQueueFullError) as excinfo:
        await scheduler.submit("bot_001", slow)
    elapsed = time.monotonic() - start

    try:
        assert elapsed < 0.1, f"队列满应立即失败, 实际 {elapsed:.3f}s"
        assert "queue full" in str(excinfo.value)
        # 失败项没有入库: 队列仍只有原先那一条
        assert scheduler._inflight_count("bot_001") == 1
    finally:
        gate.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await scheduler.close(drain_timeout=0.5)


@pytest.mark.asyncio
async def test_cancelled_after_dispatch_is_reported_to_caller():
    """已进入传输层的取消无法撤回, 但必须让调用方知道"已派发"。"""
    scheduler = OutboundScheduler(send_interval_sec=0.0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        entered.set()
        await release.wait()
        return "sent"

    try:
        attempt = DispatchAttempt()
        running = asyncio.create_task(
            scheduler.submit("bot_001", slow, label="slow", attempt=attempt)
        )
        await entered.wait()
        assert attempt.dispatched is True, "operation 已开始执行, 必须置位"

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        release.set()
        await asyncio.sleep(0.05)
        await scheduler.close(drain_timeout=0.5)


def _make_server(folder, *, interval=0.0):
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.models.config import BotConfig
    from mohobot.ws_server import WSServer

    manager = BotManager(str(folder))
    manager._bots["bot_001"] = BotInstance("bot_001", None, BotConfig(bot_id="bot_001", qq=111))
    server = WSServer(manager, data_dir=str(folder), outbound_interval=interval)
    spawned: list = []
    server._spawn_archive_task = lambda coro: (_flaky_await(coro), spawned.append(1))
    return server, manager, spawned


@pytest.mark.asyncio
async def test_send_cancelled_before_dispatch_leaves_no_tracking_and_no_archive():
    """未派发就被取消: 追踪必须清理干净, 且不得产生归档(消息没发出去)。"""
    with tempfile.TemporaryDirectory() as td:
        server, manager, spawned = _make_server(td)
        gate = asyncio.Event()

        async def blocking(instance, payload):
            await gate.wait()
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        server._send_guarded = blocking
        try:
            first = asyncio.create_task(server.send_group_msg("bot_001", 333, "first"))
            await asyncio.sleep(0.05)                 # 占住调度器
            before = dict(manager._pending_sent)

            second = asyncio.create_task(server.send_group_msg("bot_001", 333, "second"))
            await asyncio.sleep(0.05)                 # 已入队
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second

            assert len(manager._pending_sent) == len(before), "取消后不得留下残留追踪"
            assert spawned == [], "未派发不得创建归档任务"

            gate.set()
            await first
            await asyncio.sleep(0.05)
        finally:
            await server._outbound_scheduler.close(drain_timeout=0.5)


@pytest.mark.asyncio
async def test_send_cancelled_after_dispatch_transfers_archive_ownership():
    """已派发就被取消: 消息可能已发出, 归档所有权必须转交后台。"""
    with tempfile.TemporaryDirectory() as td:
        server, manager, spawned = _make_server(td)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow(instance, payload):
            entered.set()
            await release.wait()
            return {"status": "ok", "retcode": 0, "data": {"message_id": 2}}

        server._send_guarded = slow
        try:
            sending = asyncio.create_task(server.send_group_msg("bot_001", 333, "hello"))
            await entered.wait()
            sending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sending
            assert spawned == [1], "已派发时归档所有权应转交后台任务"
        finally:
            release.set()
            await asyncio.sleep(0.05)
            await server._outbound_scheduler.close(drain_timeout=0.5)


def main():
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            result = func()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
    print("\nALL OUTBOUND CANCELLATION TESTS PASSED")


if __name__ == "__main__":
    main()
