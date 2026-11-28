"""Issue #125: 引用消息缓存必须按 (bot_id, chat_type, chat_id, reply_id) 隔离。

复现: 旧实现 `_quote_cache` 只用 reply_id 作键, 600s TTL 内不同账号/不同会话
里的同号消息会互相串用解析结果, 把别的群的引用内容写进当前上下文。
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.message_handler import MessageHandler
from mohobot.models.onebot import PrivateMessageEvent, Sender


def _handler(ws) -> MessageHandler:
    h = MessageHandler.__new__(MessageHandler)
    h._ws = ws
    h._image_cache = None
    h._llm = None
    h._quote_cache = {}
    return h


class _WS:
    """按调用顺序依次返回 payload; 记录每次 get_msg, 用于断言是否命中缓存。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls: list[tuple[str, int]] = []

    async def send_to_bot(self, bot_id, action, params, **kwargs):
        assert action == "get_msg"
        self.calls.append((bot_id, params["message_id"]))
        index = min(len(self.calls) - 1, len(self._payloads) - 1)
        return self._payloads[index]

    async def get_nickname(self, bot_id, uid, group_id=None):
        return "用户"


def _payload(text, uid=123):
    return {
        "status": "ok",
        "retcode": 0,
        "data": {
            "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"user_id": uid, "nickname": "用户"},
        },
    }


def _group_event(reply_id="42", group_id=888):
    return SimpleNamespace(
        message=[{"type": "reply", "data": {"id": reply_id}}],
        group_id=group_id,
    )


def _private_event(reply_id="42", user_id=10001):
    return PrivateMessageEvent(
        time=0, self_id=1, post_type="message",
        message_type="private", message_id=100,
        user_id=user_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "reply", "data": {"id": reply_id}}],
    )


async def test_same_reply_id_different_bots_not_shared():
    """两个账号各自会话里回复同一 ID: 必须各查各的, 不能串内容。"""
    ws = _WS([_payload("甲群的旧消息"), _payload("乙群的旧消息")])
    h = _handler(ws)

    first = await h._resolve_quoted_display("bot_001", _group_event())
    second = await h._resolve_quoted_display("bot_002", _group_event())

    assert first == "用户(123): 甲群的旧消息", first
    assert second == "用户(123): 乙群的旧消息", second
    assert ws.calls == [("bot_001", 42), ("bot_002", 42)], ws.calls


async def test_same_reply_id_different_groups_not_shared():
    """同一账号不同群里的同号消息必须隔离。"""
    ws = _WS([_payload("群 888 的消息"), _payload("群 999 的消息")])
    h = _handler(ws)

    first = await h._resolve_quoted_display("bot_001", _group_event(group_id=888))
    second = await h._resolve_quoted_display("bot_001", _group_event(group_id=999))

    assert first.endswith("群 888 的消息"), first
    assert second.endswith("群 999 的消息"), second
    assert len(ws.calls) == 2, ws.calls


async def test_same_reply_id_private_vs_group_not_shared():
    """私聊与群聊同号消息必须隔离。"""
    ws = _WS([_payload("群里的消息"), _payload("私聊的消息")])
    h = _handler(ws)

    group_text = await h._resolve_quoted_display("bot_001", _group_event(group_id=888))
    private_text = await h._resolve_quoted_display("bot_001", _private_event())

    assert group_text.endswith("群里的消息"), group_text
    assert private_text.endswith("私聊的消息"), private_text
    assert len(ws.calls) == 2, ws.calls


async def test_negative_cache_does_not_leak_across_sessions():
    """一个会话解析失败留下的负缓存, 不能让另一个会话直接返回空。"""
    ws = _WS([
        {"status": "ok", "retcode": 0, "data": None},  # bot_001: 失败
        _payload("另一个群的真实消息"),  # bot_002: 成功
    ])
    h = _handler(ws)

    missing = await h._resolve_quoted_display("bot_001", _group_event())
    assert missing == ""

    found = await h._resolve_quoted_display("bot_002", _group_event())
    assert found.endswith("另一个群的真实消息"), found
    assert len(ws.calls) == 2, ws.calls


async def test_cache_hit_within_same_session():
    """同会话同 ID 正常命中缓存, 不重复调用接口。"""
    ws = _WS([_payload("大家好呀")])
    h = _handler(ws)
    event = _group_event()

    first = await h._resolve_quoted_display("bot_001", event)
    second = await h._resolve_quoted_display("bot_001", event)

    assert first == second == "用户(123): 大家好呀"
    assert len(ws.calls) == 1, ws.calls


async def test_expired_entry_refetches_same_session():
    """TTL 过期后同会话也要重新取, 不能永久沿用旧内容。"""
    ws = _WS([_payload("旧内容"), _payload("新内容")])
    h = _handler(ws)
    event = _group_event()

    await h._resolve_quoted_display("bot_001", event)
    key = ("bot_001", "group", "888", "42")
    expires_at, display = h._quote_cache[key]
    h._quote_cache[key] = (time.time() - 1.0, display)

    refreshed = await h._resolve_quoted_display("bot_001", event)
    assert refreshed.endswith("新内容"), refreshed
    assert len(ws.calls) == 2, ws.calls


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if asyncio.iscoroutinefunction(t):
            asyncio.run(t())
        else:
            t()
        print(f"PASS {t.__name__}")
    print("\nALL ISSUE #125 TESTS PASSED")


if __name__ == "__main__":
    main()
