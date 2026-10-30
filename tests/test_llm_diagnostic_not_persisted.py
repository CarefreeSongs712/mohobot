"""LLM 诊断文案不得落入记忆 — Issue #4 的 P1 部分。

缺陷: LLMService.chat()/chat_stream() 在未配置、调用失败、空响应时把诊断文字
("[LLM 调用失败: ...]" 等)当作回复返回; MessageHandler 只看 full_reply.strip() 是否非空,
于是这段诊断被写进 contexts/ 工作记忆与 SQLite conversations 表, 下一轮作为
"助手说过的话"喂回模型, 并进入总结压缩。主动聊天插件只识别 "LLM 服务未配置" 一种前缀,
API 失败的诊断会被当成主动消息正文发给用户。

覆盖三种回复模式(流式分段 / 流式不分段 / 非流式分段)与主动聊天, 全部走真实
MessageHandler / ContextManager / LLMService, 只替换 OpenAI 客户端与 WS 发送端。
"""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "proactive_chat"))

from mohobot.bot_manager import BotInstance, BotManager  # noqa: E402
from mohobot.context_manager import ContextManager  # noqa: E402
from mohobot.llm_service import LLMService  # noqa: E402
from mohobot.message_handler import MessageHandler  # noqa: E402
from mohobot.models.config import GlobalConfig, ReplyConfig  # noqa: E402
from mohobot.models.onebot import PrivateMessageEvent, Sender  # noqa: E402
from mohobot.utils.llm_diagnostic import LLMDiagnostic  # noqa: E402

MODES = [(True, True), (True, False), (False, True)]  # (stream, segment_reply)


class _Usage:
    async def record(self, *args, **kwargs):
        pass

    async def close(self):
        pass


class _FailingCompletions:
    """OpenAI 兼容客户端替身: 每次调用都抛出(模拟 429/网关故障)。"""

    async def create(self, **kwargs):
        raise RuntimeError("HTTP 429 rate limited")


class _EmptyStreamCompletions:
    """流式返回 0 个 chunk(网关不支持 max_tokens 组合时的真实现象)。"""

    async def create(self, **kwargs):
        async def empty():
            if False:
                yield None
        return empty()


class _WS:
    def __init__(self, manager):
        self._bot_manager = manager
        self.sent = []

    async def send_private_msg(self, bot_id, user_id, message, source="auto"):
        self.sent.append(message)

    async def send_group_msg(self, bot_id, group_id, message, source="auto"):
        self.sent.append(message)


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


def _sent_text(message):
    if isinstance(message, list):
        return "".join(seg.get("data", {}).get("text", "") for seg in message if seg.get("type") == "text")
    return str(message)


async def _setup(td, *, stream, segment, completions):
    manager = BotManager(data_dir=td)
    cfg = manager.create_bot(nickname="测试bot", qq=1001)
    manager._bots[cfg.bot_id] = BotInstance(cfg.bot_id, None, cfg)
    context = ContextManager(data_dir=td, summary_enabled=False, trim_at_rounds=100)
    llm = LLMService(GlobalConfig(), usage_recorder=_Usage())
    llm._available = True
    llm._chat_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    async def build_messages(*args, **kwargs):
        return [{"role": "user", "content": "你好"}]

    llm._build_messages = build_messages
    ws, db, emotion = _WS(manager), _RecordingDB(), _RecordingEmotion()
    handler = MessageHandler(
        ws, context, llm, None, data_dir=td, database_manager=db, emotion_manager=emotion,
        reply_config=ReplyConfig(stream=stream, segment_reply=segment,
                                 segment_delay_min=0, segment_delay_max=0),
    )
    return handler, context, ws, db, emotion, cfg.bot_id


