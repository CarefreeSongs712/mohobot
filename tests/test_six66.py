"""six66 数字梗插件测试:
1. 精确匹配: 6/66/666 触发, 非精确(如 6666/66 6)不触发
2. 概率: mock random 命中/未命中
3. 多 bot 去重: 群内只最小 bot 回复
4. 仅群聊: 私聊不触发
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import mock

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
    spec = importlib.util.spec_from_file_location("six66_plugin_main", "plugins/six66/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_plugin(ws=None):
    mod = load_plugin()
    inst = mod.Plugin()
    inst._ws_server = ws
    return inst


async def test_exact_match_and_replies():
    inst = make_plugin()
    # 6 / 66 / 666 各触发词回复内容
    for trigger, expected in [
        ("6", "6=5+2+0+1+3-1-4"),
        ("66", "66=52+0×13+14"),
        ("666", "666=(52-0!)×13-1+4"),
    ]:
        with mock.patch("plugins.six66.main.random.random", return_value=0.1):  # 命中(10<40)
            handled, reply = await inst.on_message_observed(
                "bot_001", make_group_event(2001, trigger), {},
            )
        assert handled and expected in reply, (trigger, reply)
    # 非精确不触发
    for t in ("6666", "66 6", "6 6", "6666", "x666"):
        handled, _ = await inst.on_message_observed(
            "bot_001", make_group_event(2001, t), {},
        )
        assert not handled, f"{t!r} 不应触发"
    print("[+] 精确匹配与回复 OK")


async def test_probability():
    inst = make_plugin()
    # 未命中(80 >= 40) → 不消费
    with mock.patch("plugins.six66.main.random.random", return_value=0.8):
        handled, reply = await inst.on_message_observed(
            "bot_001", make_group_event(2001, "666"), {},
        )
    assert not handled, "80% 未命中不应触发"
    # 命中(0.1 < 0.4) → 回复
    with mock.patch("plugins.six66.main.random.random", return_value=0.1):
        handled, reply = await inst.on_message_observed(
            "bot_001", make_group_event(2001, "666"), {},
        )
    assert handled and "你是不是在喜欢我呀" in reply
    # 概率配置生效: 100 必触发
    inst.plugin_config["probability"] = 100
    with mock.patch("plugins.six66.main.random.random", return_value=0.99):
        handled, _ = await inst.on_message_observed(
            "bot_001", make_group_event(2001, "666"), {},
        )
    assert handled
    # 0 不触发
    inst.plugin_config["probability"] = 0
    with mock.patch("plugins.six66.main.random.random", return_value=0.01):
        handled, _ = await inst.on_message_observed(
            "bot_001", make_group_event(2001, "666"), {},
        )
    assert not handled
    print("[+] 概率 OK")


async def test_min_bot_dedup():
    from mohobot.bot_manager import BotManager, BotInstance
    from mohobot.models.config import BotConfig

    bm = BotManager(data_dir=tempfile.mkdtemp())
    bm._bots["bot_001"] = BotInstance("bot_001", None, BotConfig(qq=1000))
    bm._bots["bot_002"] = BotInstance("bot_002", None, BotConfig(qq=2000))
    bm.note_group_message("bot_001", 888888)
    bm.note_group_message("bot_002", 888888)

    class WS:
        _bot_manager = bm

    inst = make_plugin(WS())
    with mock.patch("plugins.six66.main.random.random", return_value=0.1):
        handled_min, _ = await inst.on_message_observed(
            "bot_001", make_group_event(2001, "666"), {},
        )
        handled_other, _ = await inst.on_message_observed(
            "bot_002", make_group_event(2001, "666"), {},
        )
    assert handled_min, "最小 bot 应触发"
    assert not handled_other, "非最小 bot 不应触发"
    # 无 bot_manager 引用 → 不去重(单 bot 场景)
    inst2 = make_plugin()
    with mock.patch("plugins.six66.main.random.random", return_value=0.1):
        handled, _ = await inst2.on_message_observed(
            "bot_002", make_group_event(2001, "666"), {},
        )
    assert handled
    print("[+] 最小 bot 去重 OK")


async def test_group_only():
    inst = make_plugin()
    with mock.patch("plugins.six66.main.random.random", return_value=0.1):
        handled, _ = await inst.on_message_observed(
            "bot_001", make_private_event(2001, "666"), {},
        )
    assert not handled, "私聊不应触发"
    print("[+] 仅群聊 OK")


async def _main() -> int:
    import asyncio
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
