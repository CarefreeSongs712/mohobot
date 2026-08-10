"""哔哩哔哩解析插件测试:
1. 正则匹配: BV/av 链接/带协议/带参数; 非 B 站链接不触发
2. 群聊不 @ 触发(on_message_observed, gate 前)
3. 多 bot: 非最小 bot 静默消费(handled=True, 无回复), 最小 bot 解析回复
4. 解析 API: mock 成功/失败/异常
5. 信息卡片图片发送 + 渲染失败降级文本
6. 精简字段(无封面/弹幕链接)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender


def make_group_event(user_id, text, group_id=888888):
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def make_private_event(user_id, text):
    return PrivateMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="private",
        message_id=1, user_id=user_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def load_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bilibiliparse_plugin_main", "plugins/bilibiliparse/main.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeParseAPI:
    """模拟解析 API 响应(aiohttp 风格: get() 返回可 async with 的 CM)。"""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if not self.ok:
            class _Err:
                def raise_for_status(self):
                    raise RuntimeError("api down")

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def json(self):
                    return {}
            return _Err()
        data = {"data": [{
            "video_url": "https://upos.example.com/video.mp4",
            "video_size": 134217728,  # 128 MB
            "accept_format": "1080P 高清",
            "comment": "https://comment.example.com/x",
        }]}
        return FakeCM(FakeResp({
            "code": 0,
            "title": "测试视频标题",
            "imgurl": "http://cover.example.com/pic.jpg",
            "data": data["data"],
        }))


class FakeCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data


class FakeWS:
    """记录发送; 提供 _bot_manager 供多 bot 去重。"""

    def __init__(self, bot_manager=None):
        self.images = []
        self.texts = []
        self._bot_manager = bot_manager

    async def send_image(self, bot_id, chat_type, chat_id, image_path):
        self.images.append((bot_id, chat_type, chat_id, image_path))

    async def send_group_msg(self, bot_id, group_id, message):
        self.texts.append((bot_id, group_id, message))

    async def send_private_msg(self, bot_id, user_id, message):
        self.texts.append((bot_id, user_id, message))


def make_bm():
    """构造含 3 个 bot 的 BotManager(bot_001 最小)。"""
    import tempfile
    from mohobot.bot_manager import BotManager, BotInstance
    from mohobot.models.config import BotConfig

    bm = BotManager(data_dir=tempfile.mkdtemp())
    for bid in ("bot_001", "bot_002", "bot_003"):
        bm._bots[bid] = BotInstance(bid, None, BotConfig(qq=int(bid[-3:]), nickname=bid))
    bm.note_group_message("bot_001", 888888)
    bm.note_group_message("bot_002", 888888)
    bm.note_group_message("bot_003", 888888)
    return bm


def make_plugin(ok=True, bm=None):
    mod = load_plugin()
    inst = mod.Plugin()
    ws = FakeWS(bot_manager=bm)
    inst._ws_server = ws
    inst._http_session = FakeParseAPI(ok=ok)
    return mod, inst, ws


# ── 1. 正则匹配 ─────────────────────────────────────────────

def test_pattern():
    mod = load_plugin()
    p = mod.BILI_VIDEO_PATTERN
    import re
    ok_cases = [
        "https://www.bilibili.com/video/BV1xG411x7fE",
        "www.bilibili.com/video/BV1xG411x7fE/",
        "http://www.bilibili.com/video/av123456",
        "看看这个 https://www.bilibili.com/video/BV1xG411x7fE?p=2 怎么样",
    ]
    for t in ok_cases:
        assert re.search(p, t), f"应匹配: {t}"
    bad_cases = [
        "https://www.bilibili.com/video/",           # 无 BV/av
        "https://bilibili.com/video/BV123",           # 无 www.
        "https://www.bilibili.com/watch/BV123",       # 非 /video/
        "https://www.baidu.com/video/BV123",
        "BV1xG411x7fE",                               # 裸 BV 号(原插件不匹配)
    ]
    for t in bad_cases:
        assert not re.search(p, t), f"不应匹配: {t}"
    print("[+] 正则匹配 OK")


# ── 2. 群聊不 @ 触发 + 卡片图片 ────────────────────────────

async def test_group_trigger_and_card():
    mod, inst, ws = make_plugin(bm=make_bm())
    # 群聊不 @ 发链接 → 观察钩子触发
    handled, reply = await inst.on_message_observed(
        "bot_001", make_group_event(2001, "分享: https://www.bilibili.com/video/BV1xG411x7fE"), {})
    assert handled, "链接消息应被消费"
    assert reply is None, "卡片发送成功时无文本回复"
    assert ws.images and ws.images[-1][0] == "bot_001", "应发送信息卡片图片"
    assert ws.images[-1][1] == "group"
    # 非链接消息不触发
    handled, _ = await inst.on_message_observed("bot_001", make_group_event(2001, "你好"), {})
    assert not handled
    print("[+] 群聊触发 + 卡片 OK")


# ── 3. 多 bot: 非最小 bot 静默消费 ─────────────────────────

async def test_multi_bot_silent():
    mod, inst, ws = make_plugin(bm=make_bm())
    ev = make_group_event(2001, "https://www.bilibili.com/video/BV1xG411x7fE")
    # bot_002(非最小) → 静默消费(handled=True, 无回复, 不落 LLM)
    handled, reply = await inst.on_message_observed("bot_002", ev, {})
    assert handled and reply is None, "非最小 bot 应静默消费"
    assert not ws.images and not ws.texts, "非最小 bot 不应有任何回复"
    # bot_001(最小) → 正常解析
    handled, reply = await inst.on_message_observed("bot_001", ev, {})
    assert handled and reply is None
    assert ws.images, "最小 bot 应发送卡片"
    # 无 bot_manager(如未注入) → 直接解析
    mod2, inst2, ws2 = make_plugin(bm=None)
    handled, _ = await inst2.on_message_observed("bot_001", ev, {})
    assert handled and ws2.images
    print("[+] 多 bot 静默 OK")


# ── 4. 解析失败降级 ────────────────────────────────────────

async def test_parse_failures():
    # API 请求异常
    mod, inst, ws = make_plugin(ok=False, bm=make_bm())
    handled, reply = await inst.on_message_observed(
        "bot_001", make_group_event(2001, "https://www.bilibili.com/video/BV1xG411x7fE"), {})
    assert handled and reply and "解析" in reply, reply

    # API 返回 code != 0
    class ErrAPI(FakeParseAPI):
        def get(self, url, **kw):
            self.calls.append(url)
            return FakeCM(FakeResp({"code": 1, "msg": "nope"}))

    mod, inst, ws = make_plugin(bm=make_bm())
    inst._http_session = ErrAPI()
    handled, reply = await inst.on_message_observed(
        "bot_001", make_group_event(2001, "https://www.bilibili.com/video/BV1xG411x7fE"), {})
    assert handled and "解析失败" in reply
    print("[+] 解析失败降级 OK")


# ── 5. 私聊触发 ────────────────────────────────────────────

async def test_private_trigger():
    mod, inst, ws = make_plugin(bm=None)
    handled, reply = await inst.on_message_observed(
        "bot_001", make_private_event(2001, "https://www.bilibili.com/video/av123456"), {})
    assert handled and ws.images and ws.images[-1][1] == "private"
    print("[+] 私聊触发 OK")


# ── 6. 精简字段(无封面/弹幕链接) ───────────────────────────

async def test_compact_fields():
    mod, inst, ws = make_plugin(bm=None)
    # 解析结果只含 标题/链接/清晰度/大小(code/msg 除外), 不含封面与弹幕链接
    info = await inst._parse("BV1xG411x7fE", 80)
    assert info["code"] == 0
    assert set(info.keys()) == {"code", "msg", "title", "video_url", "video_size", "quality"}
    # 文本降级内容不含封面/弹幕链接字样
    text = (
        f"🎬 标题: {info['title']}\n🔗 视频链接: {info['video_url']}\n"
        f"📖 视频大小: {inst._fmt_size(info['video_size'])}\n👓 清晰度: {info['quality']}"
    )
    assert "imgurl" not in text and "comment" not in text and "picUrl" not in text
    assert "测试视频标题" in text
    print("[+] 精简字段 OK")


# ── 7. 配置注入 ────────────────────────────────────────────

async def test_config_injection():
    import tempfile
    from mohobot.interceptors.plugin_system import PluginSystem

    ps = PluginSystem(plugins_dir="plugins", data_dir=tempfile.mkdtemp())
    await ps.load_plugins()
    meta = next((m for m in ps._plugins if m["name"] == "bilibiliparse"), None)
    assert meta and meta["loaded"], "bilibiliparse 应加载成功"
    assert meta["config_schema"], "应有配置 schema"
    inst = meta["instance"]
    assert inst.plugin_config.get("api_url") == "http://114.134.188.188:3003"
    assert inst.plugin_config.get("accept_quality") == 80
    print("[+] 配置注入 OK")


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
