"""Offline persona A/B regressions. Plain functions for tests/_run_all.py."""
import asyncio
import base64
import copy
import io
import json
import random
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mohobot.bot_manager import BotManager, BotInstance
from mohobot.context_manager import ContextManager
from mohobot.llm_service import LLMService
from mohobot.message_handler import MessageHandler
from mohobot.models.config import GlobalConfig, PersonaABConfig
from mohobot.models.onebot import PrivateMessageEvent, GroupMessageEvent, Sender
from mohobot.persona_service import PersonaService, PersonaInUseError
from mohobot.services.persona_ab import PersonaABService, content_hash, delivery_status
from mohobot.utils.persona_ab_card import render_comparison, wrap_text


def event(user=3001, text="模拟问题", mid=1):
    return PrivateMessageEvent(time=1, self_id=1001, post_type="message", message_id=mid,
        user_id=user, message=text, sender=Sender(user_id=user, nickname="模拟"))


class FakeLLM:
    def __init__(self):
        self.preparations = 0
        self.calls = []
        self.fail = set()
    def freeze_completion(self, config=None):
        return {"client": object(), "parameters": {"model": "mock", "temperature": 0.7, "max_tokens": 4096}}
    async def prepare_input(self, bot, ev, context, config=None):
        self.preparations += 1
        return [{"role": "system", "content": "shared"}, {"role": "user", "content": "模拟问题"}]
    async def complete_prepared(self, bot, ev, prepared, persona, frozen):
        self.calls.append((prepared, persona, frozen))
        if persona in self.fail:
            raise RuntimeError("mock failure")
        return persona + " 回复"
    async def chat_stream(self, **kwargs):
        yield "普通聊天", True


class FakeWS:
    def __init__(self, manager=None, statuses=None):
        self._bot_manager = manager
        self.sent = []
        self.statuses = list(statuses or [])
    async def send_private_msg(self, bot, user, message, **kwargs):
        self.sent.append((bot, user, message, kwargs))
        return {"delivery_status": self.statuses.pop(0) if self.statuses else "success"}


async def setup(root):
    manager = BotManager(root)
    bot = manager.create_bot(nickname="mock", qq=1001)
    context = ContextManager(root, summary_enabled=False, trim_at_rounds=100)
    personas = PersonaService(root, manager, context)
    await personas.startup()
    bot = manager.load_bot_config(bot.bot_id)
    manager._bots[bot.bot_id] = BotInstance(bot.bot_id, None, bot)
    baseline = await personas.create_persona("基准", "BASE")
    test = await personas.create_persona("测试", "TEST")
    cfg = PersonaABConfig(enabled=True, baseline_id=baseline["id"], test_id=test["id"], probability=1, cooldown_seconds=0)
    now = [1000.0]
    llm = FakeLLM()
    service = PersonaABService(root, cfg, personas, llm, clock=lambda: now[0], rng=random.Random(7))
    await service.startup()
    session = await context.capture_session(bot.bot_id, "private", "3001")
    return SimpleNamespace(manager=manager, bot=bot, context=context, personas=personas, baseline=baseline,
        test=test, cfg=cfg, now=now, llm=llm, service=service, session=session)


async def reserve(env, effective=None, mid=None):
    return await env.service.reserve(env.bot.bot_id, event(mid=mid if mid is not None else int(env.now[0])), env.session, effective or {"id": "unrelated"})


async def exposed(env, record):
    await env.service.repo.update(env.bot.bot_id, 3001, record["id"], state="pending", exposed_at=env.now[0])


async def rejects(coroutine, fragment=""):
    try:
        await coroutine
    except ValueError as exc:
        assert fragment in str(exc), str(exc)
    else:
        raise AssertionError("expected rejection")


