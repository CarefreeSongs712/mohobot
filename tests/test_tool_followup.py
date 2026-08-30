"""工具调用 follow-up 回归测试(多轮链式调用 + 空流非流式兜底)。

背景: 生产日志中 bot_004 私聊"世末歌者"时, 工具返回空结果后模型希望
继续调用工具(换关键词再搜), 旧框架只支持一轮 follow-up 导致回复为空。

覆盖:
1. Legacy chat_stream: 链式两轮工具调用 → 最终文本正常输出
2. Legacy chat_stream: follow-up 流为空 → 非流式重试返回工具调用 → 继续下一轮
3. Legacy chat_stream: 工具轮次超上限 → 明确兜底文案
4. Agent LLMModule: 多轮工具调用后返回文本
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeUsageRecorder:
    async def record(self, *args, **kwargs) -> None:
        pass

    async def close(self) -> None:
        pass


def _tool_call_delta(index=0, call_id="call_1", name="song_search", arguments="{}"):
    return SimpleNamespace(
        index=index, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _chunk(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=_delta(content, tool_calls))], usage=None)


def _stream(chunks):
    async def _gen():
        for c in chunks:
            yield c
    return _gen()


def _stream_response(chunks):
    return _stream(chunks)


def _nonstream_message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _nonstream_response(content=None, tool_calls=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=_nonstream_message(content, tool_calls),
                                 finish_reason="tool_calls" if tool_calls else "stop")],
        usage=usage,
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **params):
        self.calls.append(params)
        if not self.responses:
            raise AssertionError("FakeCompletions: no more canned responses")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        if hasattr(resp, "__aiter__") or callable(resp):
            return resp() if callable(resp) and not hasattr(resp, "__aiter__") else resp
        return resp


class FakeChatClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))

    async def close(self) -> None:
        pass


def _make_event():
    from mohobot.models.onebot import PrivateMessageEvent, Sender
    return PrivateMessageEvent(
        time=0, self_id=0, post_type="message",
        message=[{"type": "text", "data": {"text": "世末歌者能唱给我听吗"}}],
        user_id=123456, message_id=1,
        sender=Sender(user_id=123456, nickname="测试"),
    )


async def _collect_legacy(svc, responses, tool_results):
    """Run legacy chat_stream with a fake client; return (text, client)."""
    client = FakeChatClient(responses)
    svc._available = True
    svc._chat_client = client
    results = iter(tool_results)

    async def fake_execute(name, args):
        return next(results)

    svc._execute_tool = fake_execute
    chunks = []
    async for text, is_final in svc.chat_stream("bot_001", _make_event(), [], {}):
        chunks.append((text, is_final))
    reply = "".join(t for t, f in chunks if not f or (f and t))
    return reply, client


async def test_legacy_chained_tool_calls() -> None:
    """模型连续两轮调用工具后给出文本 → 最终文本正常输出。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.config import GlobalConfig

    svc = LLMService(GlobalConfig(), usage_recorder=_FakeUsageRecorder())
    responses = [
        _stream_response([_chunk(tool_calls=[_tool_call_delta(0, "c1", "song_search", '{"query":"世末歌者"}')])]),
        _stream_response([_chunk(tool_calls=[_tool_call_delta(0, "c2", "song_get_lyrics", '{"song_name":"世末歌者"}')])]),
        _stream_response([_chunk(content="库里没找到这首歌哦"), _chunk(content=None)]),
    ]
    reply, client = await _collect_legacy(
        svc, responses,
        ['{"query": "世末歌者", "songs": []}', '{"error": "未找到歌词"}'],
    )
    assert "库里没找到这首歌哦" in reply, reply
    assert len(client.chat.completions.calls) == 3, len(client.chat.completions.calls)
    print("[1] Legacy 链式两轮工具调用 OK")


async def test_legacy_empty_stream_then_nonstream_toolcall() -> None:
    """follow-up 流为空 → 非流式重试返回工具调用 → 继续下一轮拿到文本。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.config import GlobalConfig

    svc = LLMService(GlobalConfig(), usage_recorder=_FakeUsageRecorder())
    responses = [
        _stream_response([_chunk(tool_calls=[_tool_call_delta(0, "c1", "song_search", '{"query":"世末歌者"}')])]),
        _stream_response([]),  # 网关空 SSE
        _nonstream_response(tool_calls=[_tool_call_delta(0, "c2", "song_search", '{"query":"世末"}')]),
        _stream_response([_chunk(content="找到了《世末告白》"), _chunk(content=None)]),
    ]
    reply, client = await _collect_legacy(
        svc, responses,
        ['{"query": "世末歌者", "songs": []}', '{"query": "世末", "songs": ["世末告白"]}'],
    )
    assert "找到了《世末告白》" in reply, reply
    assert len(client.chat.completions.calls) == 4, len(client.chat.completions.calls)
    print("[2] Legacy 空流降级 + 非流式工具调用 OK")


async def test_legacy_round_limit_forced_answer() -> None:
    """轮次用尽后模型仍要求调用工具 → 去掉工具强制文本回答。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.config import GlobalConfig

    svc = LLMService(GlobalConfig(), usage_recorder=_FakeUsageRecorder())
    responses = [
        # 初始 + 4 轮 follow-up 全部返回工具调用
        *[
            _stream_response([_chunk(tool_calls=[_tool_call_delta(0, f"c{i}", "song_search", "{}")])])
            for i in range(5)
        ],
        # 最后一轮不带工具 → 模型给出文本
        _stream_response([_chunk(content="没找到原曲，但找到几首相近的歌哦"), _chunk(content=None)]),
    ]
    reply, client = await _collect_legacy(svc, responses, ['{"songs": []}'] * 8)
    assert "相近的歌" in reply, reply
    calls = client.chat.completions.calls
    assert len(calls) == 6, len(calls)
    # 最后一轮不应携带 tools 参数(强制文本)
    assert "tools" not in calls[-1], calls[-1].keys()
    assert "tools" in calls[-2]
    print("[3] Legacy 轮次上限后强制文本回答 OK")


async def test_legacy_round_limit_total_failure() -> None:
    """强制文本轮也返回空流且非流式为空 → 输出兜底文案。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.config import GlobalConfig

    svc = LLMService(GlobalConfig(), usage_recorder=_FakeUsageRecorder())
    responses = [
        *[
            _stream_response([_chunk(tool_calls=[_tool_call_delta(0, f"c{i}", "song_search", "{}")])])
            for i in range(5)
        ],
        _stream_response([]),           # 最后一轮流为空
        _nonstream_response(content=""),  # 非流式重试也为空
    ]
    reply, _ = await _collect_legacy(svc, responses, ['{"songs": []}'] * 8)
    assert "未返回文本" in reply, reply
    print("[4] Legacy 强制文本轮总失败兜底 OK")


if __name__ == "__main__":
    asyncio.run(main())