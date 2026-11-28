"""Final socket-lock TTS revocation and bounded image-cache fallback."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotManager
from mohobot.file_store import _get_lock, json_write
from mohobot.image_cache import ImageCache
from mohobot.llm_service import LLMService
from mohobot.models.config import GlobalConfig, TTSConfig
from mohobot.services.task_supervisor import TaskSupervisor
from mohobot.services.tts import TTSJob, TTSService
from mohobot.ws_server import WSServer


class _ObservedLock(asyncio.Lock):
    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.waiting.set()
        return await super().acquire()


async def _socket_lock_case(change, cancel=False):
    with tempfile.TemporaryDirectory() as td:
        manager = BotManager(td)
        cfg = manager.create_bot(qq=11001)
        supervisor = TaskSupervisor()
        server = WSServer(manager, outbound_interval=0, data_dir=td, task_supervisor=supervisor)
        server._ARCHIVE_ECHO_TIMEOUT = 0.02
        server._global_config = GlobalConfig()
        entered, release = asyncio.Event(), asyncio.Event()
        wire = []

        class Socket:
            async def send(self, raw):
                payload = json.loads(raw)
                if payload["action"] == "get_login_info":
                    entered.set()
                    await release.wait()
                wire.append(payload)
                if "echo" in payload:
                    await manager.handle_api_response(cfg.bot_id, {
                        "echo": payload["echo"], "status": "ok", "retcode": 0,
                        "data": {"message_id": len(wire)},
                    })

        instance = manager.register(11001, Socket())
        instance._send_lock = _ObservedLock()
        tts_cfg = TTSConfig(enabled=True, backend="http", api_key="test", convert_to_mp3=False)
        service = TTSService(tts_cfg)
        service.set_ws(server)

        async def synthesize(text, voice_id=None):
            return b"local-audio", len(text)

        service._client.synthesize = synthesize
        blocker = asyncio.create_task(server.send_to_bot(cfg.bot_id, "get_login_info"))
        process = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            job = TTSJob(cfg.bot_id, "group", "70001", "revoked audio")
            process = asyncio.create_task(service._process(job))
            await asyncio.wait_for(instance._send_lock.waiting.wait(), 2)
            assert manager._pending_sent, "TTS must already have entered tracked dispatch"
            if change == "disable":
                service.sync_config(replace(tts_cfg, enabled=False))
                assert service._job_is_stale(job)
            elif change == "exclusive_group":
                server._global_config.external_service_groups = [70001]
            elif change == "stale":
                instance.stale = True
            elif change == "event_only":
                instance.client_role = "Event"
            elif change == "bot_disabled":
                instance.config.enabled = False
            if cancel:
                process.cancel()
                await asyncio.gather(process, return_exceptions=True)
                assert process.cancelled()
                # More than archive timeout before releasing the socket lock:
                # unsent work must never be handed to the archive fallback.
                await asyncio.sleep(0.05)
                assert not (Path(td) / "history").exists()
            release.set()
            await asyncio.wait_for(asyncio.gather(blocker, process, return_exceptions=True), 2)
            await asyncio.wait_for(server._outbound_scheduler._queues[cfg.bot_id].join(), 2)
            assert [item["action"] for item in wire] == ["get_login_info"]
            assert service.stats["done"] == 0
            if not cancel:
                key = "failed" if change in ("stale", "event_only") else "dropped"
                assert service.stats[key] == 1
            assert not manager._pending_sent and not manager._pending_responses
            assert not any(server._sent_claims._claims.values())
            assert not (Path(td) / "history").exists()
            instance.stale = False
            instance.client_role = "Universal"
            instance.config.enabled = True
            server._global_config.external_service_groups = []
            service.sync_config(replace(tts_cfg, enabled=True))
            fresh = TTSJob(cfg.bot_id, "group", "70001", "fresh audio",
                           generation=service._config_generation)
            await service._process(fresh)
            assert [item["action"] for item in wire] == ["get_login_info", "send_group_msg"]
            assert service.stats["done"] == 1
        finally:
            release.set()
            await asyncio.gather(blocker, *([process] if process else []), return_exceptions=True)
            await service.stop()
            # Finish real archive work before closing writers / removing temporary data.
            async with asyncio.timeout(2):
                while supervisor._tasks:
                    await asyncio.sleep(0.01)
            await supervisor.shutdown()
            await server.stop()


async def test_tts_disabled_after_dispatch_guard_waiting_for_socket_lock():
    await _socket_lock_case("disable")


async def test_exclusive_group_added_while_waiting_for_socket_lock():
    await _socket_lock_case("exclusive_group")


async def test_connection_stale_or_event_only_while_waiting_for_socket_lock():
    for change in ("stale", "event_only"):
        await _socket_lock_case(change)


async def test_bot_disabled_while_waiting_for_socket_lock():
    await _socket_lock_case("bot_disabled")


async def test_caller_cancel_before_socket_dispatch_never_creates_phantom_archive():
    for change in ("disable", "exclusive_group", "bot_disabled", "unchanged"):
        await _socket_lock_case(change, cancel=True)


@asynccontextmanager
async def _held_transport(hold_action):
    with tempfile.TemporaryDirectory() as td:
        manager = BotManager(td)
        cfg = manager.create_bot(qq=11001)
        supervisor = TaskSupervisor()
        server = WSServer(manager, outbound_interval=0, data_dir=td, task_supervisor=supervisor)
        entered, release = asyncio.Event(), asyncio.Event()
        wire = []

        class Socket:
            async def send(self, raw):
                payload = json.loads(raw)
                if payload["action"] == hold_action:
                    entered.set()
                    await release.wait()
                wire.append(payload)
                if "echo" in payload:
                    await manager.handle_api_response(cfg.bot_id, {
                        "echo": payload["echo"], "status": "ok", "retcode": 0,
                        "data": {"message_id": len(wire)},
                    })

        instance = manager.register(11001, Socket())
        instance._send_lock = _ObservedLock()
        try:
            yield manager, server, cfg, instance, entered, release, wire, supervisor, Path(td)
        finally:
            release.set()
            await server._outbound_scheduler.close()
            async with asyncio.timeout(2):
                while supervisor._tasks:
                    await asyncio.sleep(0.01)
            await supervisor.shutdown()
            await server.stop()


async def test_cancelled_dispatch_cleanup_keeps_new_identical_message_claim():
    async with _held_transport("get_login_info") as runtime:
        manager, server, cfg, instance, entered, release, wire, supervisor, folder = runtime
        blocker = asyncio.create_task(server.send_to_bot(cfg.bot_id, "get_login_info"))
        first = second = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            first = asyncio.create_task(server.send_message_with_status(
                cfg.bot_id, "group", 70001, "same payload", ack_timeout=0,
            ))
            await asyncio.wait_for(instance._send_lock.waiting.wait(), 2)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            assert first.cancelled() and not manager._pending_sent
            second = asyncio.create_task(server.send_message_with_status(
                cfg.bot_id, "group", 70001, "same payload", ack_timeout=0,
            ))
            async with asyncio.timeout(2):
                while not manager._pending_sent:
                    await asyncio.sleep(0)
            assert any(server._sent_claims._claims.values())
            release.set()
            await asyncio.wait_for(asyncio.gather(blocker, second), 2)
            await asyncio.wait_for(server._outbound_scheduler._queues[cfg.bot_id].join(), 2)
            assert [item["action"] for item in wire] == ["get_login_info", "send_group_msg"]
            assert any(server._sent_claims._claims.values()), "A cleanup removed B's newer claim"
            assert not manager._pending_sent and not manager._pending_responses
        finally:
            release.set()
            await asyncio.gather(blocker, *[task for task in (first, second) if task], return_exceptions=True)


async def test_cancel_after_socket_start_archives_one_successful_echo():
    async with _held_transport("send_group_msg") as runtime:
        manager, server, cfg, instance, entered, release, wire, supervisor, folder = runtime
        sending = asyncio.create_task(server.send_message_with_status(
            cfg.bot_id, "group", 70001, "already dispatching", ack_timeout=1,
        ))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
            assert sending.cancelled() and len(supervisor._tasks) == 1
            release.set()
            await asyncio.wait_for(server._outbound_scheduler._queues[cfg.bot_id].join(), 2)
            async with asyncio.timeout(2):
                while supervisor._tasks:
                    await asyncio.sleep(0.01)
            lines = (folder / "history" / "group" / "70001.jsonl").read_text(encoding="utf-8").splitlines()
            assert len(wire) == 1 and len(lines) == 1
            assert str(json.loads(lines[0])["message_id"]) == "1"
            assert not manager._pending_sent and not manager._pending_responses
            instance.config.enabled = False
            await server.send_to_bot(cfg.bot_id, "get_login_info")
            assert wire[-1]["action"] == "get_login_info", "disabled bot queries remain available"
            assert await instance.send({"action": "get_status", "params": {}}) is None
        finally:
            release.set()
            await asyncio.gather(sending, return_exceptions=True)


async def test_image_timeout_cache_peek_does_not_wait_indefinitely_for_path_lock():
    with tempfile.TemporaryDirectory() as td:
        cache = ImageCache(td)
        url = "https://example.invalid/held-cache"
        await json_write(cache._map_path, {url: {"description": "cached cat"}})
        lock = _get_lock(str(cache._map_path.absolute()))
        service = LLMService.__new__(LLMService)
        service._vision_available = True
        service._image_cache = cache
        service._vision_callback = lambda config: None
        described, peeked = asyncio.Event(), asyncio.Event()
        original_peek = cache.peek_description

        async def held_describe(*args, **kwargs):
            described.set()
            await asyncio.Event().wait()

        async def observed_peek(image_url):
            peeked.set()
            return await original_peek(image_url)

        cache.get_or_describe = held_describe
        cache.peek_description = observed_peek
        await lock.acquire()
        task = None
        try:
            with patch("mohobot.llm_service._IMAGE_DESCRIBE_TIMEOUT", 0.01):
                task = asyncio.create_task(service._describe_image_for_text(url))
                await asyncio.wait_for(described.wait(), 1)
                await asyncio.wait_for(peeked.wait(), 1)
                assert await asyncio.wait_for(asyncio.shield(task), 0.5) == ""
            assert lock.locked(), "fallback must finish before competing writer releases its lock"
        finally:
            lock.release()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await cache.close()
