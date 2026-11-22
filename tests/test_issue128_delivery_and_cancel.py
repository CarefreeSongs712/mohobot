"""Issue #128: 投递结果反馈 / 取消清理 / 归档回执 / 图片描述等待(前四部分)。

1) 发送失败(离线/被拦/负 ack)时不得把完整回复写入 contexts/DB/情感记忆;
   部分投递只把真正发出的文本当成"已说过"。
2) send_to_bot(wait_response=True) 在 socket send await 中被取消时,
   pending future 必须被清理, 不能泄漏。
3) 调用方 wait 超时 > 归档 8s 阈值时, 真实成功回执仍必须用于归档(message_id 不是 local:)。
4) image_cache.get_or_describe 的 leader 被取消时, follower 不能永久等待。
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotInstance, BotManager
from mohobot.context_manager import ContextManager
from mohobot.image_cache import ImageCache
from mohobot.llm_service import LLMService
from mohobot.message_handler import MessageHandler
from mohobot.models.config import BotConfig, GlobalConfig, ReplyConfig
from mohobot.models.onebot import PrivateMessageEvent, Sender
from mohobot.ws_server import DeliveryResult, WSServer

MODES = [(True, True), (True, False), (False, True)]  # (stream, segment_reply)


class _Usage:
    async def record(self, *args, **kwargs):
        pass

    async def close(self):
        pass


class _OkCompletions:
    async def create(self, **kwargs):
        if kwargs.get("stream"):
            async def chunks():
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="今天也很开心哦。", tool_calls=None))])
            return chunks()
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(
            message=SimpleNamespace(content="今天也很开心哦。", tool_calls=None))])


class _StatusWS:
    """真实 BotManager + 记录发送; send_message_with_status 返回给定投递状态。"""

    def __init__(self, manager, status, *, confirmed=False, reason="test"):
        self._bot_manager = manager
        self.status = status
        self.confirmed = confirmed
        self.reason = reason
        self.sent = []

    async def send_private_msg(self, bot_id, user_id, message, source="auto"):
        self.sent.append(message)

    async def send_group_msg(self, bot_id, group_id, message, source="auto"):
        self.sent.append(message)

    async def send_message_with_status(self, bot_id, chat_type, chat_id, message,
                                       *, source="llm", ack_timeout=5.0):
        self.sent.append(message)
        return DeliveryResult(self.status, confirmed=self.confirmed, reason=self.reason)


class _RecordingDB:
    def __init__(self):
        self.rows = []

    def add_conversation(self, *args, **kwargs):
        self.rows.append(args)


class _RecordingEmotion:
    enabled = True

    def __init__(self):
        self.turns = []

    def schedule_turn(self, bot_id, event, user_text, ai_reply):
        self.turns.append(ai_reply)

    async def build_context_block(self, bot_id, event):
        return ""


def _event(mid=1):
    return PrivateMessageEvent(
        time=1, self_id=1001, post_type="message", message_type="private",
        message_id=mid, user_id=3001, sender=Sender(user_id=3001, nickname="测试用户"),
        message=[{"type": "text", "data": {"text": "你好"}}],
    )


async def _setup(td, *, stream, segment, status="sent", confirmed=True):
    manager = BotManager(data_dir=td)
    cfg = manager.create_bot(nickname="测试bot", qq=1001)
    manager._bots[cfg.bot_id] = BotInstance(cfg.bot_id, None, cfg)
    context = ContextManager(data_dir=td, summary_enabled=False, trim_at_rounds=100)
    llm = LLMService(GlobalConfig(), usage_recorder=_Usage())
    llm._available = True
    llm._chat_client = SimpleNamespace(
        chat=SimpleNamespace(completions=_OkCompletions()))

    async def build_messages(*args, **kwargs):
        return [{"role": "user", "content": "你好"}]

    llm._build_messages = build_messages
    ws = _StatusWS(manager, status, confirmed=confirmed)
    db, emotion = _RecordingDB(), _RecordingEmotion()
    handler = MessageHandler(
        ws, context, llm, None, data_dir=td, database_manager=db,
        emotion_manager=emotion,
        reply_config=ReplyConfig(
            stream=stream, segment_reply=segment,
            segment_delay_min=0, segment_delay_max=0),
    )
    return handler, context, ws, db, emotion, cfg.bot_id


async def test_failed_delivery_is_not_persisted():
    """离线/网络失败: 用户没收到, 上下文/DB/情感都不得记录这条回复。"""
    for stream, segment in MODES:
        with tempfile.TemporaryDirectory() as td:
            handler, context, ws, db, emotion, bot = await _setup(
                td, stream=stream, segment=segment, status="failed", confirmed=False)
            await handler._handle_message(bot, _event(), {})
            mode = f"stream={stream} segment={segment}"
            assert await context.load_context(bot, "private", "3001") == [], mode
            assert db.rows == [], mode
            assert emotion.turns == [], mode
            await handler.close()


async def test_blocked_delivery_is_not_persisted():
    """被路由层/独占群屏障拦下: 同样不得写入记忆。"""
    with tempfile.TemporaryDirectory() as td:
        handler, context, ws, db, emotion, bot = await _setup(
            td, stream=True, segment=True, status="blocked", confirmed=False)
        await handler._handle_message(bot, _event(), {})
        assert await context.load_context(bot, "private", "3001") == []
        assert db.rows == [] and emotion.turns == []
        await handler.close()


async def test_successful_delivery_is_persisted():
    """正向对照: 确认发出(sent)才写记忆, 防止修复变成全量不落盘。"""
    for stream, segment in MODES:
        with tempfile.TemporaryDirectory() as td:
            handler, context, ws, db, emotion, bot = await _setup(
                td, stream=stream, segment=segment, status="sent", confirmed=True)
            await handler._handle_message(bot, _event(), {})
            mode = f"stream={stream} segment={segment}"
            saved = await context.load_context(bot, "private", "3001")
            assert [m["role"] for m in saved][-1] == "assistant", (mode, saved)
            assert saved[-1]["content"] == "今天也很开心哦。", (mode, saved)
            assert len(db.rows) == 2 and emotion.turns == ["今天也很开心哦。"], mode
            await handler.close()


def _bare_handler(statuses):
    h = MessageHandler.__new__(MessageHandler)
    h._segment_reply = True
    h._reply_quote = False
    h._seg_min_len = 12
    h._seg_max_len = 60
    h._seg_delay_min = h._seg_delay_max = 0
    h._strip_english_fillers = True
    h._ooc_guard = True
    h._bot_config = lambda bot_id: BotConfig(bot_id=bot_id)
    calls = []
    queue = list(statuses)

    async def _send_message(bot_id, event, message, source="auto"):
        calls.append(message)
        return queue.pop(0) if queue else "sent"

    h._send_message = _send_message
    return h, calls


async def test_partial_delivery_keeps_only_sent_segments():
    """部分段发送失败: 只把已发出的文本交回调用方(供落库), 未发尾段不算说过。"""
    handler, calls = _bare_handler(["sent", "failed"])
    text = "第一段内容。\n\n第二段内容。"
    collected = []
    outcome = {}
    sent = await handler._send_full_text(
        "bot_001", _event(), text, collected=collected, outcome=outcome)

    assert sent is True
    assert collected == ["第一段内容。"], collected
    assert outcome["failed"] is True
    assert outcome["sent_count"] == 1
    assert outcome["sent_text"] == "第一段内容。", outcome


async def test_stream_reply_failure_returns_only_delivered_text():
    """流式路径: 第二段失败时, 返回值只包含第一段, 调用方据此落库。"""
    handler, calls = _bare_handler(["sent", "failed"])

    async def chat_stream(**kwargs):
        yield "第一段内容。\n\n", False
        yield "第二段内容。", True

    handler._llm = SimpleNamespace(chat_stream=chat_stream)
    handler._stream = True
    handler._submit_tts = lambda *a, **k: None

    out = await handler._stream_llm_reply("bot_001", _event(), [], {})

    assert out == "第一段内容。", out


async def test_single_reply_failure_returns_empty():
    """非流式单条: 发送失败返回 "", 不落库、不朗读。"""
    handler, calls = _bare_handler(["failed"])
    handler._segment_reply = False
    handler._stream = False

    async def chat_stream(**kwargs):
        yield "这段回复没有发出去。", True

    handler._llm = SimpleNamespace(chat_stream=chat_stream)
    tts_calls = []
    handler._submit_tts = lambda *a, **k: tts_calls.append(a)

    out = await handler._stream_llm_reply("bot_001", _event(), [], {})

    assert out == "", out
    assert tts_calls == [], tts_calls


# ── 2) send_to_bot(wait_response=True) 取消清理 ─────────────────


class _BlockingSock:
    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def send(self, payload: str):
        self.started.set()
        await self.release.wait()


async def test_send_to_bot_cancel_cleans_pending_future():
    """socket send 等待中被取消: _pending_responses/_pending_sent 必须清空。"""
    with tempfile.TemporaryDirectory() as td:
        bm = BotManager(data_dir=td)
        sock = _BlockingSock()
        inst = BotInstance("bot_001", sock, BotConfig(bot_id="bot_001", qq=111, nickname="A"))
        bm._bots["bot_001"] = inst
        ws = WSServer(bot_manager=bm, data_dir=td)
        try:
            task = asyncio.create_task(
                ws.send_to_bot("bot_001", "get_group_info",
                               {"group_id": 500}, wait_response=True, timeout=5.0))
            await asyncio.wait_for(sock.started.wait(), timeout=2.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert bm._pending_responses == {}, bm._pending_responses
            assert all(
                not futures for futures in bm._pending_responses.values()
            ), bm._pending_responses
        finally:
            sock.release.set()
            await ws.stop()


# ── 3) wait 路径归档使用真实回执 ──────────────────────────────


def _history_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_wait_path_archive_uses_real_message_id_after_slow_ack():
    """回执晚于归档 8s 阈值到达: 归档仍必须写真实 message_id, 不能写 local:。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bm = BotManager(data_dir=td)
        inst = BotInstance("bot_001", _EchoSock(), BotConfig(bot_id="bot_001", qq=111, nickname="A"))
        bm._bots["bot_001"] = inst
        ws = WSServer(bot_manager=bm, data_dir=td)
        ws._ARCHIVE_ECHO_TIMEOUT = 0.01  # 模拟"归档等待窗口"远早于真实回执
        try:
            async def _reply_later():
                await asyncio.sleep(0.05)
                payload = json.loads(inst.ws.sent[-1])
                await bm.handle_api_response("bot_001", {
                    "status": "ok", "retcode": 0,
                    "data": {"message_id": 777}, "echo": payload["echo"],
                })

            responder = asyncio.create_task(_reply_later())
            result = await ws.send_message_with_status(
                "bot_001", "group", 500, [{"type": "text", "data": {"text": "hi"}}],
                source="llm", ack_timeout=2.0)
            await responder

            assert result.status == "sent" and result.confirmed is True, result
            lines = _history_lines(tmp / "history" / "group" / "500.jsonl")
            assert len(lines) == 1, lines
            assert lines[0]["message_id"] == "777", lines[0]
        finally:
            await ws.stop()