async def test_llm_api_failure_is_shown_but_never_persisted():
    """API 调用失败: 用户能看到诊断, 但上下文/数据库/情感分析一个都不能收到。"""
    for stream, segment in MODES:
        with tempfile.TemporaryDirectory() as td:
            handler, context, ws, db, emotion, bot = await _setup(
                td, stream=stream, segment=segment, completions=_FailingCompletions(),
            )
            await handler._handle_message(bot, _event(), {})
            shown = " ".join(_sent_text(m) for m in ws.sent)
            mode = f"stream={stream} segment={segment}"
            assert "LLM 调用失败" in shown, (mode, ws.sent)
            saved = await context.load_context(bot, "private", "3001")
            assert saved == [], (mode, saved)
            assert db.rows == [], (mode, db.rows)
            assert emotion.turns == [], (mode, emotion.turns)
            await handler.close()


async def test_llm_unconfigured_diagnostic_is_not_persisted():
    """未配置 API Key: 同样只提示、不入记忆。"""
    for stream, segment in MODES:
        with tempfile.TemporaryDirectory() as td:
            handler, context, ws, db, emotion, bot = await _setup(
                td, stream=stream, segment=segment, completions=_FailingCompletions(),
            )
            handler._llm._available = False
            await handler._handle_message(bot, _event(), {})
            mode = f"stream={stream} segment={segment}"
            assert any("LLM 服务未配置" in _sent_text(m) for m in ws.sent), (mode, ws.sent)
            assert await context.load_context(bot, "private", "3001") == [], mode
            assert db.rows == [] and emotion.turns == [], mode
            await handler.close()


async def test_empty_stream_diagnostic_is_not_persisted():
    """流式返回空(0 chunk): 诊断不入记忆。"""
    for stream, segment in [(True, True), (True, False)]:
        with tempfile.TemporaryDirectory() as td:
            handler, context, ws, db, emotion, bot = await _setup(
                td, stream=stream, segment=segment, completions=_EmptyStreamCompletions(),
            )
            await handler._handle_message(bot, _event(), {})
            mode = f"stream={stream} segment={segment}"
            assert any("模型未返回内容" in _sent_text(m) for m in ws.sent), (mode, ws.sent)
            assert await context.load_context(bot, "private", "3001") == [], mode
            assert db.rows == [] and emotion.turns == [], mode
            await handler.close()


async def test_successful_reply_is_still_persisted():
    """正向对照: 正常回复照旧写入上下文、数据库并触发情感分析(防止修复变成全量不落盘)。"""

    class _OkCompletions:
        async def create(self, **kwargs):
            if kwargs.get("stream"):
                async def chunks():
                    yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
                        delta=SimpleNamespace(content="今天也很开心哦。", tool_calls=None))])
                return chunks()
            return SimpleNamespace(usage=None, choices=[SimpleNamespace(
                message=SimpleNamespace(content="今天也很开心哦。", tool_calls=None))])

    for stream, segment in MODES:
        with tempfile.TemporaryDirectory() as td:
            handler, context, ws, db, emotion, bot = await _setup(
                td, stream=stream, segment=segment, completions=_OkCompletions(),
            )
            await handler._handle_message(bot, _event(), {})
            mode = f"stream={stream} segment={segment}"
            saved = await context.load_context(bot, "private", "3001")
            assert [m["role"] for m in saved][-1] == "assistant", (mode, saved)
            assert saved[-1]["content"] == "今天也很开心哦。", (mode, saved)
            assert len(db.rows) == 2 and emotion.turns == ["今天也很开心哦。"], mode
            await handler.close()


