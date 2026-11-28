"""Real archive writers must not reopen retired paths after maintenance."""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from mohobot.file_store import JSONLWriter
from mohobot.message_handler import MessageHandler
from mohobot.models.onebot import PrivateMessageEvent
from mohobot.services.history_barrier import HistoryBarrier
from mohobot.ws_server import WSServer


def _archive_owner(root, outbound):
    barrier = HistoryBarrier()
    if outbound:
        owner = object.__new__(WSServer)
        owner._data_dir = str(root)
        owner._archive_writers = {}
        owner._history_barrier = None
        owner._global_config = None
        owner._bot_manager = SimpleNamespace(get=lambda bot_id: None)
        owner.set_history_barrier(barrier)
        registry = owner._archive_writers
        path = root / "history" / "group" / "999.jsonl"

        async def archive(label):
            await owner._write_archive_line("bot_001", "group", "999", label, [], "llm")
    else:
        owner = object.__new__(MessageHandler)
        owner._data_dir = str(root)
        owner._writer_registry = {}
        owner._history_barrier = None
        owner._global_config = None
        owner.set_history_barrier(barrier)
        registry = owner._writer_registry
        path = root / "history" / "bot_001" / "private" / "1001.jsonl"
        event = PrivateMessageEvent.from_dict({
            "time": 1, "self_id": 111, "post_type": "message",
            "message_type": "private", "sub_type": "friend", "message_id": 1,
            "user_id": 1001, "message": [], "sender": {"user_id": 1001},
        })

        async def archive(label):
            await owner._archive_event("bot_001", event, {"message_id": label})

    return barrier, registry, path, archive


async def _check_queued_archive_retired(outbound, cancel_freeze=False, timeout_freeze=False):
    with tempfile.TemporaryDirectory() as directory:
        barrier, registry, path, archive = _archive_owner(Path(directory), outbound)
        await archive("initial")
        writer = next(iter(registry.values()))
        entered, closing = asyncio.Event(), asyncio.Event()
        append, close = writer.append, writer.close

        async def observed_append(data):
            entered.set()
            return await append(data)

        async def observed_close():
            closing.set()
            return await close()

        writer.append, writer.close = observed_append, observed_close
        await writer._lock.acquire()
        stale = asyncio.create_task(archive("stale"))
        await asyncio.wait_for(entered.wait(), 2)

        async def maintain():
            await barrier.freeze()
            if not (cancel_freeze or timeout_freeze):
                path.unlink()
            await barrier.thaw()

        work = asyncio.wait_for(maintain(), 0.05) if timeout_freeze else maintain()
        maintenance = asyncio.create_task(work)
        try:
            await asyncio.wait_for(closing.wait(), 2)
            if cancel_freeze:
                maintenance.cancel()
            # Real append is already waiting on the file lock. Freeze must
            # retire it and await drain, rather than finish and let it reopen.
            done, _ = await asyncio.wait({maintenance}, timeout=0.1)
            finished_before_drain = bool(done)
            writer._lock.release()
            if timeout_freeze:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(maintenance, 3)
            elif cancel_freeze:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(maintenance, 3)
            else:
                await asyncio.wait_for(maintenance, 3)
            await asyncio.wait_for(stale, 3)
            assert not finished_before_drain
            assert not barrier.frozen
            if cancel_freeze or timeout_freeze:
                records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                assert [row["message_id"] for row in records] == ["initial"]
            else:
                assert not path.exists(), "a pre-freeze append recreated cleaned history"
            assert registry == {}
            assert writer._file is None or writer._file.closed

            await archive("fresh")
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            assert records[-1]["message_id"] == "fresh"
            assert all(row["message_id"] != "stale" for row in records)
            assert next(iter(registry.values())) is not writer
        finally:
            if writer._lock.locked():
                writer._lock.release()
            if not maintenance.done():
                maintenance.cancel()
            await asyncio.gather(stale, maintenance, return_exceptions=True)
            await barrier.thaw()
            await writer.close()
            for current in registry.values():
                await current.close()


async def test_inbound_waiting_archive_cannot_cross_freeze_and_thaw():
    await _check_queued_archive_retired(False)


async def test_outbound_waiting_archive_cannot_cross_freeze_and_thaw():
    await _check_queued_archive_retired(True)


async def test_cancelled_inbound_freeze_drains_retired_writer_and_recovers():
    await _check_queued_archive_retired(False, cancel_freeze=True)


async def test_cancelled_outbound_freeze_drains_retired_writer_and_recovers():
    await _check_queued_archive_retired(True, cancel_freeze=True)


async def test_timed_out_freeze_drains_retired_writer_before_recovery():
    await _check_queued_archive_retired(False, timeout_freeze=True)


async def test_ordinary_jsonl_close_remains_reusable():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "usage.jsonl"
        writer = JSONLWriter(path)
        try:
            await writer.append({"value": "before"})
            await writer.close()
            await writer.append_batch([{"value": "after"}])
            await writer.close()
            assert [json.loads(line)["value"] for line in path.read_text(encoding="utf-8").splitlines()] == [
                "before", "after",
            ]
        finally:
            await writer.close()


async def test_retirement_fences_queued_batch_and_later_appends():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "history.jsonl"
        writer = JSONLWriter(path)
        await writer.append({"value": "initial"})
        await writer._lock.acquire()
        queued = asyncio.create_task(writer.append_batch([{"value": "stale"}]))
        try:
            await asyncio.sleep(0)
            writer.retire()
            closing = asyncio.create_task(writer.close())
            writer._lock.release()
            await asyncio.gather(queued, closing)
            assert [json.loads(line)["value"] for line in path.read_text(encoding="utf-8").splitlines()] == [
                "initial",
            ]
            path.unlink()
            await writer.append({"value": "late"})
            await writer.append_batch([{"value": "late batch"}])
            assert not path.exists()
        finally:
            if writer._lock.locked():
                writer._lock.release()
            await queued
            await writer.close()


async def test_cancelled_freeze_retires_all_cached_writers_before_waiting():
    with tempfile.TemporaryDirectory() as directory:
        barrier, registry, _path, archive = _archive_owner(Path(directory), False)
        await archive("initial")
        first = next(iter(registry.values()))
        second_path = Path(directory) / "history" / "second.jsonl"
        second = registry[str(second_path)] = JSONLWriter(second_path)
        await second.append({"message_id": "second initial"})
        closing = asyncio.Event()
        close_first = first.close

        async def observed_close():
            closing.set()
            await close_first()

        first.close = observed_close
        await first._lock.acquire()
        await second._lock.acquire()
        queued = asyncio.create_task(second.append({"message_id": "stale"}))
        await asyncio.sleep(0)
        freeze = asyncio.create_task(barrier.freeze())
        try:
            await asyncio.wait_for(closing.wait(), 2)
            freeze.cancel()
            # The second writer must already be fenced while first.close is
            # blocked/cancelled; otherwise its waiting append can slip through.
            second._lock.release()
            await asyncio.wait_for(queued, 2)
            first._lock.release()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(freeze, 3)
            assert registry == {} and not barrier.frozen
            assert first._file is None and second._file is None
            assert [json.loads(line)["message_id"] for line in second_path.read_text(encoding="utf-8").splitlines()] == [
                "second initial",
            ]
        finally:
            for writer in (first, second):
                if writer._lock.locked():
                    writer._lock.release()
            if not freeze.done():
                freeze.cancel()
            await asyncio.gather(queued, freeze, return_exceptions=True)
            await barrier.thaw()
            await first.close()
            await second.close()
