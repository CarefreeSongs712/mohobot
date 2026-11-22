"""Issue #131: 主动聊天发送前重验状态 + TTS 停用/配置代次门控。

主动聊天只在生成前查一次状态: 生成是长等待, 期间 bot 被禁用、插件被关闭、
用户被封禁后仍会发出消息; BanStore 异常还会 fail-open。
TTS worker 不查 cfg.enabled / 配置代次: 停用后旧队列任务仍发送;
且已建 httpx/MiniMax client 的 transport timeout 不会被热更新。
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "proactive_chat"))

from mohobot.file_store import json_read  # noqa: E402
from mohobot.services.tts import HttpTTSClient, TTSJob, TTSService  # noqa: E402
from mohobot.models.config import TTSConfig  # noqa: E402
from proactive_chat_core.engine import ProactiveEngine, session_key  # noqa: E402


# ── 主动聊天测试替身 ───────────────────────────────────────────


class _Llm:
    _chat_client = object()

    def __init__(self, reply="在忙什么呢？", on_chat=None):
        self.reply = reply
        self.on_chat = on_chat
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        if self.on_chat is not None:
            self.on_chat()
        return self.reply, None


class _Ctx:
    def __init__(self, appended):
        self._appended = appended

    async def capture_session(self, *args, **kwargs):
        return {"id": "s", "generation": "g", "persona_id": ""}

    async def load_context(self, *args, **kwargs):
        return []

    async def append_context(self, *args, **kwargs):
        self._appended.append(args)


class _MH:
    def __init__(self, sent):
        self._sent = sent

    async def send_proactive_reply(self, bot_id, event, text):
        self._sent.append(text)
        return text


class _WS:
    def __init__(self):
        self.sent = []

    async def send_private_msg(self, bot_id, user_id, message, source="auto"):
        self.sent.append(message)


def _instance(enabled=True, stale=False):
    return SimpleNamespace(
        config=SimpleNamespace(
            persona_id="", chat_model_override="", nickname="b", enabled=enabled),
        qq=111, stale=stale,
    )


_UNSET = object()


async def _make_engine(td, *, instance, llm, sent, appended, ban_store=None,
                       message_handler=_UNSET, ws_server=None, now=None):
    now = now if now is not None else [1000.0]
    refs = {
        "bot_manager": SimpleNamespace(get=lambda bot_id: instance),
        "llm_service": llm,
        "message_handler": _MH(sent) if message_handler is _UNSET else message_handler,
        "context_manager": _Ctx(appended),
        "persona_service": SimpleNamespace(resolve=lambda cfg, pid="": {"content": "x"}),
        "ws_server": ws_server,
        "ban_store": ban_store,
    }
    engine = ProactiveEngine(
        state_path=Path(td) / "state.json",
        config={
            "enable": True, "min_interval_minutes": 1, "max_interval_minutes": 1,
            "quiet_hours": "", "max_unanswered_times": 4, "exclude_users": [],
            "proactive_prompt": "主动聊聊", "enable_auto_trigger": False,
            "tick_interval_sec": 30,
        },
        get_ref=refs.get,
        clock=lambda: now[0],
    )
    engine.plugin_start_time = now[0]
    return engine, now


async def _seed_due(engine, now):
    def seed(state):
        state.setdefault("sessions", {})[session_key("bot_001", "123")] = {
            "unanswered_count": 0, "last_message_time": 1.0, "sender_name": "小明",
            "last_scheduled_at": now - 60, "next_trigger_time": now,
        }

    await engine._mutate_state(seed)


class _BanStore:
    def __init__(self, banned=False, raises=False):
        self.banned = banned
        self.raises = raises
        self.calls = 0

    async def is_banned(self, key, target):
        self.calls += 1
        if self.raises:
            raise RuntimeError("ban store down")
        return self.banned, ""


async def test_disabled_bot_does_not_generate():
    """bot 已禁用: 生成阶段就应跳过, 不能先花一次 LLM 再丢。"""
    with tempfile.TemporaryDirectory() as td:
        instance = _instance(enabled=False)
        llm = _Llm()
        sent, appended = [], []
        engine, now = await _make_engine(
            td, instance=instance, llm=llm, sent=sent, appended=appended)
        await _seed_due(engine, now[0])

        await engine.tick()

        assert llm.calls == 0, llm.calls
        assert sent == [] and appended == []


async def test_bot_disabled_during_generation_is_not_sent():
    """LLM 等待期间 bot 被禁用: 返回后必须重验, 不得发送。"""
    with tempfile.TemporaryDirectory() as td:
        instance = _instance(enabled=True)

        def _disable():
            instance.config.enabled = False

        llm = _Llm(on_chat=_disable)
        sent, appended = [], []
        engine, now = await _make_engine(
            td, instance=instance, llm=llm, sent=sent, appended=appended)
        await _seed_due(engine, now[0])

        await engine.tick()

        assert llm.calls == 1, llm.calls
        assert sent == [], sent
        assert appended == [], appended


async def test_bot_reconnected_during_generation_is_not_sent():
    """旧连接在生成期间被标记 stale(重连替换): 不得由旧实例发出。"""
    with tempfile.TemporaryDirectory() as td:
        instance = _instance(enabled=True)

        def _stale():
            instance.stale = True

        llm = _Llm(on_chat=_stale)
        sent, appended = [], []
        engine, now = await _make_engine(
            td, instance=instance, llm=llm, sent=sent, appended=appended)
        await _seed_due(engine, now[0])

        await engine.tick()

        assert sent == [], sent


async def test_plugin_disabled_during_generation_is_not_sent():
    """生成期间插件被关闭(配置热更新): 发送前重验 enable。"""
    with tempfile.TemporaryDirectory() as td:
        instance = _instance(enabled=True)
        sent, appended = [], []
        holder = {}

        def _disable_plugin():
            holder["engine"]._config["enable"] = False

        llm = _Llm(on_chat=_disable_plugin)
        engine, now = await _make_engine(
            td, instance=instance, llm=llm, sent=sent, appended=appended)
        holder["engine"] = engine
        await _seed_due(engine, now[0])

        await engine.tick()

        assert llm.calls == 1
        assert sent == [], sent


async def test_banned_during_generation_is_not_sent():
    """生成期间用户被封禁: 发送前重验封禁状态并清掉会话。"""
    with tempfile.TemporaryDirectory() as td:
        instance = _instance(enabled=True)
        store = _BanStore(banned=False)

        def _ban():
            store.banned = True

        llm = _Llm(on_chat=_ban)
        sent, appended = [], []
        engine, now = await _make_engine(
            td, instance=instance, llm=llm, sent=sent, appended=appended,
            ban_store=store)
        await _seed_due(engine, now[0])

        await engine.tick()

        assert llm.calls == 1
        assert sent == [], sent
        state = await json_read(Path(td) / "state.json")
        assert "bot_001:123" not in (state.get("sessions") or {}), state


async def test_ban_store_failure_fails_closed():
    """BanStore 故障: 保守跳过, 不生成不发送(不能 fail-open)。"""
    with tempfile.TemporaryDirectory() as td:
        instance = _instance(enabled=True)
        store = _BanStore(raises=True)
        llm = _Llm()
        sent, appended = [], []
        engine, now = await _make_engine(
            td, instance=instance, llm=llm, sent=sent, appended=appended,
            ban_store=store)
        await _seed_due(engine, now[0])

        await engine.tick()

        assert llm.calls == 0, llm.calls
        assert sent == [], sent


async def test_send_fallback_checks_bot_enabled():
    """message_handler 缺失时的 WS 兜底路径也要查 enabled。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        instance = _instance(enabled=False)
        sent, appended = [], []
        engine, _now = await _make_engine(
            td, instance=instance, llm=_Llm(), sent=sent, appended=appended,
            message_handler=None, ws_server=ws)
        event = SimpleNamespace()

        out = await engine._send("bot_001", "123", event, "在吗")
        assert out == "" and ws.sent == []

        instance.config.enabled = True
        out = await engine._send("bot_001", "123", event, "在吗")
        assert out == "在吗" and ws.sent == ["在吗"], ws.sent