async def test_proactive_chat_never_sends_llm_failure_text():
    """主动聊天: API 失败的诊断不能当成主动消息发给用户, 也不计入未回复次数。"""
    from proactive_chat_core.engine import ProactiveEngine, session_key

    class _Llm:
        _chat_client = object()

        async def chat(self, **kwargs):
            return LLMDiagnostic("[LLM 调用失败: HTTP 429 rate limited]"), None

    sent, appended = [], []

    class _MH:
        async def send_proactive_reply(self, bot_id, event, text):
            sent.append(text)
            return text

    class _Ctx:
        async def capture_session(self, *a, **k):
            return {"id": "s", "generation": "g", "persona_id": ""}

        async def load_context(self, *a, **k):
            return []

        async def append_context(self, *a, **k):
            appended.append(a)

    instance = SimpleNamespace(config=SimpleNamespace(persona_id="", chat_model_override="", nickname="b"), qq=111)
    refs = {
        "bot_manager": SimpleNamespace(get=lambda bot_id: instance),
        "llm_service": _Llm(), "message_handler": _MH(), "context_manager": _Ctx(),
        "persona_service": SimpleNamespace(resolve=lambda cfg, pid="": {"content": "x"}),
        "ws_server": None,
    }
    now = [1000.0]
    with tempfile.TemporaryDirectory() as td:
        engine = ProactiveEngine(
            state_path=Path(td) / "state.json",
            config={"enable": True, "min_interval_minutes": 1, "max_interval_minutes": 1,
                    "quiet_hours": "", "max_unanswered_times": 4, "exclude_users": [],
                    "proactive_prompt": "主动聊聊", "enable_auto_trigger": False,
                    "tick_interval_sec": 30},
            get_ref=refs.get, clock=lambda: now[0],
        )
        engine.plugin_start_time = now[0]

        def seed(state):
            state.setdefault("sessions", {})[session_key("bot_001", "123")] = {
                "unanswered_count": 0, "last_message_time": 1.0, "sender_name": "小明",
                "last_scheduled_at": now[0] - 60, "next_trigger_time": now[0],
            }

        await engine._mutate_state(seed)
        await engine.tick()

        assert sent == [], f"诊断被当作主动消息发出: {sent}"
        assert appended == [], appended
        from mohobot.file_store import json_read
        entry = (await json_read(Path(td) / "state.json"))["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 0
        assert entry["next_trigger_time"] == now[0] + 60, "失败后应正常重排"


async def test_stream_diagnostic_preserves_partial_body_order():
    """正文后紧跟半截标签再失败时: 正文、标签尾部、诊断必须保持流式顺序。

    复核曾发现旧实现先 finish() 发送半截标签尾部, 再发送尚在 buffer 的正文,
    实际顺序从 ``正文<te[diag]`` 变成 ``<te正文[diag]``。只替换 LLM 流和发送边界,
    仍走真实 MessageHandler._stream_llm_reply / ReplyArtifactFilter。
    """
    from mohobot.message_handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._ws = None
    handler._segment_reply = True
    handler._stream = True
    handler._reply_quote = False
    handler._seg_delay_min = 0
    handler._seg_delay_max = 0
    handler._seg_min_len = 12
    handler._seg_max_len = 60
    sent = []

    class _Llm:
        async def chat_stream(self, **kwargs):
            yield "正文", False
            yield "<te", False
            yield LLMDiagnostic("[diag]"), True

    async def send_segment(bot_id, event, text, first_sent, *,
                           quote=True, sent_counter=None):
        sent.append(text)
        if sent_counter is not None:
            sent_counter[0] += 1
        return True

    handler._llm = _Llm()
    handler._send_segment = send_segment
    handler._submit_tts = lambda *args: None

    result = await handler._stream_llm_reply("bot", None, [], {})

    assert sent == ["正文", "<te", "[diag]"], sent
    assert isinstance(result, LLMDiagnostic)
    assert result == "[diag]"


def test_diagnostic_marker_keeps_str_compatibility():
    """标记类型必须保持 str 兼容, 旧调用方不受影响; 字符串运算后标记丢失(调用方须先判断)。"""
    d = LLMDiagnostic("[LLM 调用失败: x]")
    assert isinstance(d, str) and d == "[LLM 调用失败: x]"
    assert d.startswith("[LLM") and len(d) == len("[LLM 调用失败: x]")
    assert not isinstance(d.strip(), LLMDiagnostic)
    assert not isinstance(d + "", LLMDiagnostic)


class _ToolThenFailCompletions:
    """第 1 次返回工具调用, 跟进调用抛异常 —— 非流式链式工具路径的失败出口。"""

    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(
                        id="call_1", type="function",
                        function=SimpleNamespace(name="unknown_test_tool", arguments="{}"),
                    )],
                ))],
                usage=None,
            )
        raise RuntimeError("HTTP 502 bad gateway")


