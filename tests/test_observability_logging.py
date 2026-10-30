"""可观测性日志回归 — [tool]/[inject]/[llm]/[reply] 四类 info 日志与换行修复。

背景(2026-10-25 生产排查《底色》歌词事件): bot 行为追溯缺日志——工具调用无记录、
歌曲注入命中无记录、LLM 调用与回复完成日志不带 bot/会话标识且无正文, 只能翻
history JSONL 反推; 且日志记录间大面积缺换行无法按行 grep。

本文件全部用运行时断言(捕获 loguru 输出), 不用 inspect.getsource 字符串断言,
以免字段/调用链被改坏后测试仍然通过。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from mohobot.models.onebot import (  # noqa: E402
    GroupMessageEvent,
    PrivateMessageEvent,
    Sender,
)


def _capture_logs():
    """捕获 loguru 输出的最小 harness(每条记录含 loguru 自动追加的结尾换行)。"""
    msgs: list[str] = []
    sink_id = logger.add(msgs.append, level="INFO", format="{message}", colorize=False)
    return msgs, sink_id


def _lines(msgs: list[str]) -> list[str]:
    """去掉 loguru 自动追加的结尾换行; 若记录内部还有换行则保留(用于检测伪造)。"""
    return [m[:-1] if m.endswith("\n") else m for m in msgs]


def _first(msgs: list[str], prefix: str) -> str:
    hits = [m for m in _lines(msgs) if m.startswith(prefix)]
    assert hits, f"缺少 {prefix} 日志: {_lines(msgs)!r}"
    return hits[0]


def _group_event(group_id: int = 123, user_id: int = 7, text: str = "唱《底色》"):
    return GroupMessageEvent(
        time=1, self_id=100, post_type="message", group_id=group_id,
        user_id=user_id, message_id=1,
        message=[{"type": "text", "data": {"text": text}}],
        sender=Sender(user_id=user_id, nickname="测试"),
    )


def _private_event(user_id: int = 456, text: str = "你好"):
    return PrivateMessageEvent(
        time=1, self_id=100, post_type="message", user_id=user_id,
        message_id=1, message=[{"type": "text", "data": {"text": text}}],
        sender=Sender(user_id=user_id, nickname="测试"),
    )


def test_file_format_ends_with_newline():
    """⑤ 换行修复: 两个格式模板末尾必须带 \\n(否则记录粘连)。"""
    from mohobot.utils.logger import _console_format, _file_format

    rec = {"time": __import__("datetime").datetime(2026, 10, 25, 12, 0, 0)}
    assert _file_format(rec).endswith("\n"), "file 格式缺换行 → 记录粘连"
    assert _console_format(rec).endswith("\n"), "console 格式缺换行"


def test_describe_chat():
    """LLMService._describe_chat: 事件 → 会话标识。"""
    from mohobot.llm_service import LLMService

    assert LLMService._describe_chat(SimpleNamespace(group_id=123, user_id=1)) == "group:123"
    assert LLMService._describe_chat(SimpleNamespace(user_id=42)) == "private:42"
    assert LLMService._describe_chat(None) == "?"


# ── N1: [tool] 会话标识来自 _tool_call_context 的 chat_type/chat_id ──


async def test_tool_log_uses_tool_context_chat_fields():
    """_tool_call_context 返回的 dict 没有 event 键, chat 必须由 chat_type/chat_id 构造。"""
    from mohobot.llm_service import LLMService

    svc = object.__new__(LLMService)
    svc._ws = object()  # 非 None 才会生成工具上下文
    svc._cfg = SimpleNamespace(snitch_admin_group=0)
    svc._bot_manager = None

    group_ctx = svc._tool_call_context("bot_010", _group_event())
    assert group_ctx is not None and "event" not in group_ctx, group_ctx
    private_ctx = svc._tool_call_context("bot_011", _private_event())

    msgs, sid = _capture_logs()
    try:
        await svc._execute_tool("get_current_time", "{}", context=group_ctx)
        await svc._execute_tool("get_current_time", "{}", context=private_ctx)
        lines = _lines(msgs)
        group_line = next(m for m in lines if "bot=bot_010" in m)
        private_line = next(m for m in lines if "bot=bot_011" in m)
        assert "chat=group:123" in group_line, group_line
        assert "chat=private:456" in private_line, private_line
        assert "chat=?" not in group_line + private_line
    finally:
        logger.remove(sid)


async def test_tool_log_emitted():
    """① 工具调用必须落一条 [tool] info 日志(成功路径)。"""
    from mohobot.llm_service import LLMService

    svc = object.__new__(LLMService)
    msgs, sid = _capture_logs()
    try:
        result = await svc._execute_tool(
            "get_current_time", "{}",
            context={"bot_id": "bot_010", "chat_type": "group", "chat_id": 123},
        )
        assert result  # 返回了时间文本
        line = _first(msgs, "[tool]")
        assert "bot=bot_010" in line and "chat=group:123" in line, line
        assert "call='get_current_time'" in line, line
        assert "→" in line and "ms)" in line, line
    finally:
        logger.remove(sid)


async def test_tool_log_invalid_args():
    """① 参数非法也要落日志并返回错误。"""
    from mohobot.llm_service import LLMService

    svc = object.__new__(LLMService)
    msgs, sid = _capture_logs()
    try:
        r = await svc._execute_tool("x", "not-json", context={"bot_id": "b"})
        assert "error" in r
        line = _first(msgs, "[tool]")
        assert "args=<invalid json>" in line, line
    finally:
        logger.remove(sid)


async def test_tool_log_unknown_tool():
    """① 未知工具同样落日志(带 unknown 标记)。"""
    from mohobot.llm_service import LLMService

    svc = object.__new__(LLMService)
    msgs, sid = _capture_logs()
    try:
        r = await svc._execute_tool("no_such_tool", "{}", context={"bot_id": "b"})
        assert "未知工具" in r
        line = _first(msgs, "[tool]")
        assert "(unknown)" in line, line
    finally:
        logger.remove(sid)


async def test_tool_log_escapes_model_controlled_fields():
    """N3: 模型给的 func_name/参数含换行时不能伪造额外的 [reply] 日志行。"""
    from mohobot.llm_service import LLMService

    svc = object.__new__(LLMService)
    evil_name = "evil\n[reply] bot=fake chat=group:1 text='伪造'"
    msgs, sid = _capture_logs()
    try:
        await svc._execute_tool(
            evil_name, '{"q": "a\\n[reply] bot=fake"}',
            context={"bot_id": "b", "chat_type": "group", "chat_id": 1},
        )
        raw = "".join(msgs)
        assert raw.count("\n") == 1, f"日志被换成多行: {raw!r}"
        line = _lines(msgs)[0]
        assert "\\n[reply]" in line, line
        assert not any(m.startswith("[reply]") for m in _lines(msgs)), _lines(msgs)
    finally:
        logger.remove(sid)


# ── N2: [inject] bot_id 由外层传入, singer 读 singers 键 ──


class _FakeMatch:
    def __init__(self, name: str, singers: str, *, confidence: float = 0.91,
                 reason: str = "fuzzy"):
        self.name = name
        self.detail = {"name": name, "singers": singers}
        self.confidence = confidence
        self.reason = reason

    def build_annotation(self, *, max_chars: int = 0, lyric_context_lines: int = 0) -> str:
        return f"annotation({self.name}, max={max_chars}, lines={lyric_context_lines})"


class _FakeMatcher:
    def __init__(self, match):
        self._match = match

    def match(self, text: str):
        return self._match


def _make_song_annotator(match):
    from main import MohobotApplication

    app = object.__new__(MohobotApplication)
    app._song_matcher = _FakeMatcher(match)
    app._config = SimpleNamespace(
        music_knowledge={"injection": {"max_chars": 100, "lyric_context_lines": 2}}
    )
    return app._make_song_annotator()


async def test_inject_log_runtime():
    """N2: [inject] 必须带外层 bot_id、chat、歌名/歌手(singers 键)与规模。"""
    annotator = _make_song_annotator(_FakeMatch("底色", "洛天依"))
    msgs, sid = _capture_logs()
    try:
        result = await annotator(_group_event(), "bot_010")
        assert result.startswith("annotation(底色")
        line = _first(msgs, "[inject]")
        assert "bot=bot_010" in line, line
        assert "chat=group:123" in line, line
        assert "song='底色'" in line, line
        assert "singer='洛天依'" in line, line
        assert "conf=0.91(fuzzy)" in line, line
        assert "chars=" in line and "lyric_lines=2" in line, line
    finally:
        logger.remove(sid)


async def test_build_messages_passes_bot_id_to_annotator():
    """N2 集成: _build_messages 把外层 bot_id 传给歌曲注解回调(事件自身没有该属性)。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.config import GlobalConfig

    seen: list[str] = []

    async def annotator(event, bot_id: str = "?"):
        seen.append(bot_id)
        return ""

    svc = LLMService(GlobalConfig(), song_annotator=annotator)
    await svc._build_messages("bot_010", _private_event(), context=[])
    assert seen == ["bot_010"], seen