def test_config_roundtrip_and_validation():
    cfg = GlobalConfig()
    assert cfg.persona_ab.to_dict() == PersonaABConfig().to_dict()
    assert not cfg.persona_ab.enabled and cfg.persona_ab.probability == .05
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "cfg.yaml"
        cfg.save(path)
        assert GlobalConfig.load(path).persona_ab == cfg.persona_ab
        assert cfg.to_dict()["persona_ab"]["pending_seconds"] == 86400
    for raw in ({"probability": float("nan")}, {"probability": 1.1}, {"enabled": "false"}, {"max_pages": 21}, {"pending_seconds": 0}, {"bots": []}):
        try:
            PersonaABConfig.from_dict(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(raw)


async def test_sampling_concurrent_cooldown_and_recovery():
    with tempfile.TemporaryDirectory() as root:
        e = await setup(root)
        e.cfg.enabled = False
        assert await reserve(e) is None
        e.cfg.enabled = True
        e.cfg.probability = 0
        assert await reserve(e) is None
        e.cfg.probability = 1
        assert await e.service.reserve("bot_003", event(), e.session, {}) is None
        group = GroupMessageEvent(time=1, self_id=1, post_type="message", user_id=3001, group_id=9, sender=Sender(user_id=3001))
        assert await e.service.reserve("bot_001", group, e.session, {}) is None
        e.cfg.cooldown_seconds = 900
        claims = await asyncio.gather(*(reserve(e) for _ in range(15)))
        assert len([r for r in claims if r]) == 1
        record = next(r for r in claims if r)
        await e.service.startup()
        report = await e.service.report()
        assert report["records"][0]["state"] == "aborted"
        assert await reserve(e) is None
        e.now[0] += 901
        record = await reserve(e)
        await e.service.repo.update("bot_001", 3001, record["id"], state="sending")
        await e.service.startup()
        assert (await e.service.report())["records"][0]["state"] == "delivery_unknown"
        e.now[0] += 901
        record = await reserve(e)
        await exposed(e, record)
        await e.service.startup()
        assert (await e.service.report())["records"][0]["state"] == "pending"
        e.now[0] += 86401
        assert await reserve(e) is not None
        assert any(r["state"] == "expired" for r in (await e.service.report())["records"])


async def test_votes_owner_idempotency_switch_guards_and_references():
    with tempfile.TemporaryDirectory() as root:
        e = await setup(root)
        record = await reserve(e)
        await exposed(e, record)
        await rejects(e.service.vote("bot_002", 3001, record["id"], "A"))
        await rejects(e.service.vote("bot_001", 3002, record["id"], "A"))
        results = await asyncio.gather(*(e.service.vote("bot_001", 3001, record["id"], c) for c in ["A", "B", "平局"]))
        assert all("投票：A" in response for response in results)
        assert "/ab switch" in results[0] and "未切换" in results[0]
        original = await e.context.load_context("bot_001", "private", "3001")
        await e.service.switch("bot_001", 3001, record["id"], "B")
        assert await e.context.load_context("bot_001", "private", "3001") == original
        other = await e.context.create_session("bot_001", "private", "3001", "other")
        await rejects(e.service.switch("bot_001", 3001, record["id"], "A"), "活动")
        await e.context.switch_session("bot_001", "private", "3001", record["session_id"])
        snapshot = record["personas"][record["mapping"]["A"]]
        await e.personas.update_persona(snapshot["id"], "changed", "CHANGED")
        await rejects(e.service.switch("bot_001", 3001, record["id"], "A"), "修改")
        await e.personas.update_persona(snapshot["id"], "restore", snapshot["content"])
        await e.service.repo.update("bot_001", 3001, record["id"], session_generation="wrong")
        await rejects(e.service.switch("bot_001", 3001, record["id"], "A"), "generation")
        await rejects(e.personas.delete_persona(e.test["id"]), "使用")
        library = copy.deepcopy(e.personas._library)
        library["personas"] = [p for p in library["personas"] if p["id"] != e.test["id"]]
        await rejects(e.personas.restore_library(library), "缺少")
        e.now[0] += 86401
        await rejects(e.service.vote("bot_001", 3001, record["id"], "A"), "过期")


async def test_vote_after_deleted_session_and_switch_generation():
    with tempfile.TemporaryDirectory() as root:
        e = await setup(root)
        sid = await e.context.create_session("bot_001", "private", "3001", "temporary")
        e.session = await e.context.capture_session("bot_001", "private", "3001")
        record = await reserve(e)
        await exposed(e, record)
        assert await e.context.delete_session("bot_001", "private", "3001", sid)
        assert "已记录" in await e.service.vote("bot_001", 3001, record["id"], "都不好")
        assert await e.context.create_session("bot_001", "private", "3001", "recreated") == sid
        await rejects(e.service.switch("bot_001", 3001, record["id"], "A"), "generation")


async def test_shared_preparation_success_and_failures():
    for statuses, fail, expected_state, expected_answer in [
        (["success", "success"], set(), "pending", "TEST 回复"),
        (["success", "unknown", "success"], set(), "fallback", "TEST 回复"),
        (["success", "failed", "failed"], set(), "failed", ""),
        (["success", "success"], {"BASE"}, "fallback", "TEST 回复"),
        (["success"], {"BASE", "TEST"}, "failed", ""),
    ]:
        with tempfile.TemporaryDirectory() as root:
            e = await setup(root)
            e.llm.fail = fail
            record = await reserve(e, {"id": e.test["id"]})
            ws = FakeWS(statuses=statuses)
            with patch("mohobot.services.persona_ab.render_comparison", return_value=[{"type": "image", "data": {"file": "base64://mock"}}]):
                answer = await e.service.run(record, event(), [], e.bot, ws)
            assert answer == expected_answer, (answer, expected_answer)
            saved = (await e.service.report())["records"][0]
            assert saved["state"] == expected_state, saved
            assert e.llm.preparations == 1
            assert all(call[0] is e.llm.calls[0][0] and call[2] is e.llm.calls[0][2] for call in e.llm.calls)
            assert len(e.llm.calls) <= 3
            assert saved["exposed_at"] is None or expected_state == "pending"
            assert "client" not in json.dumps(saved)
            assert all(msg[3]["wait_response"] for msg in ws.sent)


async def test_llm_prepared_same_metadata_no_tools_or_tts():
    with tempfile.TemporaryDirectory() as root:
        cfg = GlobalConfig(data_dir=root)
        cfg.tts.enabled = True
        llm = LLMService(cfg)
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="<tts>回复</tts>", tool_calls=None))])
        llm._chat_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        llm._record_usage = AsyncMock()
        llm._song_annotator = AsyncMock(return_value="歌曲共享")
        llm._describe_image_for_text = AsyncMock(return_value="图片共享")
        bot = SimpleNamespace(tts_enabled=True, chat_model_override="fixed")
        ev = event(text=[{"type":"text","data":{"text":"问题"}}, {"type":"image","data":{"url":"https://invalid.local/mock.png"}}])
        frozen = llm.freeze_completion(bot)
        cfg.llm.chat_temperature = 0.1
        prepared = await llm.prepare_input("bot_001", ev, [{"role":"emotion","content":"情感共享"}], bot)
        try:
            assert await llm.complete_prepared("bot_001", ev, prepared, "BASE", frozen) == "回复"
            assert await llm.complete_prepared("bot_001", ev, prepared, "TEST", frozen) == "回复"
            assert llm._song_annotator.await_count == llm._describe_image_for_text.await_count == 1
            assert calls[0]["messages"][0]["content"][4:] == calls[1]["messages"][0]["content"][4:]
            assert calls[0]["messages"][1:] == calls[1]["messages"][1:]
            assert all("tools" not in c and "tool_choice" not in c and c["temperature"] == .7 for c in calls)
            assert "语音标注规则" not in calls[0]["messages"][0]["content"]
            assert "语音标注规则" in (await llm._build_messages("bot_001", event(), [], bot, "BASE"))[0]["content"]
        finally:
            llm._chat_client = None
            await llm.close()