async def test_nonstream_tool_followup_failure_keeps_diagnostic_marker():
    """非流式链式工具路径的"跟进失败"出口必须返回 LLMDiagnostic, 而不是普通 str。

    这是 #18 与 #19 合并时唯一的冲突点: 若把它解成 `return f"[LLM 调用失败: {e}]", ...`,
    诊断会被 MessageHandler 当成正文写回上下文/DB/情感, 静默重开 issue #4 的 P1。
    本用例把该出口钉住, 使错误的冲突解析会被测试直接杀死。
    """
    with tempfile.TemporaryDirectory() as td:
        handler, _, _, _, _, bot = await _setup(
            td, stream=False, segment=True, completions=_ToolThenFailCompletions(),
        )
        reply, tool_results = await handler._llm.chat(bot, _event(), [], {})
        assert isinstance(reply, LLMDiagnostic), type(reply)
        assert reply == "[LLM 调用失败: HTTP 502 bad gateway]", reply
        assert isinstance(tool_results, list) and tool_results, tool_results
        await handler.close()

    with tempfile.TemporaryDirectory() as td:
        handler, context, ws, db, emotion, bot = await _setup(
            td, stream=False, segment=True, completions=_ToolThenFailCompletions(),
        )
        await handler._handle_message(bot, _event(), {})
        shown = " ".join(_sent_text(m) for m in ws.sent)
        assert "LLM 调用失败" in shown, ws.sent
        assert await context.load_context(bot, "private", "3001") == []
        assert db.rows == [] and emotion.turns == [], (db.rows, emotion.turns)
        await handler.close()


class _ToolThenSilentCompletions:
    """第 1 次返回工具调用, 跟进轮只返回空文本 —— 非流式"工具跑完但模型无文本"出口。"""

    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(
                        id="call_1", type="function",
                        function=SimpleNamespace(name="unknown_test_tool", arguments="{}"),
                    )],
                ))],
                usage=None,
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=None))],
            usage=None,
        )


async def test_nonstream_tool_no_text_fallback_is_diagnostic_not_persisted():
    """非流式"工具调用完成，但模型未返回文本"兜底必须与流式同一兜底一致: 是 LLMDiagnostic。

    该文案是框架占位, 不是模型说的话。若返回普通 str, MessageHandler 会把它当正文
    写进上下文/数据库并触发情感分析, 下一轮模型会把占位当作"自己说过的话"。
    """
    with tempfile.TemporaryDirectory() as td:
        handler, _, _, _, _, bot = await _setup(
            td, stream=False, segment=True, completions=_ToolThenSilentCompletions(),
        )
        reply, tool_results = await handler._llm.chat(bot, _event(), [], {})
        assert isinstance(reply, LLMDiagnostic), type(reply)
        assert reply == "[工具调用完成，但模型未返回文本]", reply
        assert isinstance(tool_results, list) and tool_results, tool_results
        await handler.close()

    with tempfile.TemporaryDirectory() as td:
        handler, context, ws, db, emotion, bot = await _setup(
            td, stream=False, segment=True, completions=_ToolThenSilentCompletions(),
        )
        await handler._handle_message(bot, _event(), {})
        shown = " ".join(_sent_text(m) for m in ws.sent)
        assert "工具调用完成" in shown, ws.sent
        assert await context.load_context(bot, "private", "3001") == []
        assert db.rows == [] and emotion.turns == [], (db.rows, emotion.turns)
        await handler.close()