async def test_inject_log_escapes_song_fields():
    """N3: 歌名/歌手含换行时不能伪造额外的 [reply] 日志行。"""
    annotator = _make_song_annotator(
        _FakeMatch("底色\n[reply] bot=fake", "洛天依\n[reply] bot=fake")
    )
    msgs, sid = _capture_logs()
    try:
        await annotator(_group_event(), "bot_010")
        raw = "".join(msgs)
        assert raw.count("\n") == 1, f"日志被换成多行: {raw!r}"
        line = _lines(msgs)[0]
        assert "\\n[reply]" in line, line
        assert not any(m.startswith("[reply]") for m in _lines(msgs)), _lines(msgs)
    finally:
        logger.remove(sid)


# ── [llm] 运行时日志 + 工具 schema 只取一次 ──


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


class _FakeCompletions:
    def __init__(self, stream):
        self._stream = stream

    async def create(self, **kwargs):
        return self._stream


class _FakeClient:
    def __init__(self, stream):
        self.chat = SimpleNamespace(completions=_FakeCompletions(stream))


async def test_llm_log_runtime_and_schema_snapshot():
    """③ [llm] 运行时落日志; 同一请求内 _current_tools_schemas 只调用一次。"""
    from mohobot.llm_service import LLMService

    content_delta = SimpleNamespace(content="hi", tool_calls=None)
    chunk = SimpleNamespace(
        usage=None, choices=[SimpleNamespace(delta=content_delta)]
    )
    client = _FakeClient(_FakeStream([chunk]))

    svc = object.__new__(LLMService)
    svc._available = True
    svc._cfg = SimpleNamespace(
        llm=SimpleNamespace(chat_temperature=0.7, chat_max_tokens=100)
    )
    svc._resolve_chat_target = lambda bot_config: (client, "test-model", "p")

    async def _build_messages(*a, **k):
        return [{"role": "system", "content": ""}]

    svc._build_messages = _build_messages
    schema_calls: list[int] = []

    def _schemas():
        schema_calls.append(1)
        return [{"function": {"name": "t"}}]

    svc._current_tools_schemas = _schemas

    msgs, sid = _capture_logs()
    try:
        out = [piece async for piece in svc.chat_stream(
            "bot_010", _private_event(), context=[], raw_event={},
        )]
        assert any(piece for piece, _final in out)
        line = _first(msgs, "[llm]")
        assert "bot=bot_010" in line and "chat=private:456" in line, line
        assert "model=test-model" in line and "tools=1" in line, line
        assert "stream=true" in line, line
        assert schema_calls == [1], f"schema 被重复计算 {len(schema_calls)} 次"
    finally:
        logger.remove(sid)