async def test_handler_context_choice_pending_chat_and_vote_bypass():
    with tempfile.TemporaryDirectory() as root:
        e = await setup(root)
        ws = FakeWS(e.manager)
        emotion = SimpleNamespace(schedule_turn=lambda *a: None)
        handler = MessageHandler(ws, e.context, e.llm, None, data_dir=root, persona_service=e.personas, persona_ab_service=e.service)
        with patch("mohobot.services.persona_ab.render_comparison", return_value=[{"type":"image","data":{"file":"mock"}}]):
            await handler._handle_message("bot_001", event(), {})
        saved = await e.context.load_context("bot_001", "private", "3001")
        assert saved[-1]["content"] == "BASE 回复"  # unrelated effective -> baseline
        await handler._handle_message("bot_001", event(text="继续聊天", mid=2), {})
        assert (await e.context.load_context("bot_001", "private", "3001"))[-1]["content"] == "普通聊天"
        before = len(await e.context.load_context("bot_001", "private", "3001"))
        handler._emotion = SimpleNamespace(schedule_turn=lambda *a: (_ for _ in ()).throw(AssertionError("vote emotion")))
        await handler._handle_message("bot_001", event(text="/ab A", mid=3), {})
        assert len(await e.context.load_context("bot_001", "private", "3001")) == before
        await handler.close()


