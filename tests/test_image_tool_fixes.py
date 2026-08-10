"""两个重大问题的修复测试:
1. 工具调用(联网搜索)结果不输出给用户 —— 回传 LLM 生成最终回复
2. 群聊图片解析 —— NapCat 无 url 图片段经 get_image 归一化为 data URI,
   ImageCache 支持 data URI 下载
"""

import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.image_cache import ImageCache
from mohobot.message_handler import MessageHandler
from mohobot.models.config import GlobalConfig, ReplyConfig
from mohobot.models.onebot import GroupMessageEvent, Sender


# ── 1. 工具调用结果不回显 ───────────────────────────────────

def make_fake_tool_call(name="anysearch_search"):
    return type("TC", (), {
        "id": "call_1",
        "function": type("F", (), {"name": name, "arguments": '{"query": "天气"}'}),
    })()


class FakeChoice:
    def __init__(self, content="", tool_calls=None):
        self.message = type("M", (), {"content": content, "tool_calls": tool_calls})()


class FakeResponse:
    def __init__(self, choice):
        self.choices = [choice]
        self.usage = None


class FakeCompletions:
    """按调用次数返回: 第1次→工具调用, 第2次→最终回复。"""

    def __init__(self, final_text="根据搜索结果,明天晴天,25度。"):
        self.calls = 0
        self.final_text = final_text

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(FakeChoice(tool_calls=[make_fake_tool_call()]))
        return FakeResponse(FakeChoice(self.final_text))


class FakeClient:
    def __init__(self, completions=None):
        self.chat = type("Chat", (), {"completions": completions or FakeCompletions()})()


def make_llm_service(completions=None):
    from mohobot.llm_service import LLMService
    svc = LLMService(global_config=GlobalConfig())
    svc._chat_client = FakeClient(completions)
    svc._available = True
    svc._anysearch_client = None  # 工具执行返回"未配置"错误, 同样回传
    return svc


async def test_tool_result_not_sent_to_user():
    """非流式: 工具结果作为 tool 消息回传, 回复=最终生成文本, 不含搜索结果。"""
    svc = make_llm_service()
    from mohobot.models.onebot import PrivateMessageEvent

    ev = PrivateMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="private",
        message_id=1, user_id=2001, sender=Sender(user_id=2001),
        message=[{"type": "text", "data": {"text": "今天天气怎么样"}}],
    )
    reply, tool_results = await svc.chat(
        bot_id="bot_001", event=ev, context=[], raw_event={},
    )
    assert reply == "根据搜索结果,明天晴天,25度。", reply
    assert "工具" not in reply and "[工具调用" not in reply, "不应把工具结果拼进回复"
    assert tool_results and tool_results[0]["function_name"] == "anysearch_search"
    print("[+] 非流式工具结果不回显 OK")


class FakeStream:
    """流式 chunks: 第一次给 tool_calls, 第二次给文本。"""

    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration


def make_delta(content=None, tool_calls=None):
    def _tc(idx, name, args):
        return type("TC", (), {"index": idx, "id": f"call_{idx}",
                               "function": type("F", (), {"name": name, "arguments": args})})()
    return type("D", (), {
        "content": content,
        "tool_calls": tool_calls,
    })()


class FakeStreamCompletions:
    """第一次流式调用返回 tool_calls, 第二次返回最终文本。"""

    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FakeStream([
                type("C", (), {"choices": [type("CH", (), {"delta": make_delta(tool_calls=[_tc(0)])})()], "usage": None})(),
            ])
        return FakeStream([
            type("C", (), {"choices": [type("CH", (), {"delta": make_delta("最终")})()], "usage": None})(),
            type("C", (), {"choices": [type("CH", (), {"delta": make_delta("回复")})()], "usage": None})(),
        ])


def _tc(idx):
    return type("TC", (), {"index": idx, "id": f"call_{idx}",
                           "function": type("F", (), {"name": "anysearch_search", "arguments": '{"query":"x"}'})})()


