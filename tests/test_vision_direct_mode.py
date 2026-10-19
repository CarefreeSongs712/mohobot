"""识图模式回归: describe(默认) 与 direct(多模态直传) 的请求构造与失败回退。

Fake 掉 OpenAI 客户端, 不联网。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import LLMConfig, _normalize_vision_mode  # noqa: E402


def test_normalize_vision_mode():
    assert _normalize_vision_mode("direct") == "direct"
    assert _normalize_vision_mode("DIRECT") == "direct"
    assert _normalize_vision_mode("describe") == "describe"
    assert _normalize_vision_mode("") == "describe"
    assert _normalize_vision_mode(None) == "describe"
    assert _normalize_vision_mode("bogus") == "describe"
    assert LLMConfig().vision_mode == "describe", "默认必须是 describe, 不改变既有行为"


def _image_event(url: str = "https://img.example/a.jpg"):
    from mohobot.models.onebot import PrivateMessageEvent

    raw = {
        "time": 1700000000, "self_id": 111, "post_type": "message",
        "message_type": "private", "sub_type": "friend", "message_id": 1,
        "user_id": 10001,
        "message": [
            {"type": "text", "data": {"text": "看看这个"}},
            {"type": "image", "data": {"url": url}},
        ],
        "raw_message": "看看这个", "font": 0,
        "sender": {"user_id": 10001, "nickname": "张三"},
    }
    event = PrivateMessageEvent.from_dict(raw)
    return event


def _stub_service(mode: str, captured: list, fail_first: bool = False):
    """最小 LLMService: 只借用 _prepare_messages/_build_messages 与 chat()。"""
    from mohobot.llm_service import LLMService

    svc = LLMService.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=LLMConfig(vision_mode=mode))
    svc._image_cache = None
    svc._song_annotator = None
    svc._vision_available = True
    svc._vision_client = object()
    svc._available = True
    svc._retired_clients = []

    async def _describe(url):
        captured.append(("describe", url))
        return "一张风景照"
    svc._describe_image_for_text = _describe

    def _tools():
        return None
    svc._current_tools_schemas = _tools

    calls = {"n": 0}

    class _Completions:
        async def create(self, **kwargs):
            calls["n"] += 1
            captured.append(("request", kwargs.get("messages")))
            if fail_first and calls["n"] == 1:
                raise RuntimeError("400 model does not support image input")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="好的", tool_calls=None))],
                usage=None,
            )

    svc._chat_client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    async def _record(*args, **kwargs):
        return None
    svc._record_usage = _record

    def _persona(cfg=None):
        return {"content": "PERSONA"}
    svc.resolve_bot_persona = _persona
    return svc


async def test_describe_mode_does_not_send_image():
    captured: list = []
    svc = _stub_service("describe", captured)
    await svc.chat("bot_001", _image_event(), [], {})
    kinds = [k for k, *_ in captured]
    assert kinds[0] == "describe", kinds
    messages = next(v for k, v in captured if k == "request")
    user = [m for m in messages if m["role"] == "user"][-1]
    assert isinstance(user["content"], str), "describe 模式不能发多模态结构"
    assert "一张风景照" in user["content"]


async def test_direct_mode_sends_image_and_skips_vision_call():
    captured: list = []
    svc = _stub_service("direct", captured)
    await svc.chat("bot_001", _image_event(), [], {})
    assert not any(k == "describe" for k, *_ in captured), "direct 模式不应再调用视觉模型"
    messages = next(v for k, v in captured if k == "request")
    user = [m for m in messages if m["role"] == "user"][-1]
    assert isinstance(user["content"], list), user["content"]
    assert any(p.get("type") == "image_url" for p in user["content"])
    assert any(p.get("type") == "text" and "看看这个" in p.get("text", "") for p in user["content"])


async def test_direct_mode_falls_back_to_describe_on_failure():
    captured: list = []
    svc = _stub_service("direct", captured, fail_first=True)
    reply, _ = await svc.chat("bot_001", _image_event(), [], {})
    assert reply == "好的", reply
    assert any(k == "describe" for k, *_ in captured), "失败后必须回退 describe"
    requests = [v for k, v in captured if k == "request"]
    assert len(requests) == 2, requests
    assert isinstance(requests[0][-1]["content"], list), "首次应为多模态"
    assert isinstance(requests[1][-1]["content"], str), "回退后应为纯文本"


async def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
        print(f"PASS {fn.__name__}")
    print("\nALL VISION DIRECT MODE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
