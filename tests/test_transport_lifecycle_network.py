import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys

from aiohttp import web
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotManager
from mohobot.history_dedup import group_message_fingerprint
from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.models.config import TTSConfig
from mohobot.models.onebot import Event
from mohobot.services.task_supervisor import TaskSupervisor
from mohobot.services.tts import TTSJob, TTSService
from mohobot.ws_server import WSServer


async def _until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.005)


@asynccontextmanager
async def _runtime(path, qqs=(11001,)):
    manager = BotManager(str(path / "data"))
    configs = [manager.create_bot(qq=qq) for qq in qqs]
    supervisor = TaskSupervisor()
    server = WSServer(manager, host="127.0.0.1", port=0, outbound_interval=0,
                      data_dir=str(path / "data"), task_supervisor=supervisor)
    await server.start()
    url = f"ws://127.0.0.1:{server._server.sockets[0].getsockname()[1]}"
    clients, tasks, received = [], [], []

    async def adapter(client):
        async for raw in client:
            payload = json.loads(raw)
            received.append(payload)
            with (path / "wire.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            if "echo" in payload:
                await client.send(json.dumps({
                    "status": "ok", "retcode": 0, "echo": payload["echo"],
                    "data": {"message_id": len(received), "card": "local", "nickname": "local"},
                }))

    async def connection(qq, role="Universal", receive=True):
        client = await connect(url, additional_headers={"X-Self-ID": str(qq), "X-Client-Role": role})
        clients.append(client)
        await _until(lambda: manager._connections.get(str(qq), {}).get(role) is not None)
        if receive:
            tasks.append(asyncio.create_task(adapter(client)))
        return client

    try:
        yield manager, server, configs, connection, received
    finally:
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
        await asyncio.gather(*tasks, return_exceptions=True)
        await supervisor.shutdown()
        await server.stop()


def _event(qq, message_id, *, text="占卜", group_id=70001, timestamp=100, at=None):
    message = [{"type": "text", "data": {"text": text}}]
    if at is not None:
        message.insert(0, {"type": "at", "data": {"qq": str(at)}})
    return {"time": timestamp, "self_id": qq, "post_type": "message",
            "message_type": "group", "sub_type": "normal", "message_id": message_id,
            "user_id": 80001, "group_id": group_id, "message": message,
            "raw_message": text, "sender": {"user_id": 80001, "nickname": "local"}}


async def test_event_api_roles_coexist_and_replace_independently(tmp_path):
    for order in (("Event", "API"), ("API", "Event")):
        path = tmp_path / order[0]
        path.mkdir()
        async with _runtime(path) as (manager, server, configs, connection, received):
            await connection(11001, order[0], receive=order[0] == "API")
            await connection(11001, order[1], receive=order[1] == "API")
            pair = dict(manager._connections["11001"])
            assert all(manager.is_current(instance) for instance in pair.values())
            assert manager.get(configs[0].bot_id) is pair["API"]
            deliveries = asyncio.Queue()

            async def callback(bot_id, event, raw):
                deliveries.put_nowait(raw["message_id"])

            server.set_event_callback(callback)
            await server._dispatch(pair["Event"], _event(11001, 1))
            assert await asyncio.wait_for(deliveries.get(), 5) == 1
            result = await server.send_message_with_status(configs[0].bot_id, "group", 70001, "hello", ack_timeout=2)
            assert result.status == "sent"
            assert received[-1]["action"] == "send_group_msg"
            pair["API"].config = replace(pair["API"].config, enabled=False)
            assert pair["Event"].config is pair["API"].config
            await server._dispatch(pair["Event"], _event(11001, 2))
            await asyncio.sleep(0)
            assert deliveries.empty()
            assert not manager._is_eligible(configs[0].bot_id)
            pair["API"].config = replace(pair["API"].config, enabled=True)
            assert manager._is_eligible(configs[0].bot_id)
            # The Event socket has no API receiver; a wrong route times out above.
            event_client = await connection(11001, "Event", receive=False)
            await _until(lambda: manager._connections["11001"]["Event"] is not pair["Event"])
            assert pair["Event"].stale and not pair["API"].stale
            manager.unregister(pair["Event"])
            assert manager.get(configs[0].bot_id) is pair["API"]
            await connection(11001, "API")
            await _until(lambda: manager._connections["11001"]["API"] is not pair["API"])
            assert pair["API"].stale
            assert manager.is_current(manager._connections["11001"]["Event"])
            result = await server.send_message_with_status(configs[0].bot_id, "private", 80001, "new API", ack_timeout=2)
            assert result.status == "sent"
            await event_client.close()
            await _until(lambda: "Event" not in manager._connections["11001"])
            assert not manager._is_eligible(configs[0].bot_id)
            result = await server.send_message_with_status(configs[0].bot_id, "private", 80001, "API only", ack_timeout=2)
            assert result.status == "sent"
            await connection(11001, "Event", receive=False)
            assert manager._is_eligible(configs[0].bot_id)
            paired = list(manager._connections["11001"].values())
            await connection(11001)
            await _until(lambda: set(manager._connections["11001"]) == {"Universal"})
            assert all(instance.stale for instance in paired)


async def test_split_connections_follow_live_bindings(tmp_path):
    async with _runtime(tmp_path, qqs=()) as (manager, server, configs, connection, received):
        await connection(11001, "Event", receive=False)
        await connection(11001, "API")
        pair = list(manager._connections["11001"].values())
        assert not any(instance.bound for instance in pair)
        cfg = manager.create_bot(qq=11001)
        assert all(instance.bound and instance.bot_id == cfg.bot_id for instance in pair)
        assert manager.unbind_qq(cfg.bot_id)
        assert not any(instance.bound for instance in pair)
        assert all(instance.qq == 11001 for instance in pair)
        assert manager.bind_qq(cfg.bot_id, 11001)
        assert all(instance.bound and instance.bot_id == cfg.bot_id for instance in pair)
        result = await server.send_message_with_status(cfg.bot_id, "private", 80001, "bound", ack_timeout=2)
        assert result.status == "sent"
        await connection(11002, "Event", receive=False)
        await connection(11002, "API")
        next_pair = list(manager._connections["11002"].values())
        assert manager.bind_qq(cfg.bot_id, 11002)
        assert not any(instance.bound for instance in pair)
        assert all(instance.bound and instance.bot_id == cfg.bot_id for instance in next_pair)
        assert manager.get(cfg.bot_id) in next_pair
        seen = []

        async def callback(bot_id, event, raw):
            seen.append(raw["self_id"])

        server.set_event_callback(callback)
        await server._dispatch(pair[0], _event(11001, 10))
        await server._dispatch(next_pair[0], _event(11002, 11))
        await _until(lambda: bool(seen))
        assert seen == [11002]
        for old in pair:
            manager.unregister(old)
        assert manager.get(cfg.bot_id) in next_pair
        result = await server.send_message_with_status(cfg.bot_id, "private", 80001, "rebound", ack_timeout=2)
        assert result.status == "sent"


async def test_stale_queued_event_never_enters_callback(tmp_path):
    sockets = asyncio.Queue()

    async def accept(socket):
        sockets.put_nowait(socket)
        await socket.wait_closed()

    async with serve(accept, "127.0.0.1", 0) as listener:
        url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        async with connect(url), connect(url):
            first, second = await sockets.get(), await sockets.get()
            manager = BotManager(str(tmp_path / "data"))
            manager.create_bot(qq=11001)
            supervisor = TaskSupervisor()
            server = WSServer(manager, task_supervisor=supervisor, data_dir=str(tmp_path / "data"))
            seen = []

            async def callback(bot_id, event, raw):
                seen.append(raw["message_id"])
                with (tmp_path / "effects.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(str(raw["message_id"]) + "\n")

            server.set_event_callback(callback)
            try:
                old = manager.register(11001, first)
                await server._dispatch(old, _event(11001, 1))
                # No event-loop yield between scheduling and replacing the instance.
                fresh = manager.register(11001, second)
                await server._dispatch(fresh, _event(11001, 2))
                await _until(lambda: bool(seen))
                await asyncio.sleep(0)
                assert seen == [2]
                assert (tmp_path / "effects.jsonl").read_text() == "2\n"
            finally:
                await supervisor.shutdown()
                await server.stop()


async def test_no_prefix_divination_claims_one_owner_across_account_ids(tmp_path):
    async with _runtime(tmp_path, qqs=(11001, 11002)) as (manager, server, configs, connection, received):
        await connection(11001)
        await connection(11002)
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / "plugins" / "divination.py", plugins / "fortune_network.py")
        system = PluginSystem(str(plugins), str(tmp_path / "plugin-data"))
        system.set_runtime_refs(ws_server=server, bot_manager=manager)
        await system.load_plugins()
        try:
            for index in range(24):
                group_id = 70001 + index
                for cfg in configs:
                    manager.note_group_message(cfg.bot_id, group_id)
                before = sum(item["action"] == "send_group_msg" for item in received)
                events = [_event(cfg.qq, index * 100 + n, text=("占卜" if index % 2 else "今日占卜"),
                                 group_id=group_id, timestamp=100 + index) for n, cfg in enumerate(configs)]
                assert group_message_fingerprint(Event.from_dict(events[0])) == group_message_fingerprint(Event.from_dict(events[1]))
                order = (0, 1) if index % 2 else (1, 0)
                for n in order:
                    handled, reply = await system.dispatch_observed(configs[n].bot_id, Event.from_dict(events[n]), events[n])
                    if handled:
                        result = await server.send_message_with_status(configs[n].bot_id, "group", group_id, reply, ack_timeout=2)
                        assert result.status == "sent"
                assert sum(item["action"] == "send_group_msg" for item in received) == before + 1
            assert len(manager._command_claims) == 24
            # @ targeting must not allow the unmentioned account to run the plugin.
            raw = _event(11001, 9991, group_id=90001, at=11002)
            assert (await system.dispatch_observed(configs[0].bot_id, Event.from_dict(raw), raw))[0] is False
            assert (await system.dispatch_observed(configs[1].bot_id, Event.from_dict(raw), raw))[0] is True
            assert (tmp_path / "plugin-data" / "divination.json").is_file()
        finally:
            await system.shutdown_plugins()


@asynccontextmanager
async def _speech_http():
    inputs, started, release = [], asyncio.Event(), asyncio.Event()

    async def tts(request):
        inputs.append(await request.json())
        return web.Response(body=b"RIFF-local-audio", content_type="audio/wav")

    async def held(request):
        started.set()
        await release.wait()
        return web.Response(text="released")

    app = web.Application()
    app.router.add_post("/tts", tts)
    app.router.add_get("/hold", held)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", inputs, started, release
    finally:
        release.set()
        await runner.cleanup()


async def test_http_queue_survives_unused_minimax_changes(tmp_path):
    async with _speech_http() as (url, inputs, started, release):
        async with _runtime(tmp_path) as (manager, server, configs, connection, received):
            await connection(11001)
            cfg = TTSConfig(enabled=True, backend="http", base_url=url, api_key="local-test", convert_to_mp3=False)
            service = TTSService(cfg)
            service.set_ws(server)
            original = service._client
            assert service.submit(TTSJob(configs[0].bot_id, "group", "70001", "first"))
            assert service.submit(TTSJob(configs[0].bot_id, "group", "70001", "second"))
            service.sync_config(replace(cfg, model="unused", voice_id="unused", speed=1.2, vol=1.1,
                                        pitch=1, sample_rate=16000, bitrate=64000, format="wav"))
            assert service._client is original and service._config_generation == 0
            try:
                service.start()
                await _until(lambda: service.stats["done"] == 2)
                assert [body["text"] for body in inputs] == ["first", "second"]
                assert service.stats["dropped"] == 0
                assert len(received) == 2
                assert service.submit(TTSJob(configs[0].bot_id, "group", "70001", "stale timeout"))
                service.sync_config(replace(cfg, timeout=float(cfg.timeout) + 1))
                await _until(lambda: service.stats["dropped"] == 1)
                assert len(inputs) == 2 and len(received) == 2
            finally:
                await service.stop()


async def test_disabled_speech_is_revoked_in_actual_outbound_queue(tmp_path):
    import httpx

    async with _speech_http() as (url, inputs, started, release):
        async with _runtime(tmp_path) as (manager, server, configs, connection, received):
            await connection(11001)
            bot_id = configs[0].bot_id
            cfg = TTSConfig(enabled=True, backend="http", base_url=url, api_key="local-test", convert_to_mp3=False)
            service = TTSService(cfg)
            service.set_ws(server)
            async with httpx.AsyncClient() as client:
                async def held_operation():
                    await client.get(url + "/hold")

                blocker = asyncio.create_task(server._outbound_scheduler.submit(bot_id, held_operation))
                process = None
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    job = TTSJob(bot_id, "group", "70001", "queued audio")
                    process = asyncio.create_task(service._process(job))
                    await _until(lambda: bool(manager._pending_sent))
                    assert inputs == [{"text": "queued audio"}]
                    service.sync_config(replace(cfg, enabled=False))
                    release.set()
                    await asyncio.wait_for(blocker, 5)
                    await asyncio.wait_for(process, 5)
                    assert not received
                    assert service.stats["done"] == 0 and service.stats["dropped"] == 1
                    assert not manager._pending_sent
                    assert not any(manager._pending_responses.values())
                    assert not any(server._sent_claims._claims.values())
                    assert not (tmp_path / "data" / "history").exists()
                    # Enabling again permits a new job; already sent audio is retained.
                    service.sync_config(replace(cfg, enabled=True))
                    fresh = TTSJob(bot_id, "group", "70001", "fresh audio", generation=service._config_generation)
                    await service._process(fresh)
                    assert len(received) == 1 and service.stats["done"] == 1
                    service.sync_config(replace(cfg, enabled=False))
                    assert len(received) == 1
                finally:
                    release.set()
                    await asyncio.gather(blocker, *( [process] if process is not None else [] ), return_exceptions=True)
                    await service.stop()