async def test_stream_tool_result_not_sent():
    """流式: 工具后二次流式生成最终回复, 不 yield 搜索结果段。"""
    svc = make_llm_service(FakeStreamCompletions())
    from mohobot.models.onebot import PrivateMessageEvent

    ev = PrivateMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="private",
        message_id=1, user_id=2001, sender=Sender(user_id=2001),
        message=[{"type": "text", "data": {"text": "hi"}}],
    )
    chunks = []
    async for chunk, is_final in svc.chat_stream(
        bot_id="bot_001", event=ev, context=[], raw_event={},
    ):
        chunks.append((chunk, is_final))
    text = "".join(c for c, _ in chunks)
    assert text == "最终回复", text
    assert "[工具" not in text and "工具调用" not in text, "不应向用户输出工具结果"
    print("[+] 流式工具结果不回显 OK")


# ── 2. 群聊图片归一化 ───────────────────────────────────────

PNG_B64 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
).decode()


class ImageWS:
    def __init__(self):
        self.get_image_calls = []
        self.fail_get_image = False

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        if action == "get_image":
            self.get_image_calls.append(params["file"])
            if self.fail_get_image:
                raise RuntimeError("api error")
            return {"status": "ok", "retcode": 0, "data": {"base64": PNG_B64}}


def make_img_event(file_val, url_val=""):
    data = {"file": file_val}
    if url_val:
        data["url"] = url_val
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=2001, group_id=888888,
        sender=Sender(user_id=2001),
        message=[{"type": "image", "data": data}],
    )


async def test_normalize_image_segments():
    ws = ImageWS()
    handler = MessageHandler(
        ws_server=ws,
        context_manager=None,
        llm_service=None,
        plugin_system=None,
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(),
        global_config=GlobalConfig(),
    )
    # 群聊图片: 只有 file 文件名 → get_image → data URI
    ev = make_img_event("群图片123.jpg")
    await handler._normalize_image_segments("bot_001", ev)
    url = ev.message[0]["data"]["url"]
    assert url.startswith("data:image/png;base64,"), url
    assert ws.get_image_calls == ["群图片123.jpg"]

    # 已有 url 的不动
    ev2 = make_img_event("x.jpg", url_val="http://example.com/a.jpg")
    await handler._normalize_image_segments("bot_001", ev2)
    assert ev2.message[0]["data"]["url"] == "http://example.com/a.jpg"
    assert len(ws.get_image_calls) == 1, "有 url 不应调 get_image"

    # base64:// 不动
    ev3 = make_img_event(f"base64://{PNG_B64}")
    await handler._normalize_image_segments("bot_001", ev3)
    assert ev3.message[0]["data"].get("url") in (None, ""), "base64:// 不应调 get_image"
    assert len(ws.get_image_calls) == 1

    # get_image 失败 → 保持原样(降级 [图片])
    ws.fail_get_image = True
    ev4 = make_img_event("另一个.jpg")
    await handler._normalize_image_segments("bot_001", ev4)
    assert not ev4.message[0]["data"].get("url"), "失败应保持无 url"
    print("[+] 图片归一化 OK")


async def test_image_cache_data_uri():
    cache = ImageCache(cache_dir=tempfile.mkdtemp())
    path, desc = await cache.get_or_describe(
        f"data:image/png;base64,{PNG_B64}", vision_callback=None,
    )
    assert path and Path(path).exists(), "data URI 应解码落盘"
    assert desc == "[图片]", desc
    # 坏 data URI → 下载失败
    path2, desc2 = await cache.get_or_describe(
        "data:image/png;base64,!!!bad!!!", vision_callback=None,
    )
    assert path2 == "" and desc2 == "[图片下载失败]", (path2, desc2)
    print("[+] ImageCache data URI OK")


async def test_agent_path_uses_normalized_image():
    """端到端: 归一化后 agent 路径能取到图片引用(视觉描述走 data URI)。"""
    from mohobot.utils.cq_code import extract_image_urls
    ws = ImageWS()
    handler = MessageHandler(
        ws_server=ws,
        context_manager=None,
        llm_service=None,
        plugin_system=None,
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(),
        global_config=GlobalConfig(),
    )
    ev = make_img_event("群图.jpg")
    await handler._normalize_image_segments("bot_001", ev)
    urls = extract_image_urls(ev.message)
    assert urls and urls[0].startswith("data:image/png;base64,"), urls
    print("[+] agent 路径图片引用 OK")


async def _main() -> int:
    import asyncio as _a
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if _a.iscoroutinefunction(fn):
                    await fn()
                else:
                    fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    return failed


if __name__ == "__main__":
    import asyncio
    failed = asyncio.run(_main())
    total = len([n for n in globals() if n.startswith("test_") and callable(globals()[n])])
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
