"""CommandHandler 测试:
1. 未知指令: 同一会话 60 分钟内只提醒一次(冷却期内静默拦截)
2. 不同会话各自独立冷却
3. 冷却期过后再次提醒
4. 指令存在但出错: 每次都回复(不节流)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.interceptors.command_handler import CommandHandler
from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender


def group_event(text: str, user_id: int = 1001, group_id: int = 2001) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=0, self_id=1, post_type="message",
        message_type="group", message_id=1,
        user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def private_event(text: str, user_id: int = 3001) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=0, self_id=1, post_type="message",
        message_type="private", message_id=1,
        user_id=user_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


class BoomError(RuntimeError):
    pass


async def _boom_handler(bot_id, event, args):
    raise BoomError("boom!")


def make_handler() -> CommandHandler:
    h = CommandHandler(context_manager=None, llm_service=None, ws_server=None, plugin_system=None)
    h._commands["boom"] = (_boom_handler, "测试用爆炸指令")
    return h


async def test_unknown_throttle() -> None:
    h = make_handler()
    ev = group_event("/nope1")

    # 1. 第一次: 提醒
    handled, reply = await h.intercept("bot_001", ev, {})
    assert handled and reply and "未知指令" in reply, reply
    assert ("group", "2001") in h._unknown_remind_at

    # 2. 同一会话 60min 内: 不同指令也静默
    handled, reply = await h.intercept("bot_001", group_event("/nope2"), {})
    assert handled and reply is None, reply

    # 3. 同一会话 60min 内: 同指令也静默
    handled, reply = await h.intercept("bot_001", group_event("/nope1"), {})
    assert handled and reply is None, reply

    # 4. 同一会话 60min 内: 别人发的也静默(按会话节流)
    handled, reply = await h.intercept("bot_001", group_event("/nope3", user_id=2002), {})
    assert handled and reply is None, reply
    print("[1] 60min 内同一会话只提醒一次 OK")


async def test_scope_per_chat() -> None:
    h = make_handler()

    # 群 A 提醒过, 群 B 独立冷却 → 仍提醒
    _, r1 = await h.intercept("bot_001", group_event("/foo", group_id=2001), {})
    assert r1 and "未知指令" in r1
    _, r2 = await h.intercept("bot_001", group_event("/bar", group_id=2002), {})
    assert r2 and "未知指令" in r2

    # 私聊也独立
    _, r3 = await h.intercept("bot_001", private_event("/baz"), {})
    assert r3 and "未知指令" in r3

    # 不同 bot 共享同一会话的冷却(多 bot 群只提醒一次)
    _, r4 = await h.intercept("bot_002", group_event("/qux", group_id=2001), {})
    assert r4 is None, "同会话冷却应跨 bot 共享"
    print("[2] 冷却按会话隔离(跨 bot 共享) OK")


async def test_cooldown_expiry() -> None:
    h = make_handler()
    ev = group_event("/exp1")

    _, r1 = await h.intercept("bot_001", ev, {})
    assert r1 and "未知指令" in r1

    # 手动把时间拨回 60 分钟前 → 应再次提醒
    h._unknown_remind_at[("group", "2001")] -= h.UNKNOWN_CMD_COOLDOWN
    _, r2 = await h.intercept("bot_001", ev, {})
    assert r2 and "未知指令" in r2
    print("[3] 冷却期过后再次提醒 OK")


async def test_error_command_always_replies() -> None:
    h = make_handler()

    for i in range(3):
        handled, reply = await h.intercept("bot_001", group_event("/boom"), {})
        assert handled and reply and "boom!" in reply, reply
    print("[4] 指令存在但出错每次都回复 OK")


async def test_prune() -> None:
    h = make_handler()
    # 塞入一条 3 小时前的记录, 触发新提醒时清理
    h._unknown_remind_at[("group", "9999")] = 0.0
    await h.intercept("bot_001", group_event("/fresh"), {})
    assert ("group", "9999") not in h._unknown_remind_at
    print("[5] 过期记录清理 OK")


class _PickBM:
    """固定抽签结果, 模拟多 bot 共享抽签(真实实现按 (群, message_id) 缓存)。"""
    def pick_bot_for_group(self, gid, mid=None):
        return "bot_001"


class _PickWS:
    _bot_manager = _PickBM()


async def test_multi_bot_single_reminder() -> None:
    """多 bot 群: 未知指令只由随机选中的 bot 提醒, 其余静默。"""
    h = CommandHandler(context_manager=None, llm_service=None,
                       ws_server=_PickWS(), plugin_system=None)
    ev = group_event("/nope1")

    # 非选中 bot(bot_002) → 静默拦截, 不回复不记节流
    handled, reply = await h.intercept("bot_002", ev, {})
    assert handled and reply is None, reply
    assert h._unknown_remind_at == {}, "非选中 bot 不应记录节流"

    # 选中 bot(bot_001) → 提醒一次
    handled, reply = await h.intercept("bot_001", ev, {})
    assert handled and reply and "未知指令" in reply, reply

    # 冷却期内, 其它 bot 也静默(会话级节流跨 bot 共享)
    handled, reply = await h.intercept("bot_002", group_event("/nope2"), {})
    assert handled and reply is None, reply
    print("[6] 多 bot 群未知指令单 bot 提醒 OK")


async def main() -> None:
    await test_unknown_throttle()
    await test_scope_per_chat()
    await test_cooldown_expiry()
    await test_error_command_always_replies()
    await test_prune()
    await test_multi_bot_single_reminder()
    print("\nALL COMMAND THROTTLE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
