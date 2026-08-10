"""新功能测试: send_image / get_nickname / status 图片渲染 / divination 插件 / beta_mode。"""

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def test_send_image() -> None:
    from mohobot.ws_server import WSServer
    from mohobot.bot_manager import BotManager, BotInstance
    from mohobot.models.config import BotConfig

    tmp = Path(tempfile.mkdtemp(prefix="img_"))
    png = tmp / "t.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfakedata")

    sent = []

    class FakeInst(BotInstance):
        async def send(self, data):
            sent.append(data)

    bm = BotManager(data_dir=str(tmp))
    inst = FakeInst("bot_001", None, BotConfig(bot_id="bot_001", qq=123), bound=True)
    bm._bots["bot_001"] = inst

    ws = WSServer(bot_manager=bm)
    await ws.send_image("bot_001", "group", 555, str(png))
    assert len(sent) == 1
    payload = sent[0]
    assert payload["action"] == "send_group_msg"
    msg = payload["params"]["message"]
    assert msg[0]["type"] == "image"
    assert msg[0]["data"]["file"].startswith("base64://")
    assert base64.b64decode(msg[0]["data"]["file"][len("base64://"):]) == png.read_bytes()
    print("[1] send_image base64 段 OK")


async def test_get_nickname() -> None:
    from mohobot.ws_server import WSServer
    from mohobot.bot_manager import BotManager

    bm = BotManager(data_dir=tempfile.mkdtemp())
    ws = WSServer(bot_manager=bm)

    async def fake_send_to_bot(bot_id, action, params, wait_response=False, timeout=10.0):
        if action == "get_group_member_info":
            return {"status": "ok", "data": {"card": "群名片名", "nickname": "QQ名"}}
        if action == "get_stranger_info":
            return {"status": "ok", "data": {"nickname": "陌生人名"}}
        return None

    ws.send_to_bot = fake_send_to_bot
    nick = await ws.get_nickname("bot_001", 10001, 555)
    assert nick == "群名片名", nick
    # 缓存命中(不再调 API)
    calls = []
    async def counting_send(*a, **k):
        calls.append(a)
        return None
    ws.send_to_bot = counting_send
    nick2 = await ws.get_nickname("bot_001", 10001, 555)
    assert nick2 == "群名片名" and len(calls) == 0, "缓存应命中"
    # 私聊(无 group): 陌生人接口
    ws.send_to_bot = fake_send_to_bot
    nick3 = await ws.get_nickname("bot_001", 20002)
    assert nick3 == "陌生人名", nick3
    # 全失败 → 数字兜底
    async def fail_send(*a, **k):
        return None
    ws.send_to_bot = fail_send
    nick4 = await ws.get_nickname("bot_001", 30003)
    assert nick4 == "30003"
    print("[2] get_nickname 优先级/缓存/兜底 OK")


async def test_status_image_render() -> None:
    from plugins.status import _render_status_image, _clean_status_text

    text = "📦 框架状态:\n  Bot ID: bot_001\n⚙️ 配置信息:\n  LLM: DeepSeek"
    lines = _clean_status_text(text)
    assert not any("📦" in l or "⚙️" in l for l in lines)
    path = _render_status_image(text)
    assert path is not None and Path(path).exists()
    from PIL import Image
    img = Image.open(path)
    assert img.size[0] == 600
    img.close()
    Path(path).unlink()
    print("[3] status 深色卡片渲染 OK")


