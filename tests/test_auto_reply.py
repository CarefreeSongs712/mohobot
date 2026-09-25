"""私聊自动回复过滤测试:
1. 文本以「[自动回复]」开头 → 静默丢弃(不进拦截器链; history 归档保留)
2. 同一用户连续 3 条相同文本且间隔 < 5min → 第 3 条起丢弃; 换文本/超窗重置
3. 开关关闭时两条规则都不生效
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import GlobalConfig
from mohobot.models.onebot import PrivateMessageEvent, Sender


def private_event(user_id: int = 1001, text: str = "你好") -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=0, self_id=1, post_type="message",
        message_type="private", sub_type="friend",
        message_id=42, user_id=user_id,
        message=[{"type": "text", "data": {"text": text}}],
        raw_message=text, font=0,
        sender=Sender(user_id=user_id, nickname="测试"),
    )


class Recorder:
    """记录拦截器链是否被触达。"""

    def __init__(self):
        self.calls = 0

    async def intercept(self, bot_id, event, raw_event):
        self.calls += 1
        return (False, None)


def make_handler(tmp: str, ignore_auto_reply: bool = True):
    from mohobot.context_manager import ContextManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import ReplyConfig

    cfg = GlobalConfig()
    cfg.ignore_auto_reply = ignore_auto_reply

    class FakeWS:
        pass

    handler = MessageHandler(
        ws_server=FakeWS(),
        context_manager=ContextManager(data_dir=tmp),
        llm_service=None,
        plugin_system=None,
        data_dir=tmp,
        reply_config=ReplyConfig(),
        global_config=cfg,
    )
    rec = Recorder()
    handler.set_interceptors([rec])
    return handler, rec


async def test_prefix_drop_and_archive() -> None:
    """[自动回复] 前缀: 拦截器链不触达, history 归档保留。"""
    tmp = tempfile.mkdtemp(prefix="auto_reply_prefix_")
    handler, rec = make_handler(tmp)
    raw = {
        "post_type": "message", "message_type": "private",
        "user_id": 1001, "raw_message": "[自动回复]我在线的，马上回消息",
    }
    ev = private_event(text="[自动回复]我在线的，马上回消息")
    await handler.handle_event("bot_001", ev, raw)

    assert rec.calls == 0, f"应静默丢弃, 拦截器被调了 {rec.calls} 次"
    hist = Path(tmp) / "history/bot_001/private/1001.jsonl"
    assert hist.exists(), "history 归档应保留"
    assert "[自动回复]" in hist.read_text(encoding="utf-8")
    print("[1] 前缀丢弃 + 归档保留 OK")


async def test_repeat_drop_and_reset() -> None:
    """连续 3 条相同文本(间隔<5min): 第 3 条丢弃; 换文本重置; 超 5min 重置。"""
    tmp = tempfile.mkdtemp(prefix="auto_reply_repeat_")
    handler, rec = make_handler(tmp)

    # 第 1、2 条放行, 第 3 条丢弃
    for _ in range(3):
        await handler.handle_event("bot_001", private_event(text="我在线的，马上回消息"), {})
    assert rec.calls == 2, f"前 2 条应放行第 3 条丢弃, 实际拦截器调了 {rec.calls} 次"

    # 第 4 条相同文本 → 继续丢弃
    await handler.handle_event("bot_001", private_event(text="我在线的，马上回消息"), {})
    assert rec.calls == 2, "第 4 条相同文本应继续丢弃"

    # 换文本 → 重置, 新文本 1、2 条放行
    await handler.handle_event("bot_001", private_event(text="在吗"), {})
    await handler.handle_event("bot_001", private_event(text="在吗"), {})
    assert rec.calls == 4, f"换文本应重置计数, 实际 {rec.calls} 次"

    # 把时间拨回 6 分钟前 → 超窗重置, 下一条相同文本放行
    state = handler._repeat_state[("bot_001", 1001)]
    state["time"] -= 360
    await handler.handle_event("bot_001", private_event(text="在吗"), {})
    assert rec.calls == 5, f"超窗应重置放行, 实际 {rec.calls} 次"
    print("[2] 连续重复判定 + 重置 OK")


async def test_disabled_switch() -> None:
    """开关关闭: 前缀消息与重复消息都放行进拦截器链。"""
    tmp = tempfile.mkdtemp(prefix="auto_reply_off_")
    handler, rec = make_handler(tmp, ignore_auto_reply=False)
    await handler.handle_event("bot_001", private_event(text="[自动回复]x"), {})
    for _ in range(3):
        await handler.handle_event("bot_001", private_event(text="我在线的，马上回消息"), {})
    assert rec.calls == 4, f"关闭时全部放行, 实际 {rec.calls} 次"
    print("[3] 开关关闭不生效 OK")


async def test_config_roundtrip() -> None:
    """GlobalConfig save/load 往返保留 ignore_auto_reply。"""
    tmp = tempfile.mkdtemp(prefix="auto_reply_cfg_")
    cfg = GlobalConfig()
    cfg.ignore_auto_reply = False
    path = str(Path(tmp) / "global.yaml")
    cfg.save(path)
    loaded = GlobalConfig.load(path)
    assert loaded.ignore_auto_reply is False, "save/load 应保留 ignore_auto_reply=False"

    # yaml 里显式 false 也能读回; 默认值应为 True
    Path(path).write_text("ignore_auto_reply: false\n", encoding="utf-8")
    assert GlobalConfig.load(path).ignore_auto_reply is False
    assert GlobalConfig().ignore_auto_reply is True
    print("[4] 配置往返 OK")


async def main() -> None:
    await test_prefix_drop_and_archive()
    await test_repeat_drop_and_reset()
    await test_disabled_switch()
    await test_config_roundtrip()
    print("\nALL AUTO REPLY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
