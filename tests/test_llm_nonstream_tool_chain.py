"""非流式链式工具调用 — Issue #4 的 P2 部分。

缺陷: LLMService.chat()(非流式)执行完第一轮工具后只跟进一次; 跟进响应若再次返回
tool_calls(例如搜索为空后换关键词重搜、先搜索再查详情), 代码只读 content(此时为空),
第二轮工具被静默丢弃, 用户收到空回复。流式路径 chat_stream() 早已支持有界多轮。

触发入口: reply.stream=false 且 segment_reply=true 时, MessageHandler 走 chat()。

全部走真实 LLMService(及最后一个用例的真实 MessageHandler / ContextManager),
只替换 OpenAI 兼容客户端与 WS 发送端。
"""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.llm_service import LLMService  # noqa: E402
from mohobot.models.config import GlobalConfig  # noqa: E402


class _Usage:
    async def record(self, *args, **kwargs):
        pass

    async def close(self):
        pass


def _tool_call(call_id, name="song_search", arguments="{}"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _response(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=None,
    )


class _Completions:
    """按顺序返回预置响应, 并记录每次请求参数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("no more canned responses")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _service(responses, tool_outputs):
    svc = LLMService(GlobalConfig(), usage_recorder=_Usage())
    svc._available = True
    completions = _Completions(responses)
    svc._chat_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    async def build_messages(*args, **kwargs):
        return [{"role": "user", "content": "世末歌者能唱给我听吗"}]

    outputs = iter(tool_outputs)
    executed = []

    async def execute(name, arguments, *, context=None):
        executed.append((name, arguments))
        return next(outputs)

    svc._build_messages = build_messages
    svc._execute_tool = execute
    return svc, completions, executed


async def test_nonstream_mixed_text_and_tool_calls_is_preserved():
    """中间跟进同时带正文和 tool_calls: 正文不能被后续最终文本覆盖。"""
    svc, completions, executed = _service(
        [
            _response(tool_calls=[_tool_call("c1")]),
            _response(content="我先查一下。", tool_calls=[_tool_call("c2")]),
            _response(content="结果是晴天。"),
        ],
        ['{"query": "天气"}', '{"weather": "sunny"}'],
    )
    reply, tool_results = await svc.chat("bot_001", None, [], {})

    assert reply == "我先查一下。结果是晴天。", reply
    assert [item["tool_call_id"] for item in tool_results] == ["c1", "c2"]
    assert [name for name, _ in executed] == ["song_search", "song_search"]
    assert len(completions.calls) == 3
    await svc.close()


async def test_nonstream_chained_tool_calls_reach_final_text():
    """跟进响应再次要求调用工具 → 继续执行 → 拿到最终文本。"""
    svc, completions, executed = _service(
        [
            _response(tool_calls=[_tool_call("c1", "song_search", '{"query":"世末歌者"}')]),
            _response(tool_calls=[_tool_call("c2", "song_get_lyrics", '{"song_name":"世末歌者"}')]),
            _response(content="库里没找到这首歌的歌词哦"),
        ],
        ['{"songs": ["世末歌者"]}', '{"error": "未找到歌词"}'],
    )
    reply, tool_results = await svc.chat("bot_001", None, [], {})

    assert reply == "库里没找到这首歌的歌词哦", reply
    assert [item["tool_call_id"] for item in tool_results] == ["c1", "c2"]
    assert [name for name, _ in executed] == ["song_search", "song_get_lyrics"]
    assert len(completions.calls) == 3
    # 第二轮工具结果必须真的回传给模型
    last_messages = completions.calls[-1]["messages"]
    assert last_messages[-1] == {"role": "tool", "tool_call_id": "c2", "content": '{"error": "未找到歌词"}'}
    assert "tools" in completions.calls[1] and "tools" in completions.calls[2]
    await svc.close()


async def test_nonstream_round_limit_forces_text_answer():
    """与流式路径同一语义: 4 轮跟进仍带工具, 之后一轮不带工具强制给出文本。"""
    svc, completions, executed = _service(
        [*[_response(tool_calls=[_tool_call(f"c{i}")]) for i in range(5)],
         _response(content="没找到原曲, 但有几首相近的歌哦")],
        ['{"songs": []}'] * 5,
    )
    reply, tool_results = await svc.chat("bot_001", None, [], {})

    assert reply == "没找到原曲, 但有几首相近的歌哦", reply
    assert len(completions.calls) == 6, len(completions.calls)
    assert len(executed) == 5 and len(tool_results) == 5
    assert "tools" not in completions.calls[-1], "强制文本轮不应再提供工具"
    assert "tools" in completions.calls[-2]
    await svc.close()


async def test_nonstream_loop_is_bounded_even_if_gateway_keeps_returning_tools():
    """强制文本轮里网关仍返回 tool_calls: 不再执行, 循环必须终止。

    工具已执行但模型最终没给出文本 → 必须回落到与 chat_stream 相同的兜底文案,
    不能静默返回空串(否则 stream=false 的用户什么都收不到)。
    """
    svc, completions, executed = _service(
        [*[_response(tool_calls=[_tool_call(f"c{i}")]) for i in range(6)],
         _response(content="不应被请求到")],
        ['{"songs": []}'] * 10,
    )
    reply, tool_results = await svc.chat("bot_001", None, [], {})

    assert len(completions.calls) == 6, len(completions.calls)
    assert len(executed) == 5, "强制文本轮返回的工具调用不得执行"
    assert reply == "[工具调用完成，但模型未返回文本]", reply
    assert len(tool_results) == 5, tool_results
    await svc.close()


async def test_nonstream_empty_choices_keeps_accumulated_text():
    """跟进的响应没有 choices 时, 结束循环并保留此前已累积的正文(不再丢弃成空串)。"""
    empty_choices = SimpleNamespace(choices=[], usage=None)
    svc, completions, _ = _service(
        [_response(tool_calls=[_tool_call("c1")]),
         _response(content="前半句。", tool_calls=[_tool_call("c2")]),
         empty_choices],
        ['{"songs": []}'] * 4,
    )
    reply, tool_results = await svc.chat("bot_001", None, [], {})
    assert len(completions.calls) == 3, len(completions.calls)
    assert reply == "前半句。", reply
    assert len(tool_results) == 2, tool_results
    await svc.close()


async def test_nonstream_followup_failure_keeps_existing_behavior():
    """第 2 轮跟进调用异常: 与既有"第 1 轮跟进异常"的处理一致 —— 返回诊断文本,
    并保留已执行的全部工具结果(修复前根本走不到第 2 轮)。"""
    svc, completions, _ = _service(
        [_response(tool_calls=[_tool_call("c1")]),
         _response(tool_calls=[_tool_call("c2")]),
         RuntimeError("HTTP 502 bad gateway")],
        ['{"songs": []}', '{"songs": []}'],
    )
    reply, tool_results = await svc.chat("bot_001", None, [], {})

    assert reply == "[LLM 调用失败: HTTP 502 bad gateway]", reply
    assert [item["tool_call_id"] for item in tool_results] == ["c1", "c2"]
    await svc.close()


async def test_nonstream_single_round_unchanged():
    """一轮工具 + 文本: 行为与修复前完全一致(2 次请求)。"""
    svc, completions, _ = _service(
        [_response(tool_calls=[_tool_call("c1")]), _response(content="明天晴, 25 度")],
        ['{"weather": "sunny"}'],
    )
    reply, tool_results = await svc.chat("bot_001", None, [], {})
    assert reply == "明天晴, 25 度" and len(tool_results) == 1
    assert len(completions.calls) == 2
    await svc.close()


async def test_message_handler_nonstream_segment_mode_delivers_chained_answer():
    """真实入口: reply.stream=false + segment_reply=true 时用户收到最终答案, 且写入上下文。"""
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.context_manager import ContextManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import ReplyConfig
    from mohobot.models.onebot import PrivateMessageEvent, Sender

    class _WS:
        def __init__(self, manager):
            self._bot_manager = manager
            self.sent = []

        async def send_private_msg(self, bot_id, user_id, message, source="auto"):
            self.sent.append(message)

    with tempfile.TemporaryDirectory() as td:
        manager = BotManager(data_dir=td)
        cfg = manager.create_bot(nickname="测试bot", qq=1001)
        manager._bots[cfg.bot_id] = BotInstance(cfg.bot_id, None, cfg)
        context = ContextManager(data_dir=td, summary_enabled=False, trim_at_rounds=100)
        svc, completions, _ = _service(
            [_response(tool_calls=[_tool_call("c1")]),
             _response(tool_calls=[_tool_call("c2")]),
             _response(content="找到了《世末歌者》。")],
            ['{"songs": []}', '{"songs": ["世末歌者"]}'],
        )
        ws = _WS(manager)
        handler = MessageHandler(
            ws, context, svc, None, data_dir=td,
            reply_config=ReplyConfig(stream=False, segment_reply=True,
                                     segment_delay_min=0, segment_delay_max=0),
        )
        event = PrivateMessageEvent(
            time=1, self_id=1001, post_type="message", message_type="private",
            message_id=1, user_id=3001, sender=Sender(user_id=3001, nickname="测试用户"),
            message=[{"type": "text", "data": {"text": "世末歌者"}}],
        )
        await handler._handle_message(cfg.bot_id, event, {})

        def text_of(message):
            if isinstance(message, list):
                return "".join(s["data"].get("text", "") for s in message if s.get("type") == "text")
            return str(message)

        shown = "".join(text_of(m) for m in ws.sent)
        assert "找到了《世末歌者》" in shown, ws.sent
        assert len(completions.calls) == 3
        saved = await context.load_context(cfg.bot_id, "private", "3001")
        assert saved and saved[-1]["role"] == "assistant"
        assert saved[-1]["content"] == "找到了《世末歌者》。", saved
        await handler.close()
        await svc.close()