async def test_divination_plugin() -> None:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins"))
    from divination import Plugin

    tmp = Path(tempfile.mkdtemp(prefix="div_"))
    plugin = Plugin()
    Plugin.inject_data_dir(str(tmp))

    class FakeEvent:
        user_id = 10001
        group_id = 555
        message = [{"type": "text", "data": {"text": "/占卜"}}]

    class FakeWS:
        async def get_nickname(self, bot_id, user_id, group_id):
            return "小明"

    Plugin.inject_ws_server(FakeWS())

    # 首次占卜
    handled, result = await plugin.on_message("bot_001", FakeEvent(), {})
    assert handled and result.startswith("@小明") and "财运:" in result
    # 记录已持久化
    records = json.loads((tmp / "divination.json").read_text(encoding="utf-8"))
    assert "10001" in records and records["10001"]["date"]
    # 同日再占卜 → 返回缓存结果
    handled2, result2 = await plugin.on_message("bot_001", FakeEvent(), {})
    assert handled2 and "今天已经占卜过了" in result2
    assert result2.endswith(result)
    # 非触发词不处理
    ev = FakeEvent()
    ev.message = [{"type": "text", "data": {"text": "随便聊聊"}}]
    handled3, _ = await plugin.on_message("bot_001", ev, {})
    assert handled3 is False
    # "今日占卜" 也触发
    ev2 = FakeEvent()
    ev2.message = [{"type": "text", "data": {"text": "今日占卜"}}]
    handled4, _ = await plugin.on_message("bot_001", ev2, {})
    assert handled4 is True
    print("[4] divination 触发/每日一次/持久化 OK")


async def test_beta_mode_config() -> None:
    from mohobot.models.config import GlobalConfig
    import tempfile as _tf, os, yaml

    tmp = _tf.mkdtemp()
    path = os.path.join(tmp, "g.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump({"beta_mode": False}, f, allow_unicode=True)
    cfg = GlobalConfig.load(path)
    assert cfg.beta_mode is False
    # 缺失时默认 true
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump({"server": {"port": 1}}, f, allow_unicode=True)
    assert GlobalConfig.load(path).beta_mode is True
    print("[5] beta_mode 配置解析 OK")


async def main() -> None:
    await test_send_image()
    await test_get_nickname()
    await test_status_image_render()
    await test_divination_plugin()
    await test_beta_mode_config()
    print("\nALL NEW FEATURE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())


async def test_praise_daily_limit_cache() -> None:
    """点赞: 当日上限缓存后不再调用 API。"""
    # 独立模块名加载, 避免覆盖 sys.modules["main"]
    # (test_wifepicker 也用 from main import Plugin, 全量跑时互相冲突)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "praise_plugin_main", "plugins/praise/main.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    Plugin = mod.Plugin

    class WS:
        def __init__(self):
            self.calls = 0

        async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=5.0):
            self.calls += 1
            if self.calls == 1:
                # 第一次成功
                return {"status": "ok", "retcode": 0, "data": {}}
            # 第二次失败(已达上限)
            return {"status": "failed", "retcode": 1, "data": None, "wording": "操作频繁,请稍后再试"}

    ws = WS()
    Plugin._ws_server = ws
    Plugin._like_limit_cache.clear()

    def ev(user_id, text):
        from mohobot.models.onebot import GroupMessageEvent, Sender
        return GroupMessageEvent(
            time=0, self_id=1000, post_type="message", message_type="group",
            message_id=1, user_id=user_id, group_id=1,
            sender=Sender(user_id=user_id),
            message=[{"type": "text", "data": {"text": text}}],
        )

    inst = Plugin()

    # 第一次: 第一次调用成功, 第二次失败(上限) → 缓存
    handled, reply = await inst.on_message("bot_001", ev(2001, "/赞我"), {})
    assert handled and "点赞失败" in reply
    assert Plugin._like_limit_cache.get("bot_001:2001"), "失败(上限)应缓存"

    # 第二次: 命中缓存, 不再调用 API
    ws.calls = 0
    handled2, reply2 = await inst.on_message("bot_001", ev(2001, "/赞我"), {})
    assert handled2 and "上限" in reply2
    assert ws.calls == 0, "缓存命中后不应再调用 API"

    # 其它用户不受影响
    handled3, _ = await inst.on_message("bot_001", ev(2002, "/赞我"), {})
    assert ws.calls > 0, "其它用户应正常调用"

    # 跨天缓存失效(模拟日期变化)
    Plugin._like_limit_cache["bot_001:2001"] = "2000-01-01"
    ws.calls = 0
    await inst.on_message("bot_001", ev(2001, "/赞我"), {})
    assert ws.calls > 0, "跨天后应重新调用"
    print("[+] 点赞上限缓存 OK")
