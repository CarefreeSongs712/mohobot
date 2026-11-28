"""Issue #127: 多账号全局指令去重 / 禁用 bot 候选过滤 / 重连接替的陈旧派发。

复现三条缺陷:
1. 抽签 key 用各账号自己的 message_id → 同一条物理消息各账号分桶,
   要么都执行, 要么都被别的账号的抽签结果 defer。
2. 已禁用但仍保持连接的 bot 仍进入候选 → 抽中后其事件在 _dispatch 被丢弃, 没人回复。
3. 同账号重连时 register 直接替换实例, 旧连接循环仍继续派发 → 重叠窗口重复处理。
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotInstance, BotManager
from mohobot.message_handler import MessageHandler
from mohobot.models.config import BotConfig
from mohobot.models.onebot import GroupMessageEvent, Sender
from mohobot.ws_server import WSServer


class _FakeSock:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


def _group_event(text: str, *, message_id: int, group_id: int = 500,
                  user_id: int = 1001, time: int = 1700) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=time, self_id=1, post_type="message",
        message_type="group", message_id=message_id,
        user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def _raw_group(message_id: int, text: str = "/help", group_id: int = 500) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "time": 1700,
        "self_id": 1,
        "message_id": message_id,
        "user_id": 1001,
        "group_id": group_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": 1001, "nickname": "用户"},
    }


def _make_manager(tmp: Path) -> BotManager:
    bm = BotManager(data_dir=str(tmp))
    for bot_id, qq, enabled in (("bot_001", 111, True), ("bot_002", 222, True)):
        cfg = BotConfig(bot_id=bot_id, qq=qq, nickname=bot_id, enabled=enabled)
        cfg.save(bm._bot_config_path(bot_id))
    return bm


def _install(bm: BotManager, bot_id: str, qq: int, enabled: bool = True) -> BotInstance:
    cfg = BotConfig(bot_id=bot_id, qq=qq, nickname=bot_id, enabled=enabled)
    inst = BotInstance(bot_id, _FakeSock(), cfg)
    bm._bots[bot_id] = inst
    bm.note_group_message(bot_id, "500")
    return inst


def _handler(bm: BotManager) -> MessageHandler:
    h = MessageHandler.__new__(MessageHandler)
    h._ws = SimpleNamespace(_bot_manager=bm)
    h._plugins = None
    h._ban_interceptor = None
    h._interceptors = []
    return h


async def test_disabled_bot_excluded_from_candidates():
    """禁用的 bot 不进入候选: 不会抽中它再被入站丢弃。"""
    with tempfile.TemporaryDirectory() as td:
        bm = _make_manager(Path(td))
        _install(bm, "bot_001", 111, enabled=False)
        _install(bm, "bot_002", 222, enabled=True)
        bm.note_group_message("bot_001", "500")
        bm.note_group_message("bot_002", "500")

        assert bm.bots_in_group("500") == ["bot_002"], bm.bots_in_group("500")
        for index in range(5):
            assert bm.pick_bot_for_group("500", f"mid-{index}") == "bot_002"


async def test_disabling_cached_winner_refreshes_candidates():
    """抽签缓存里的 owner 被禁用后, 下一次抽签必须重选可用 bot。"""
    with tempfile.TemporaryDirectory() as td:
        bm = _make_manager(Path(td))
        first = _install(bm, "bot_001", 111, enabled=True)
        second = _install(bm, "bot_002", 222, enabled=False)
        bm.note_group_message("bot_001", "500")
        bm.note_group_message("bot_002", "500")

        assert bm.pick_bot_for_group("500", "same-mid") == "bot_001"
        first.config.enabled = False
        second.config.enabled = True

        assert bm.pick_bot_for_group("500", "same-mid") == "bot_002"


async def test_event_only_bot_cannot_keep_or_claim_global_command():
    """Losing API transport invalidates an owner while Event remains connected."""
    with tempfile.TemporaryDirectory() as td:
        bm = _make_manager(Path(td))
        event = bm.register(111, _FakeSock(), client_role="Event")
        api = bm.register(111, _FakeSock(), client_role="API")
        healthy = bm.register(222, _FakeSock())
        bm.note_group_message("bot_001", "500")
        bm.note_group_message("bot_002", "500")
        healthy.config.enabled = False
        fingerprint = "owner-before-disconnect"
        assert bm.claim_global_command("500", fingerprint) == "bot_001"
        healthy.config.enabled = True
        bm.unregister(api)

        assert bm.get("bot_001") is event and bm.is_current(event)
        assert bm.bots_in_group("500") == ["bot_002"]
        assert bm.claim_global_command("500", fingerprint) == "bot_002"
        for index in range(8):
            assert bm.pick_bot_for_group("500", f"after-disconnect-{index}") == "bot_002"
        handler = _handler(bm)
        assert handler._should_defer_global_command(
            "bot_001", _group_event("/help", message_id=1),
        )
        assert not handler._should_defer_global_command(
            "bot_002", _group_event("/help", message_id=2),
        )
        await healthy.send({"action": "send_group_msg", "params": {"group_id": 500}})
        assert len(healthy.ws.sent) == 1

        replacement = bm.register(111, _FakeSock(), client_role="API")
        assert bm.is_current(replacement) and bm._is_eligible("bot_001")


async def test_claim_global_command_is_stable_across_accounts():
    """各账号 message_id 不同, 但同一物理消息认领到同一个 owner。"""
    with tempfile.TemporaryDirectory() as td:
        bm = _make_manager(Path(td))
        _install(bm, "bot_001", 111)
        _install(bm, "bot_002", 222)
        bm.note_group_message("bot_001", "500")
        bm.note_group_message("bot_002", "500")

        fingerprint = "1001:1700:同一个物理消息"
        owner_first = bm.claim_global_command("500", fingerprint)
        owner_second = bm.claim_global_command("500", fingerprint)

        assert owner_first in ("bot_001", "bot_002"), owner_first
        assert owner_first == owner_second, (owner_first, owner_second)


async def test_global_command_exactly_one_bot_executes():
    """两个 handler 收到同一物理消息(各账号 message_id 不同): 恰好一只执行。"""
    with tempfile.TemporaryDirectory() as td:
        bm = _make_manager(Path(td))
        _install(bm, "bot_001", 111)
        _install(bm, "bot_002", 222)
        bm.note_group_message("bot_001", "500")
        bm.note_group_message("bot_002", "500")

        handler_a = _handler(bm)
        handler_b = _handler(bm)
        event_a = _group_event("/help", message_id=9001)
        event_b = _group_event("/help", message_id=9002)

        deferred = [
            handler_a._should_defer_global_command("bot_001", event_a),
            handler_b._should_defer_global_command("bot_002", event_b),
        ]
        assert sum(deferred) == 1, deferred
        # 再次判定结果稳定(认领缓存生效), 不会一会儿都执行一会儿都跳过
        assert handler_a._should_defer_global_command("bot_001", event_a) == deferred[0]
        assert handler_b._should_defer_global_command("bot_002", event_b) == deferred[1]


async def test_mention_directed_command_not_deduped():
    """@ 定向优先: 被点名的 bot 不因抽签被 defer。"""
    with tempfile.TemporaryDirectory() as td:
        bm = _make_manager(Path(td))
        _install(bm, "bot_001", 111)
        _install(bm, "bot_002", 222)
        bm.note_group_message("bot_001", "500")
        bm.note_group_message("bot_002", "500")

        handler = _handler(bm)
        event = _group_event("/help", message_id=9100)
        event.message = [
            {"type": "at", "data": {"qq": "111"}},
            {"type": "text", "data": {"text": " /help"}},
        ]
        assert handler._mentioned_bot_ids(event) == {"bot_001"}
        assert handler._should_defer_global_command("bot_001", event) is False


async def test_reconnect_retires_old_connection():
    """同账号重连: 旧实例标记 stale/关闭 WS, 新实例为当前实例。"""
    with tempfile.TemporaryDirectory() as td:
        bm = _make_manager(Path(td))
        old_sock = _FakeSock()
        old = bm.register(111, old_sock)
        new_sock = _FakeSock()
        new = bm.register(111, new_sock)
        await asyncio.sleep(0)  # 让关闭旧连接的调度任务执行

        assert old is not new
        assert old.stale is True, "旧连接必须被标记失效"
        assert old_sock.closed is True, "旧连接 WS 应被关闭"
        assert new.stale is False
        assert new.generation > old.generation
        assert bm.get("bot_001") is new
        assert bm.is_current(new) is True
        assert bm.is_current(old) is False

        # 旧连接最后调用的 unregister 不能删掉新实例
        bm.unregister(old)
        assert bm.get("bot_001") is new


async def test_dispatch_ignores_stale_connection():
    """重连重叠窗口: 旧连接继续推来的事件必须被丢弃。"""
    with tempfile.TemporaryDirectory() as td:
        bm = _make_manager(Path(td))
        old = bm.register(111, _FakeSock())
        new = bm.register(111, _FakeSock())
        ws = WSServer(bot_manager=bm, data_dir=str(td))
        received: list[tuple[str, object]] = []

        async def _on_event(bot_id, event, raw):
            received.append((bot_id, raw.get("message_id")))

        ws.set_event_callback(_on_event)
        try:
            await ws._dispatch(old, _raw_group(1))
            await asyncio.sleep(0)
            assert received == [], received

            await ws._dispatch(new, _raw_group(2))
            await asyncio.sleep(0)
            assert received == [("bot_001", 2)], received
        finally:
            await ws._outbound_scheduler.close()


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if asyncio.iscoroutinefunction(t):
            asyncio.run(t())
        else:
            t()
        print(f"PASS {t.__name__}")
    print("\nALL ISSUE #127 TESTS PASSED")


if __name__ == "__main__":
    main()