class _EchoSock:
    def __init__(self):
        self.sent = []

    async def send(self, payload: str):
        self.sent.append(payload)

    async def close(self):
        pass


# ── 4) image_cache leader 取消后 follower 不永久等待 ───────────


class _SlowDescriber:
    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def impl(self, image_url, vision_callback=None):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return "/tmp/img.jpg", "一只橘猫"


async def _make_cache(td):
    cache = ImageCache(cache_dir=td)
    describe = _SlowDescriber()
    cache._get_or_describe_impl = describe.impl
    return cache, describe


async def test_image_cache_leader_cancel_does_not_strand_follower():
    """leader 被取消后, 共享任务继续跑, follower 仍能拿到结果。"""
    with tempfile.TemporaryDirectory() as td:
        cache, describe = await _make_cache(td)
        leader = asyncio.create_task(cache.get_or_describe("https://example.com/a.jpg"))
        await asyncio.wait_for(describe.started.wait(), timeout=2.0)
        follower = asyncio.create_task(cache.get_or_describe("https://example.com/a.jpg"))
        await asyncio.sleep(0)

        leader.cancel()
        try:
            await leader
        except asyncio.CancelledError:
            pass

        describe.release.set()
        path, desc = await asyncio.wait_for(follower, timeout=2.0)
        assert (path, desc) == ("/tmp/img.jpg", "一只橘猫")
        assert describe.calls == 1, describe.calls
        assert cache._in_flight == {}, cache._in_flight


async def test_image_cache_follower_cancel_keeps_shared_task():
    """follower 被取消不应取消共享任务, 后续请求仍能复用同一次下载。"""
    with tempfile.TemporaryDirectory() as td:
        cache, describe = await _make_cache(td)
        leader = asyncio.create_task(cache.get_or_describe("https://example.com/b.jpg"))
        await asyncio.wait_for(describe.started.wait(), timeout=2.0)
        follower = asyncio.create_task(cache.get_or_describe("https://example.com/b.jpg"))
        await asyncio.sleep(0)

        follower.cancel()
        try:
            await follower
        except asyncio.CancelledError:
            pass

        late = asyncio.create_task(cache.get_or_describe("https://example.com/b.jpg"))
        describe.release.set()
        assert await asyncio.wait_for(leader, timeout=2.0) == ("/tmp/img.jpg", "一只橘猫")
        assert await asyncio.wait_for(late, timeout=2.0) == ("/tmp/img.jpg", "一只橘猫")
        assert describe.calls == 1, describe.calls


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if asyncio.iscoroutinefunction(t):
            asyncio.run(t())
        else:
            t()
        print(f"PASS {t.__name__}")
    print("\nALL ISSUE #128 TESTS PASSED")


if __name__ == "__main__":
    main()