# ── N4: [reply] 覆盖非流式 / 单条 / 主动消息 / 流式 ──


def _make_handler(*, segment_reply: bool, stream: bool = True, llm=None):
    from mohobot.message_handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._llm = llm
    handler._ws = None
    handler._segment_reply = segment_reply
    handler._stream = stream
    handler._ooc_guard = False
    handler._strip_english_fillers = False
    handler._reply_quote = False
    handler._seg_min_len = 1
    handler._seg_max_len = 8
    handler._seg_delay_min = 0
    handler._seg_delay_max = 0
    handler._sent: list = []

    async def _send_message(bot_id, event, message, **kwargs):
        handler._sent.append(message)

    def _submit_tts(*a, **k):
        return None

    handler._send_message = _send_message
    handler._submit_tts = _submit_tts
    return handler


async def test_reply_log_streaming_runtime():
    """④ 流式分段完成落 [reply] 日志(bot/会话/段数/字数/预览)。"""
    class _LLM:
        async def chat_stream(self, **kwargs):
            yield ("你好。", False)
            yield ("世界。", False)
            yield ("", True)

    handler = _make_handler(segment_reply=True, stream=True, llm=_LLM())
    msgs, sid = _capture_logs()
    try:
        reply = await handler._stream_llm_reply(
            "bot_010", _group_event(), [], {}, reply_options={},
        )
        assert reply == "你好。世界。", reply
        line = _first(msgs, "[reply]")
        assert "bot=bot_010" in line and "chat=group:123" in line, line
        assert "segments=" in line and "chars=" in line, line
        assert "你好。世界。" in line, line
    finally:
        logger.remove(sid)