async def test_append_false_blocks_database_and_emotion():
    with tempfile.TemporaryDirectory() as root:
        e = await setup(root)
        handler = MessageHandler(FakeWS(e.manager), e.context, e.llm, None, data_dir=root, persona_service=e.personas, persona_ab_service=e.service)
        handler._persist_legacy_turn = lambda *a: (_ for _ in ()).throw(AssertionError("DB called"))
        handler._emotion = SimpleNamespace(schedule_turn=lambda *a: (_ for _ in ()).throw(AssertionError("emotion called")))
        with patch.object(e.context, "append_context", new=AsyncMock(return_value=False)), patch("mohobot.services.persona_ab.render_comparison", return_value=[]):
            await handler._handle_message("bot_001", event(), {})
        assert not (await e.service.report())["records"][0]["context_written"]
        await handler.close()


async def test_timeouts_release_slot_and_failure_is_not_an_answer():
    for phase in ("prepare", "generate"):
        with tempfile.TemporaryDirectory() as root:
            e = await setup(root)
            e.service.PREPARE_TIMEOUT = e.service.GENERATE_TIMEOUT = e.service.RETRY_TIMEOUT = .01
            async def never(*args, **kwargs):
                await asyncio.Event().wait()
            if phase == "prepare":
                e.llm.prepare_input = never
            else:
                e.llm.complete_prepared = never
            record = await reserve(e)
            ws = FakeWS()
            assert await asyncio.wait_for(e.service.run(record, event(), [], e.bot, ws), 1) == ""
            report = await e.service.report()
            assert report["records"][0]["state"] == "failed"
            assert "未能完成" in ws.sent[-1][2][0]["data"]["text"]
            assert "用于人设评估" in ws.sent[0][2][0]["data"]["text"]
            assert await reserve(e, mid=2000) is not None


async def test_source_dedup_skip_statistics_and_readonly_storage():
    with tempfile.TemporaryDirectory() as root:
        e = await setup(root)
        e.cfg.probability = 0
        assert await reserve(e) is None
        assert not list(e.service.repo.root.glob("*/*.json")), "sampling miss must not create an empty file"
        e.cfg.probability = 1
        record = await reserve(e)
        await exposed(e, record)
        await e.service.vote("bot_001", 3001, record["id"], "跳过")
        assert await reserve(e) is None, "same source message must never be evaluated again"
        path = e.service.repo.path("bot_001", 3001)
        mtime = path.stat().st_mtime_ns
        report = await e.service.report()
        assert report["statistics"]["votes"] == 0 and report["statistics"]["skipped"] == 1
        assert report["statistics"]["response_rate"] == 0
        assert path.stat().st_mtime_ns == mtime, "report must not rewrite unchanged records"


async def test_render_failure_preserves_full_text_and_no_exposure():
    with tempfile.TemporaryDirectory() as root:
        e = await setup(root)
        record = await reserve(e)
        ws = FakeWS()
        with patch("mohobot.services.persona_ab.render_comparison", side_effect=ValueError("too long")):
            answer = await e.service.run(record, event(), [], e.bot, ws)
        assert answer == "BASE 回复"
        stored = (await e.service.report())["records"][0]
        assert stored["state"] == "fallback" and stored["exposed_at"] is None
        assert ws.sent[-1][2] == [{"type":"text", "data":{"text":answer}}]