# ── TTS 门控与客户端重建 ─────────────────────────────────────


class _RecorderWS:
    def __init__(self):
        self.sent = []

    async def send_group_msg(self, bot_id, group_id, message):
        self.sent.append(("group", group_id, message))

    async def send_private_msg(self, bot_id, user_id, message):
        self.sent.append(("private", user_id, message))


class _FakeSynth:
    def __init__(self, *, disabled_flag=None, svc=None):
        self.calls = 0
        self._disabled_flag = disabled_flag
        self._svc = svc
        self.closed = False

    async def synthesize(self, text, voice_id=None):
        self.calls += 1
        if self._disabled_flag is not None:
            self._disabled_flag()
        return b"wav-bytes", len(text)

    async def close(self):
        self.closed = True


def _make_service(**cfg_kwargs) -> tuple[TTSService, _RecorderWS]:
    cfg = TTSConfig(backend="http", enabled=True, convert_to_mp3=False, **cfg_kwargs)
    svc = TTSService(cfg)
    ws = _RecorderWS()
    svc.set_ws(ws)
    return svc, ws


async def test_tts_disabled_config_drops_queued_job():
    """TTS 已停用: 队列里的任务不得再合成/发送。"""
    with tempfile.TemporaryDirectory():
        svc, ws = _make_service()
        svc.cfg.enabled = False
        fake = _FakeSynth()
        svc._client = fake
        job = TTSJob("bot_001", "group", 500, "你好", source="command")

        await svc._process(job)

        assert fake.calls == 0, fake.calls
        assert ws.sent == []
        assert svc.stats["dropped"] == 1, svc.stats