async def test_reply_log_nonstream_runtime():
    """N4: _stream=False 的非流式分段路径也要落 [reply] 日志。"""
    class _LLM:
        async def chat(self, **kwargs):
            return "第一句。第二句。", None

    handler = _make_handler(segment_reply=True, stream=False, llm=_LLM())
    msgs, sid = _capture_logs()
    try:
        reply = await handler._stream_llm_reply(
            "bot_010", _group_event(), [], {}, reply_options={},
        )
        assert reply == "第一句。第二句。", reply
        line = _first(msgs, "[reply]")
        assert "bot=bot_010" in line and "chat=group:123" in line, line
        assert "第一句" in line, line
    finally:
        logger.remove(sid)


async def test_reply_log_single_send_runtime():
    """N4: 单条发送路径(_segment_reply=False)也要落 [reply] 日志。"""
    class _LLM:
        async def chat_stream(self, **kwargs):
            yield ("单条回复", False)
            yield ("", True)

    handler = _make_handler(segment_reply=False, llm=_LLM())
    msgs, sid = _capture_logs()
    try:
        reply = await handler._stream_llm_reply(
            "bot_010", _private_event(), [], {}, reply_options={},
        )
        assert reply == "单条回复", reply
        line = _first(msgs, "[reply]")
        assert "bot=bot_010" in line and "chat=private:456" in line, line
        assert "单条回复" in line, line
    finally:
        logger.remove(sid)


async def test_reply_log_proactive_runtime():
    """N4: 主动消息 send_proactive_reply 也要落 [reply] 日志(标注 proactive)。"""
    handler = _make_handler(segment_reply=False)
    msgs, sid = _capture_logs()
    try:
        reply = await handler.send_proactive_reply(
            "bot_010", _private_event(), "主动问好",
        )
        assert reply == "主动问好", reply
        line = _first(msgs, "[reply]")
        assert "bot=bot_010" in line and "chat=private:456" in line, line
        assert "主动问好" in line and "(proactive)" in line, line
    finally:
        logger.remove(sid)


async def test_reply_log_proactive_segmented_runtime():
    """N4: 分段版主动消息经 _send_full_text 同样落 [reply] 日志。"""
    handler = _make_handler(segment_reply=True)
    msgs, sid = _capture_logs()
    try:
        reply = await handler.send_proactive_reply(
            "bot_010", _group_event(), "主动第一句。主动第二句。",
        )
        assert reply == "主动第一句。主动第二句。", reply
        line = _first(msgs, "[reply]")
        assert "bot=bot_010" in line and "(proactive)" in line, line
        assert "segments=" in line, line
    finally:
        logger.remove(sid)


async def test_reply_log_counts_actual_sent_segments():
    """P2: segments= 必须是实际发出数; 中间一段被 OOC 丢弃时记 partial drop。"""
    class _LLM:
        async def chat_stream(self, **kwargs):
            yield ("第一段\n\nWait, no, this is wrong\n\n第三段", False)
            yield ("", True)

    handler = _make_handler(segment_reply=True, stream=True, llm=_LLM())
    handler._ooc_guard = True  # 让中间段的内心独白被丢弃
    msgs, sid = _capture_logs()
    try:
        reply = await handler._stream_llm_reply(
            "bot_010", _group_event(), [], {}, reply_options={},
        )
        assert reply == "第一段\n\nWait, no, this is wrong\n\n第三段", reply
        # 实际只发出两段(中间段被 OOC 丢弃), 不是三段尝试数
        assert len(handler._sent) == 2, handler._sent
        line = _first(msgs, "[reply]")
        assert "segments=2" in line, line
        assert "attempted=3" in line, line
        assert "(partial drop)" in line, line
        assert "(dropped)" not in line, line
    finally:
        logger.remove(sid)