async def test_ws_private_confirmed_delivery_contract():
    from mohobot.ws_server import WSServer
    with tempfile.TemporaryDirectory() as root:
        manager = BotManager(root)
        ws = WSServer(bot_manager=manager, data_dir=root)
        assert (await ws.send_private_msg("bot_001", 3001, "mock", wait_response=True))["delivery_status"] == "failed"
        bot = manager.create_bot(qq=1001)
        manager._bots[bot.bot_id] = BotInstance(bot.bot_id, None, bot)
        for response, status in [(None,"unknown"), ({"status":"ok","retcode":0},"success"), ({"status":"failed","retcode":100},"failed")]:
            with patch.object(ws, "_send_tracked", new=AsyncMock(return_value=response)) as tracked:
                result = await ws.send_private_msg(bot.bot_id, 3001, "mock", wait_response=True)
                assert result["delivery_status"] == status
                assert tracked.call_args.kwargs["wait_response"] is True
        with patch.object(ws, "_send_tracked", new=AsyncMock(return_value=None)) as tracked:
            assert await ws.send_private_msg(bot.bot_id, 3001, "normal") is None
            assert tracked.call_args.kwargs["wait_response"] is False


async def test_ws_archive_timeout_keeps_business_echo_route():
    # Windows monotonic has 15.6ms granularity; use its high-resolution clock
    # so independent 80/90/100ms deadlines do not collapse into one loop tick.
    loop = asyncio.get_running_loop()
    with patch.object(loop, "time", time.perf_counter), patch.object(loop, "_clock_resolution", 1e-6):
        await _assert_delayed_echo_route()


async def _assert_delayed_echo_route():
    from mohobot.ws_server import WSServer
    with tempfile.TemporaryDirectory() as root:
        manager = BotManager(root)
        bot = manager.create_bot(qq=1001)
        manager._bots[bot.bot_id] = BotInstance(bot.bot_id, None, bot)
        ws = WSServer(bot_manager=manager, data_dir=root)
        ws._ARCHIVE_ECHO_TIMEOUT = .08
        payloads = []
        archive_tasks = []
        async def guarded(instance, payload):
            payloads.append(payload)
        async def direct(address, callback, **kwargs):
            return await callback()
        ws._send_guarded = guarded
        ws._reply_sender.send = direct
        ws._write_archive_line = AsyncMock()
        ws._spawn_archive_task = lambda coroutine: archive_tasks.append(asyncio.create_task(coroutine))
        started = asyncio.get_running_loop().time()
        task = asyncio.create_task(ws._send_tracked(bot.bot_id, "send_private_msg", {"user_id":3001,"message":"mock"}, "private", 3001, wait_response=True, timeout=.10))
        while not payloads:
            await asyncio.sleep(0)
        echo = payloads[0]["echo"]
        await asyncio.gather(*archive_tasks)
        # Windows coarse sleep timers can coalesce 90/100ms. Yield (not sleep)
        # up to the 90ms echo to keep this real-routing regression deterministic.
        while asyncio.get_running_loop().time() < started + .09:
            await asyncio.sleep(0)
        assert echo in manager._pending_responses[bot.bot_id], "archive must not remove caller routing at 80ms"
        future = manager._pending_responses[bot.bot_id][echo]
        assert not future.cancelled(), "archive must not cancel the shared future"
        await manager.handle_api_response(bot.bot_id, {"status":"ok","retcode":0,"echo":echo,"data":{"message_id":123}})
        assert (await task)["status"] == "ok"
        await asyncio.gather(*archive_tasks)
        assert not manager._pending_responses and not manager._pending_sent


async def test_switch_rechecks_expiry_after_persona_and_chat_lock_wait():
    for held_lock in ("persona", "chat"):
        with tempfile.TemporaryDirectory() as root:
            e = await setup(root)
            record = await reserve(e)
            await exposed(e, record)
            lock = e.personas.lock if held_lock == "persona" else e.context.chat_lock("bot_001", "private", "3001")
            async with lock:
                task = asyncio.create_task(e.service.switch("bot_001", 3001, record["id"], "A"))
                await asyncio.sleep(.02)
                assert not task.done()
                e.now[0] = record["expires_at"] + 1
            await rejects(task, "过期")
            binding = await e.personas.get_session_binding("bot_001", "3001", record["session_id"])
            assert binding["persona_id"] == ""
            assert (await e.service.report())["records"][0]["switch"] is None