async def test_tts_generation_mismatch_drops_queued_job():
    """配置代次变化后, 旧代次入队的任务不得发送。"""
    with tempfile.TemporaryDirectory():
        svc, ws = _make_service()
        fake = _FakeSynth()
        svc._client = fake
        job = TTSJob("bot_001", "group", 500, "你好", source="command")
        job.generation = svc._config_generation
        svc._config_generation += 1

        await svc._process(job)

        assert fake.calls == 0
        assert ws.sent == []
        assert svc.stats["dropped"] == 1


async def test_tts_disabled_during_synthesis_is_not_sent():
    """合成完成后、发送前发现已停用: 丢弃音频, 不发送。"""
    with tempfile.TemporaryDirectory():
        svc, ws = _make_service()
        fake = _FakeSynth(disabled_flag=lambda: setattr(svc.cfg, "enabled", False))
        svc._client = fake
        job = TTSJob("bot_001", "group", 500, "你好", source="command")

        await svc._process(job)

        assert fake.calls == 1
        assert ws.sent == [], ws.sent
        assert svc.stats["dropped"] == 1, svc.stats


async def test_tts_sync_config_rebuilds_client_on_timeout_change():
    """timeout 变化必须重建客户端, 新客户端用新 timeout(旧 transport timeout 不生效)。"""
    with tempfile.TemporaryDirectory():
        svc, ws = _make_service(timeout=60)
        old_client = svc._client
        new_cfg = TTSConfig(backend="http", enabled=True, convert_to_mp3=False, timeout=2)

        svc.sync_config(new_cfg)

        assert svc._client is not old_client
        assert getattr(svc._client, "_timeout", None) == 2.0, svc._client
        assert svc._config_generation == 1, svc._config_generation
        await svc.stop()


async def test_http_tts_client_drops_built_client_on_timeout_change():
    """已建 httpx client 在 timeout 热更新后必须丢弃, 下次调用用新超时。"""
    created: list[float] = []

    class _FakeHttpClient:
        def __init__(self, timeout):
            created.append(timeout)
            self.is_closed = False
            self.timeout = timeout

        async def aclose(self):
            self.is_closed = True

    def factory(timeout):
        return _FakeHttpClient(timeout)

    client = HttpTTSClient(
        "http://tts.local", "key", timeout=60.0, http_client_factory=factory)
    first = await client._get_client()
    assert first.timeout == 60.0

    client.sync_config(SimpleNamespace(
        timeout=2, base_url="http://tts.local", api_key="key"))
    assert client._client is None, "旧 transport timeout 的 client 必须被丢弃"
    await asyncio.sleep(0)  # 让关闭旧客户端的调度任务执行
    assert first.is_closed is True

    second = await client._get_client()
    assert second is not first
    assert created == [60.0, 2.0], created


async def test_tts_submit_stamps_generation_and_sync_advances():
    """submit 打上当前代次; sync_config 推进代次后旧任务被判 stale。"""
    with tempfile.TemporaryDirectory():
        svc, ws = _make_service(timeout=60)
        job = TTSJob("bot_001", "group", 500, "你好", source="command")
        assert svc.submit(job) is True
        assert job.generation == 0

        svc.sync_config(TTSConfig(
            backend="http", enabled=True, convert_to_mp3=False, timeout=2))

        assert svc._config_generation == 1
        assert svc._job_is_stale(job) is True
        # 清空队列并关闭(避免 worker 未启动时的资源残留)
        while not svc._queue.empty():
            svc._queue.get_nowait()
        await svc.stop()


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if asyncio.iscoroutinefunction(t):
            asyncio.run(t())
        else:
            t()
        print(f"PASS {t.__name__}")
    print("\nALL ISSUE #131 TESTS PASSED")


if __name__ == "__main__":
    main()
