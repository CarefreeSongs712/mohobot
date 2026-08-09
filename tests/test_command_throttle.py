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
    assert ("bot_001", "group", "2001") in h._unknown_remind_at

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

    # 不同 bot 也独立
    _, r4 = await h.intercept("bot_002", group_event("/qux", group_id=2001), {})
    assert r4 and "未知指令" in r4
    print("[2] 冷却按 (bot, 会话) 隔离 OK")


async def test_cooldown_expiry() -> None:
    h = make_handler()
    ev = group_event("/exp1")

    _, r1 = await h.intercept("bot_001", ev, {})
    assert r1 and "未知指令" in r1

    # 手动把时间拨回 60 分钟前 → 应再次提醒
    h._unknown_remind_at[("bot_001", "group", "2001")] -= h.UNKNOWN_CMD_COOLDOWN
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
    h._unknown_remind_at[("bot_001", "group", "9999")] = 0.0
    await h.intercept("bot_001", group_event("/fresh"), {})
    assert ("bot_001", "group", "9999") not in h._unknown_remind_at
    print("[5] 过期记录清理 OK")


async def main() -> None:
    await test_unknown_throttle()
    await test_scope_per_chat()
    await test_cooldown_expiry()
    await test_error_command_always_replies()
    await test_prune()
    print("\nALL COMMAND THROTTLE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