async def test_pending_blocks_across_session_changes_and_statistics():
    with tempfile.TemporaryDirectory() as root:
        e = await setup(root)
        record = await reserve(e)
        await exposed(e, record)
        await e.context.create_session("bot_001", "private", "3001", "new")
        e.session = await e.context.capture_session("bot_001", "private", "3001")
        assert await reserve(e) is None
        e.cfg.enabled = False  # closing sampling does not invalidate an existing ballot
        await e.service.vote("bot_001", 3001, None, "B")
        selected = record["mapping"]["B"]
        report = await e.service.report(version_hash=record["personas"][selected]["hash"])
        assert report["total"] == 1 and report["statistics"][selected + "_wins"] == 1
        assert report["statistics"]["response_rate"] == 1
        assert (await e.service.report(bot_id="bot_002"))["total"] == 0


def test_cards_pagination_and_limits():
    from PIL import Image, ImageFont
    from mohobot.utils.image_card import find_cjk_font
    font = ImageFont.truetype(find_cjk_font(), 22)
    text = "中文 English " * 200
    lines = wrap_text(text, font, 530)
    assert "".join(lines) == text
    images = render_comparison("0123456789abcdef", text, "短文本", 20)
    assert len(images) > 1
    for segment in images:
        image = Image.open(io.BytesIO(base64.b64decode(segment["data"]["file"].split("//",1)[1])))
        assert image.size == (1200,1240)
    try:
        render_comparison("id", text * 20, "short", 1)
    except ValueError:
        pass
    else:
        raise AssertionError("must fall back instead of truncate")
    with patch("mohobot.utils.persona_ab_card.wrap_text", side_effect=AssertionError("oversized text reached wrapping")):
        try:
            render_comparison("id", "x" * 40001, "", 20)
        except ValueError:
            pass
        else:
            raise AssertionError("must bound characters before wrapping")
    assert delivery_status(None) == "unknown"
    assert delivery_status({"status":"ok","retcode":0}) == "success"
    assert delivery_status({"status":"failed","retcode":100}) == "failed"


async def test_web_auth_filters_export_and_safe_ui():
    from fastapi.testclient import TestClient
    from mohobot.web_panel.app import WebPanel
    with tempfile.TemporaryDirectory() as root:
        e = await setup(root)
        cfg = GlobalConfig(data_dir=root, persona_ab=e.cfg)
        path = Path(root) / "cfg.yaml"
        cfg.save(path)
        with patch.object(WebPanel, "_install_log_sink"):
            panel = WebPanel(password_hash=WebPanel._hash_password("mock"), config_path=str(path), data_dir=root,
                persona_service=e.personas, persona_ab_service=e.service)
        with TestClient(panel._app) as client:
            assert client.get("/api/persona-ab").status_code == 401
            assert client.get("/api/persona-ab/export").status_code == 401
            panel._tokens["mock"] = time.time() + 60
            client.headers["Authorization"] = "Bearer mock"
            assert client.get("/api/persona-ab?bot_id=bot_999&version_hash=bad").json()["total"] == 0
            assert client.get("/api/persona-ab/export").headers["content-disposition"].startswith("attachment")
            invalid = e.cfg.to_dict(); invalid["test_id"] = invalid["baseline_id"]
            assert client.put("/api/config", json={"data":{"persona_ab":invalid}}).status_code == 400
            valid = e.cfg.to_dict(); valid["probability"] = .25
            assert client.put("/api/config", json={"data":{"persona_ab":valid}}).status_code == 200
            assert e.service.config.probability == .25
            assert client.get("/api/config").json()["persona_ab"]["probability"] == .25
        html = (Path(__file__).resolve().parent.parent / "mohobot/web_panel/static/index.html").read_text(encoding="utf-8")
        assert "esc(r.question)" in html and "esc(JSON.stringify(r,null,2))" in html


if __name__ == "__main__":
    for name, target in sorted(list(globals().items())):
        if name.startswith("test_"):
            if asyncio.iscoroutinefunction(target):
                asyncio.run(target())
            else:
                target()
            print("PASS", name)
