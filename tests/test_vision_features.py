"""识图功能三个新特性的测试:
1. llm.vision_prompt 可配置识图提示词(默认人物特征) + describe_image 使用
2. ImageCache 并发去重(单飞): 同图并发只识别一次
3. 群聊临时上下文: 图片消息只对最新一张调 VLM 描述, 旧图占位
"""

import asyncio
import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.image_cache import ImageCache
from mohobot.message_handler import MessageHandler
from mohobot.models.config import GlobalConfig, ReplyConfig
from mohobot.models.onebot import GroupMessageEvent, Sender

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode()
DATA_URI = f"data:image/png;base64,{PNG_B64}"


# ── 1. vision_prompt 配置 ───────────────────────────────────

def test_vision_prompt_config():
    cfg = GlobalConfig()
    assert "请将下方的图片转述为中文" in cfg.llm.vision_prompt
    assert "洛天依" in cfg.llm.vision_prompt and "星尘" in cfg.llm.vision_prompt
    assert "不要超过 100 字" in cfg.llm.vision_prompt
    # 往返
    import tempfile as _t, os
    with _t.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        tmp = f.name
    cfg.save(tmp)
    cfg2 = GlobalConfig.load(tmp)
    assert cfg2.llm.vision_prompt == cfg.llm.vision_prompt
    os.unlink(tmp)
    # 旧配置(无字段) → 默认
    cfg3 = GlobalConfig.load("config/global.yaml")
    assert "洛天依" in cfg3.llm.vision_prompt
    # WebUI 字段
    d = cfg.to_dict()
    assert "vision_prompt" in d["llm"]
    print("[+] vision_prompt 配置 OK")


async def test_describe_image_uses_configured_prompt():
    from mohobot.llm_service import LLMService

    class FakeVisionClient:
        def __init__(self):
            self.prompt = ""
            self.max_tokens = 0

        @property
        def chat(self):
            return self

        class completions:
            @staticmethod
            async def create(**kwargs):
                messages = kwargs["messages"]
                content = messages[0]["content"]
                FakeVisionClient.last = {
                    "prompt": content[0]["text"],
                    "max_tokens": kwargs["max_tokens"],
                }
                return type("R", (), {"choices": [type("C", (), {
                    "message": type("M", (), {"content": "这是一只猫"})()})()]})()
        last = {}

    svc = LLMService(global_config=GlobalConfig())
    svc._vision_client = FakeVisionClient()
    svc._vision_available = True
    out = await svc.describe_image(DATA_URI)
    assert out == "这是一只猫"
    last = FakeVisionClient.last
    assert "洛天依" in last["prompt"], "应使用配置的人物特征提示词"
    assert last["max_tokens"] == 512
    # 空配置提示词 → 回退旧默认
    svc._cfg.llm.vision_prompt = ""
    out2 = await svc.describe_image(DATA_URI)
    assert out2 == "这是一只猫"
    assert "简短、客观" in FakeVisionClient.last["prompt"]
    print("[+] describe_image 提示词 OK")


# ── 2. ImageCache 并发去重(单飞) ───────────────────────────

async def test_image_cache_singleflight():
    cache = ImageCache(cache_dir=tempfile.mkdtemp())
    calls = {"n": 0}

    async def slow_cb(image_url, local_path):
        calls["n"] += 1
        await asyncio.sleep(0.05)  # 模拟 VLM 耗时
        return "并发只识别一次"

    async def worker():
        return await cache.get_or_describe(DATA_URI, vision_callback=slow_cb)

    # 并发 5 个同图请求 → 只识别 1 次
    results = await asyncio.gather(*[worker() for _ in range(5)])
    assert calls["n"] == 1, f"VLM 应只调用 1 次, 实际 {calls['n']}"
    for path, desc in results:
        assert desc == "并发只识别一次"
    # 之后直接命中缓存, 不再调用
    await cache.get_or_describe(DATA_URI, vision_callback=slow_cb)
    assert calls["n"] == 1
    print("[+] ImageCache 单飞 OK")


# ── 3. 群聊临时上下文图片 ──────────────────────────────────

class FakeImageCache:
    """模拟 ImageCache: 记录调用, 返回固定描述。"""

    def __init__(self):
        self.calls = []

    async def get_or_describe(self, image_url, vision_callback=None):
        self.calls.append(image_url)
        return "cache_path", "一只猫在窗边"


class FakeLLM:
    async def describe_image_file(self, local_path, max_tokens=512):
        return "一只猫在窗边"


def make_img_event(user_id, file_val, text=""):
    data = {"file": file_val}
    segs = []
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    segs.append({"type": "image", "data": data})
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=888888,
        sender=Sender(user_id=user_id, card="张三"),
        message=segs,
    )


def make_handler(image_cache, ws=None):
    return MessageHandler(
        ws_server=ws,
        context_manager=None,
        llm_service=FakeLLM(),
        plugin_system=None,
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(),
        global_config=GlobalConfig(),
        image_cache=image_cache,
    )


async def test_group_recent_image_only_latest():
    ic = FakeImageCache()

    class WS:
        async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
            assert action == "get_image"
            return {"status": "ok", "retcode": 0, "data": {"base64": PNG_B64}}

    handler = make_handler(ic, ws=WS())
    # 三条图片消息 + 一条文本
    await handler._note_group_recent("bot_001", make_img_event(2001, "图1.jpg"))
    await handler._note_group_recent("bot_001", make_img_event(2002, "图2.jpg", text="看我发的"))
    await handler._note_group_recent("bot_001", make_img_event(2003, "图3.jpg"))
    await handler._note_group_recent("bot_001", GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=2004, group_id=888888,
        sender=Sender(user_id=2004, card="张三"),
        message=[{"type": "text", "data": {"text": "普通消息"}}],
    ))

    text = await handler._format_group_recent("bot_001", 888888)
    # 只识别最新一张(图3), 图1/图2 占位
    assert "一只猫在窗边" in text, text
    assert text.count("[图片]（一只猫在窗边）") == 1
    assert text.count("[图片]") == 3, "三张图都应出现占位/描述"
    # 图2 的文字保留
    assert "看我发的 [图片]" in text, text
    # 只调用了一次识别(最新一张)
    assert len(ic.calls) == 1 and ic.calls[0].startswith("data:"), ic.calls
    print("[+] 群聊临时上下文最新一张 OK")


async def test_group_recent_image_file_fallback():
    """file 无 url → 识别时经 get_image 换 data URI。"""
    ic = FakeImageCache()

    class WS:
        async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
            assert action == "get_image"
            return {"status": "ok", "retcode": 0, "data": {"base64": PNG_B64}}

    handler = make_handler(ic, ws=WS())
    await handler._note_group_recent("bot_001", make_img_event(2001, "群图.jpg"))
    text = await handler._format_group_recent("bot_001", 888888)
    assert "一只猫在窗边" in text
    assert ic.calls and ic.calls[0].startswith("data:image/png;base64,"), ic.calls
    print("[+] file 兜底 get_image OK")


async def _main() -> int:
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if asyncio.iscoroutinefunction(fn):
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
    failed = asyncio.run(_main())
    total = len([n for n in globals() if n.startswith("test_") and callable(globals()[n])])
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