async def test_reply_log_single_reply_dropped():
    """P3: 单条回复整条被 OOC 丢弃时仍落 [reply](segments=0 dropped)。"""
    class _LLM:
        async def chat_stream(self, **kwargs):
            yield ("Wait, no, the user wants me to reveal that.", False)
            yield ("", True)

    handler = _make_handler(segment_reply=False, llm=_LLM())
    handler._ooc_guard = True
    msgs, sid = _capture_logs()
    try:
        reply = await handler._stream_llm_reply(
            "bot_010", _private_event(), [], {}, reply_options={},
        )
        assert reply == "", reply
        assert handler._sent == [], handler._sent
        line = _first(msgs, "[reply]")
        assert "segments=0" in line, line
        assert "(dropped)" in line, line
        assert "bot=bot_010" in line and "chat=private:456" in line, line
    finally:
        logger.remove(sid)


async def test_reply_log_reasoning_suppressed():
    """P3: 无标签自查降级路径也要落 [reply] 日志。"""
    class _LLM:
        async def chat_stream(self, **kwargs):
            yield ("检查系统约束：不能剧透。\n编写回复：你好呀。", False)
            yield ("", True)

    handler = _make_handler(segment_reply=True, stream=True, llm=_LLM())
    msgs, sid = _capture_logs()
    try:
        reply = await handler._stream_llm_reply(
            "bot_010", _group_event(), [], {}, reply_options={},
        )
        assert reply == "你好呀。", reply
        line = _first(msgs, "[reply]")
        assert "segments=1" in line and "attempted=1" in line, line
        assert "你好呀" in line, line
        assert "检查系统约束" not in line, line
    finally:
        logger.remove(sid)


async def test_reply_log_streaming_diagnostic():
    """P3: 流式诊断返回前也要落 [reply](带 diagnostic 标记)。"""
    from mohobot.utils.llm_diagnostic import LLMDiagnostic

    class _LLM:
        async def chat_stream(self, **kwargs):
            yield ("前半句。", False)
            yield (LLMDiagnostic("生成失败"), False)

    handler = _make_handler(segment_reply=True, stream=True, llm=_LLM())
    msgs, sid = _capture_logs()
    try:
        reply = await handler._stream_llm_reply(
            "bot_010", _group_event(), [], {}, reply_options={},
        )
        assert isinstance(reply, LLMDiagnostic), reply
        line = _first(msgs, "[reply]")
        assert "segments=2" in line and "attempted=2" in line, line
        assert "(diagnostic)" in line, line
        assert "生成失败" in line, line
    finally:
        logger.remove(sid)


async def test_reply_log_text_newline_not_injected():
    """P3: 回复正文含换行/伪造前缀时, text=!r 只产生一条 [reply] 记录。"""
    class _LLM:
        async def chat_stream(self, **kwargs):
            yield ("第一行\n[reply] bot=fake chat=group:1 text='伪造'\n[tool] evil", False)
            yield ("", True)

    handler = _make_handler(segment_reply=False, llm=_LLM())
    msgs, sid = _capture_logs()
    try:
        await handler._stream_llm_reply(
            "bot_010", _group_event(), [], {}, reply_options={},
        )
        lines = _lines(msgs)
        reply_lines = [m for m in lines if m.startswith("[reply]")]
        assert len(reply_lines) == 1, lines
        assert not any(m.startswith("[tool]") for m in lines), lines
        assert "\\n[reply]" in reply_lines[0], reply_lines[0]
    finally:
        logger.remove(sid)


async def test_reply_log_empty_text_silent():
    """P3: 空文本不发送、不 warning、不记 [reply], 直接返回 False。"""
    handler = _make_handler(segment_reply=True)
    msgs, sid = _capture_logs()
    try:
        sent = await handler._send_full_text("bot_010", _group_event(), "   \n  ")
        assert sent is False, sent
        assert handler._sent == [], handler._sent
        assert not any(m.startswith("[reply]") for m in _lines(msgs)), _lines(msgs)
    finally:
        logger.remove(sid)


if __name__ == "__main__":
    failed = 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_"):
            continue
        try:
            if asyncio.iscoroutinefunction(_fn):
                asyncio.run(_fn())
            else:
                _fn()
            print(f"PASS {_name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {_name}: {e}")
    print("---")
    assert failed == 0, f"{failed} failed"
    print("ALL OBSERVABILITY LOGGING TESTS PASSED")
