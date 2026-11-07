"""单回合分段总量熔断回归 — LLM 退化复读不得灌满出站队列。

生产事故(2026-11-01): 一次 LLM 生成退化为 8.2 万字复读, 分段器切成
7161 段全部入队, 以限速节奏连续刷屏近 2 小时, /stop 与 /clear 都停不住
(内容生成完成时已整体入队)。熔断: 单回合 attempted 分段达
_MAX_SEGMENTS_PER_TURN 后, 剩余分段不再发送, [reply] 日志以
``(segment cap: -N)`` 标注截断数。覆盖流式与非流式两条路径;
主动消息(send_proactive_reply)与被动回复共用 _send_full_text, 同受保护。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from mohobot.message_handler import MessageHandler  # noqa: E402


def _capture_logs():
    """捕获 loguru 输出的最小 harness(与 test_observability_logging 一致)。"""
    msgs: list[str] = []
    sink_id = logger.add(msgs.append, level="INFO", format="{message}", colorize=False)
    return msgs, sink_id


def _make_handler(sent: list[str], *, cap: int | None = None, chunks: list | None = None):
    """构造只替换最终发送边界的真实 MessageHandler(分段/熔断/治理全真实)。

    chunks 非 None 时注入假 LLM 流, 供流式路径测试使用。
    """
    handler = object.__new__(MessageHandler)
    handler._ws = None
    handler._segment_reply = True
    handler._stream = True
    handler._reply_quote = False
    handler._seg_delay_min = 0
    handler._seg_delay_max = 0
    handler._seg_min_len = 12
    handler._seg_max_len = 60
    if cap is not None:
        handler._MAX_SEGMENTS_PER_TURN = cap

    class _Llm:
        async def chat_stream(self, **kwargs):
            for chunk, is_final in (chunks or []):
                yield chunk, is_final

    async def _send_message(bot_id, event, message, source="llm"):
        sent.append(message if isinstance(message, str) else str(message))
        return None

    handler._llm = _Llm()
    handler._send_message = _send_message
    handler._submit_tts = lambda *args, **kwargs: None
    return handler


def _flood_text(paragraphs: int) -> str:
    """模拟退化复读: N 个独立段落(双换行必然切段, 每段一个气泡)。"""
    return "第一句话内容足够长了。\n\n" * paragraphs


def _stream_chunks(text: str, piece: int = 5) -> list[tuple[str, bool]]:
    """把整段文本切成流式 chunk 序列, 最后一块 is_final=True。"""
    chunks = [(text[i:i + piece], False) for i in range(0, len(text), piece)]
    chunks[-1] = (chunks[-1][0], True)
    return chunks


def test_default_cap_is_20():
    """熔断默认值固化: 正常回复个位数段, 20 留足余量; 防止被无意改小/改没。"""
    assert MessageHandler._MAX_SEGMENTS_PER_TURN == 20


async def test_send_full_text_caps_flood():
    """非流式路径: 30 段回复只发前 20 段, 日志有 [segment-cap] 告警与截断数。"""
    sent: list[str] = []
    handler = _make_handler(sent)
    msgs, sid = _capture_logs()
    try:
        ok = await handler._send_full_text("bot", None, _flood_text(30))
    finally:
        logger.remove(sid)

    assert ok is True
    assert len(sent) == 20, f"应只发送 {MessageHandler._MAX_SEGMENTS_PER_TURN} 段, 实际 {len(sent)}"
    assert all(s == "第一句话内容足够长了。" for s in sent), sent[:3]
    lines = [m[:-1] if m.endswith("\n") else m for m in msgs]
    assert any("[segment-cap]" in m for m in lines), lines
    reply_line = next(m for m in lines if "[reply]" in m)
    assert "segments=20" in reply_line, reply_line
    assert "(segment cap: -10)" in reply_line, reply_line


async def test_send_full_text_under_cap_unaffected():
    """回归: 上限以内的回复逐段全发, 不出现熔断告警与截断备注。"""
    sent: list[str] = []
    handler = _make_handler(sent)
    msgs, sid = _capture_logs()
    try:
        ok = await handler._send_full_text("bot", None, _flood_text(5))
    finally:
        logger.remove(sid)

    assert ok is True
    assert len(sent) == 5, sent
    lines = [m[:-1] if m.endswith("\n") else m for m in msgs]
    assert not any("[segment-cap]" in m for m in lines), lines
    reply_line = next(m for m in lines if "[reply]" in m)
    assert "segments=5" in reply_line and "segment cap" not in reply_line, reply_line


async def test_stream_reply_caps_flood():
    """流式路径: 跨 chunk 累计计数, 达上限后剩余分段不再发送; 返回值不变
    (整段文本仍回传给调用方做上下文回写, 熔断只作用于发送端)。"""
    sent: list[str] = []
    text = _flood_text(30)
    handler = _make_handler(sent, chunks=_stream_chunks(text))
    msgs, sid = _capture_logs()
    try:
        result = await handler._stream_llm_reply("bot", None, [], {},
                                                 reply_options={"bot_config": None})
    finally:
        logger.remove(sid)

    assert len(sent) == 20, f"应只发送 20 段, 实际 {len(sent)}"
    assert result == text, "熔断不得改变回传全文(上下文回写语义不变)"
    lines = [m[:-1] if m.endswith("\n") else m for m in msgs]
    assert any("[segment-cap]" in m for m in lines), lines
    reply_line = next(m for m in lines if "[reply]" in m)
    assert "(segment cap: -10)" in reply_line, reply_line


async def test_stream_reply_exact_cap_boundary_no_truncation():
    """边界: 段数恰好等于上限时全部发出, 不触发告警与截断备注。"""
    sent: list[str] = []
    handler = _make_handler(sent, cap=4, chunks=_stream_chunks(_flood_text(4)))
    msgs, sid = _capture_logs()
    try:
        await handler._stream_llm_reply("bot", None, [], {},
                                        reply_options={"bot_config": None})
    finally:
        logger.remove(sid)

    assert len(sent) == 4, sent
    lines = [m[:-1] if m.endswith("\n") else m for m in msgs]
    assert not any("[segment-cap]" in m for m in lines), lines


async def test_proactive_reply_inherits_cap():
    """主动消息走 send_proactive_reply → _send_full_text, 同受熔断保护。"""
    sent: list[str] = []
    handler = _make_handler(sent)
    msgs, sid = _capture_logs()
    try:
        await handler.send_proactive_reply("bot", None, _flood_text(30))
    finally:
        logger.remove(sid)

    assert len(sent) == 20, f"主动消息同样只应发送 20 段, 实际 {len(sent)}"
    lines = [m[:-1] if m.endswith("\n") else m for m in msgs]
    assert any("[segment-cap]" in m for m in lines), lines


async def test_stream_reply_caps_trailing_buffer_at_default_cap():
    """回归: 恰好发满 20 段后, 流末剩余的无标点尾部不得绕过熔断再发第 21 段。"""
    text = _flood_text(20) + "尾部无标点文本"
    sent: list[str] = []
    handler = _make_handler(sent, chunks=_stream_chunks(text, piece=5))
    msgs, sid = _capture_logs()
    try:
        result = await handler._stream_llm_reply("bot", None, [], {},
                                                 reply_options={"bot_config": None})
    finally:
        logger.remove(sid)

    assert len(sent) == 20, f"默认上限 20, 尾段不得绕过, 实际发送 {len(sent)} 段: {sent[-2:]}"
    assert result == text, "熔断只作用于发送端, 回传全文不变"
    lines = [m[:-1] if m.endswith("\n") else m for m in msgs]
    assert any("[segment-cap]" in m for m in lines), lines
    reply_line = next(m for m in lines if "[reply]" in m)
    assert "(segment cap: -1)" in reply_line, reply_line


async def test_stream_reply_caps_trailing_buffer_at_small_cap():
    """小上限场景: 2 个完整段 + 无标点尾部 → 只发 2 段, 尾部计入截断数。"""
    sent: list[str] = []
    text = _flood_text(2) + "尾部缓冲文字"
    handler = _make_handler(sent, cap=2, chunks=_stream_chunks(text, piece=5))
    msgs, sid = _capture_logs()
    try:
        result = await handler._stream_llm_reply("bot", None, [], {},
                                                 reply_options={"bot_config": None})
    finally:
        logger.remove(sid)

    assert len(sent) == 2, sent
    assert result == text
    lines = [m[:-1] if m.endswith("\n") else m for m in msgs]
    assert any("[segment-cap]" in m for m in lines), lines
    reply_line = next(m for m in lines if "[reply]" in m)
    assert "(segment cap: -1)" in reply_line, reply_line
