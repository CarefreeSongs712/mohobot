"""引用消息内容解析 + 图片消息上下文渲染回归测试。

覆盖:
1. 图片段带 NapCat summary(如 "[动画表情]") → 渲染直接用 summary,
   绝不出现段列表 repr(含 url/file/sub_type)
2. 照片(无 summary): 缓存命中带 "[图片]（概要：…）"; 未命中仅 "[图片]"
   (describe_missing=False 只读缓存, 不现调视觉; True 才下载+识别)
3. 引用(reply)消息解析: get_msg 取内容+发送者+图片概要, 结果带 TTL 缓存
4. 解析失败(查不到/无内容)静默返回 "" 并短缓存, 不重复打接口
5. 引用内容并入该条 user 消息(_build_user_store_content), 供后续轮次追溯
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.message_handler import MessageHandler

# 用户 bug 报告里的真实 NapCat 图片段形状(动画表情, 带 summary)
IMG_STICKER = {
    "type": "image",
    "data": {
        "url": "https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=EhQeyf0-elvu",
        "file": "5DCA4A5F80E5CF9AF9A62031AF47E95A.jpg",
        "sub_type": 1,
        "summary": "[动画表情]",
    },
}

IMG_PHOTO = {
    "type": "image",
    "data": {
        "url": "https://cdn.example.com/photo.jpg",
        "file": "photo.jpg",
    },
}


def _handler(**attrs) -> MessageHandler:
    """按测试惯例用 __new__ 构造, 只绑最小属性集。"""
    h = MessageHandler.__new__(MessageHandler)
    h._ws = None
    h._image_cache = None
    h._llm = None
    h._quote_cache = {}  # 实例级缓存, 避免测试间相互污染
    for k, v in attrs.items():
        setattr(h, k, v)
    return h


class _PeekCache:
    """只实现只读探测(peek)的图片缓存替身。"""

    def __init__(self, desc):
        self._desc = desc

    async def peek_description(self, url):
        return self._desc


class _DescribeCache:
    """带下载+视觉(get_or_describe)的图片缓存替身。"""

    def __init__(self, desc):
        self._desc = desc

    async def get_or_describe(self, url, vision_callback=None):
        return "/tmp/cached.jpg", self._desc

    async def peek_description(self, url):
        return self._desc


async def test_sticker_uses_summary_no_repr():
    h = _handler()
    out = await h._render_message_content("bot_001", [IMG_STICKER])
    assert out == "[动画表情]", out
    # 绝不出现段列表 repr / url / file
    for bad in ("'type'", "'data'", "http", "sub_type", "fileid"):
        assert bad not in out, out


async def test_text_plus_sticker():
    h = _handler()
    msg = [{"type": "text", "data": {"text": "哈哈哈"}}, IMG_STICKER]
    out = await h._render_message_content("bot_001", msg)
    assert out == "哈哈哈 [动画表情]", out


async def test_photo_peek_hit_includes_summary():
    h = _handler(_image_cache=_PeekCache("一只橘猫"))
    out = await h._render_message_content("bot_001", [IMG_PHOTO])
    assert out == "[图片]（概要：一只橘猫）", out


async def test_photo_peek_miss_is_plain_placeholder():
    # describe_missing=False(上下文存储): 缓存未命中不现调视觉, 只留 [图片]
    h = _handler(_image_cache=_PeekCache(None))
    out = await h._render_message_content("bot_001", [IMG_PHOTO])
    assert out == "[图片]", out


async def test_photo_describe_when_missing_for_quote():
    # describe_missing=True(引用消息): 缓存未命中会现调视觉
    h = _handler(_image_cache=_DescribeCache("一只蹲在窗台的猫"))
    out = await h._render_message_content("bot_001", [IMG_PHOTO], describe_missing=True)
    assert out == "[图片]（概要：一只蹲在窗台的猫）", out


async def test_bad_desc_keeps_placeholder():
    h = _handler(_image_cache=_DescribeCache("[图片下载失败]"))
    out = await h._render_message_content("bot_001", [IMG_PHOTO], describe_missing=True)
    assert out == "[图片]", out


def test_extract_reply_id():
    h = _handler()
    ev = SimpleNamespace(message=[
        {"type": "reply", "data": {"id": "12345"}},
        {"type": "text", "data": {"text": "hi"}},
    ])
    assert h._extract_reply_id(ev) == "12345"
    assert h._extract_reply_id(SimpleNamespace(message=[])) == ""
    assert h._extract_reply_id(SimpleNamespace(
        message=[{"type": "text", "data": {"text": "x"}}])) == ""
    assert h._extract_reply_id(SimpleNamespace(
        message=[{"type": "reply", "data": {"id": "abc"}}])) == ""  # 非法 id


class _FakeWS:
    """get_msg / get_nickname 假实现, 记录 get_msg 调用次数。"""

    def __init__(self, payload, nickname="李四"):
        self._payload = payload
        self._nickname = nickname
        self.calls = 0

    async def send_to_bot(self, bot_id, action, params, **kw):
        assert action == "get_msg"
        self.calls += 1
        return self._payload

    async def get_nickname(self, bot_id, uid, group_id=None):
        return self._nickname


def _reply_event(reply_id="42"):
    return SimpleNamespace(
        message=[{"type": "reply", "data": {"id": reply_id}}],
        group_id=888,
    )


def _ok_payload(message, uid=123, nickname="李四"):
    return {
        "status": "ok",
        "retcode": 0,
        "data": {"message": message, "sender": {"user_id": uid, "nickname": nickname}},
    }


async def test_resolve_quoted_text():
    ws = _FakeWS(_ok_payload([{"type": "text", "data": {"text": "大家好呀"}}]))
    h = _handler(_ws=ws)
    ev = _reply_event()
    out = await h._resolve_quoted_display("bot_001", ev)
    assert out == "李四(123): 大家好呀", out
    # TTL 缓存: 同一消息重复引用不重复调 get_msg
    out2 = await h._resolve_quoted_display("bot_001", ev)
    assert out2 == out
    assert ws.calls == 1


async def test_resolve_quoted_image_summary():
    ws = _FakeWS(_ok_payload([IMG_STICKER]))
    h = _handler(_ws=ws)
    out = await h._resolve_quoted_display("bot_001", _reply_event())
    assert out == "李四(123): [动画表情]", out


async def test_resolve_quoted_failure_silent_and_negative_cache():
    ws = _FakeWS({"status": "ok", "retcode": 0, "data": None})
    h = _handler(_ws=ws)
    ev = _reply_event("999")
    out = await h._resolve_quoted_display("bot_001", ev)
    assert out == "", out
    # 负缓存: 不重复打接口
    await h._resolve_quoted_display("bot_001", ev)
    assert ws.calls == 1


async def test_quote_failure_when_no_content():
    ws = _FakeWS(_ok_payload([]))
    h = _handler(_ws=ws)
    out = await h._resolve_quoted_display("bot_001", _reply_event("7"))
    assert out == "", out


async def test_build_user_store_with_quote():
    h = _handler()
    ev = SimpleNamespace(message=[{"type": "text", "data": {"text": "我也这么觉得"}}])
    out = await h._build_user_store_content(
        "bot_001", ev, "李四(123): 大家好呀",
    )
    assert out == "【引用消息】\n李四(123): 大家好呀\n\n我也这么觉得", out


async def test_build_user_store_quote_only_message():
    # 引用但无自己的文字: 只存引用块(不把空内容/段列表存进去)
    h = _handler()
    ev = SimpleNamespace(message=[{"type": "reply", "data": {"id": "42"}}])
    out = await h._build_user_store_content(
        "bot_001", ev, "李四(123): 大家好呀",
    )
    assert out == "【引用消息】\n李四(123): 大家好呀", out


async def test_build_user_store_image_sticker():
    h = _handler()
    ev = SimpleNamespace(message=[IMG_STICKER])
    out = await h._build_user_store_content("bot_001", ev, "")
    assert out == "[动画表情]", out


async def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        import asyncio
        if asyncio.iscoroutinefunction(t):
            await t()
        else:
            t()
        print(f"PASS {t.__name__}")
    print("\nALL REPLY/QUOTE CONTENT TESTS PASSED")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
