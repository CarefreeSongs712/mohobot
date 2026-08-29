"""广播插件全体广播去重回归测试。

覆盖:
1. 全体广播去重: 同一群/同一好友在多个 bot 列表中只发一条(首选 bot_id 最小者)
2. 指定单个 bot 广播: 不去重, 该 bot 的全部好友/群各发一条
3. 发送失败回退: 首选 bot 发送失败时自动回退到该群的其他 bot
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "broadcast" / "main.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("broadcast_plugin_test", _PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Plugin


class FakeWS:
    """Fake ws_server: canned lists + recorded sends."""

    def __init__(self, bot_groups, bot_friends, fail_sends=None):
        self.bot_groups = bot_groups
        self.bot_friends = bot_friends
        self.fail_sends = fail_sends or set()  # {(kind, bot_id, target_id)}
        self.sent = []  # (kind, bot_id, target_id, text)
        self._bot_manager = SimpleNamespace(all_bots=[
            SimpleNamespace(bot_id="bot_001"),
            SimpleNamespace(bot_id="bot_002"),
        ])

    async def send_to_bot(self, bid, action, params, wait_response=False, timeout=None):
        if action == "get_friend_list":
            return {"data": self.bot_friends.get(bid, [])}
        if action == "get_group_list":
            return {"data": self.bot_groups.get(bid, [])}
        return {}

    async def send_private_msg(self, bid, uid, text):
        if ("private", bid, int(uid)) in self.fail_sends:
            raise RuntimeError("private send fail")
        self.sent.append(("private", bid, int(uid), text))

    async def send_group_msg(self, bid, gid, text):
        if ("group", bid, int(gid)) in self.fail_sends:
            raise RuntimeError("group send fail")
        self.sent.append(("group", bid, int(gid), text))


def _make_event():
    from mohobot.models.onebot import PrivateMessageEvent, Sender
    return PrivateMessageEvent(
        time=0, self_id=0, post_type="message",
        message=[{"type": "text", "data": {"text": "/广播确认"}}],
        user_id=999, message_id=1,
        sender=Sender(user_id=999, nickname="管理员"),
    )


def _make_ws(fail_sends=None):
    # bot_001: 群 [1,2] 好友 [11];  bot_002: 群 [1,3] 好友 [11,12]
    return FakeWS(
        fail_sends=fail_sends,
        bot_groups={
            "bot_001": [{"group_id": 1, "group_name": "g1"}, {"group_id": 2, "group_name": "g2"}],
            "bot_002": [{"group_id": 1, "group_name": "g1"}, {"group_id": 3, "group_name": "g3"}],
        },
        bot_friends={
            "bot_001": [{"user_id": 11, "nickname": "u11"}],
            "bot_002": [{"user_id": 11, "nickname": "u11"}, {"user_id": 12, "nickname": "u12"}],
        },
    )


async def test_all_bots_dedup() -> None:
    """全体广播: 群 1 两个 bot 都在 → 只发一条; 好友 11 两个 bot 都有 → 只发一条。"""
    Plugin = _load_plugin()
    ws = _make_ws()
    Plugin._ws_server = ws
    plugin = Plugin()

    await plugin._run_broadcast(
        bot_id="bot_001", event=_make_event(),
        content="测试广播", target_bot="", scope="全部",
    )
    group_sends = [(b, g) for kind, b, g, _ in ws.sent if kind == "group"]
    private_sends = [(b, u) for kind, b, u, _ in ws.sent if kind == "private" and u != 999]
    # 群: 1→bot_001(最小), 2→bot_001, 3→bot_002;  群 1 只出现一次
    assert sorted(group_sends) == [("bot_001", 1), ("bot_001", 2), ("bot_002", 3)], group_sends
    # 好友: 11→bot_001, 12→bot_002; 好友 11 只出现一次
    assert sorted(private_sends) == [("bot_001", 11), ("bot_002", 12)], private_sends
    # 汇总带去重信息
    summary = ws.sent[-1][3]
    assert "去重跳过" in summary, summary
    print("[1] 全体广播去重 OK")


async def test_single_bot_no_dedup() -> None:
    """指定 bot 广播: 不去重, 该 bot 的全部群/好友各发一条。"""
    Plugin = _load_plugin()
    ws = _make_ws()
    Plugin._ws_server = ws
    plugin = Plugin()

    await plugin._run_broadcast(
        bot_id="bot_001", event=_make_event(),
        content="测试广播", target_bot="bot_002", scope="全部",
    )
    group_sends = [(b, g) for kind, b, g, _ in ws.sent if kind == "group"]
    private_sends = [(b, u) for kind, b, u, _ in ws.sent if kind == "private" and u != 999]
    assert sorted(group_sends) == [("bot_002", 1), ("bot_002", 3)], group_sends
    assert sorted(private_sends) == [("bot_002", 11), ("bot_002", 12)], private_sends
    print("[2] 单 bot 广播不去重 OK")


async def test_fail_fallback() -> None:
    """首选 bot 发送失败 → 自动回退到该群的其他 bot。"""
    Plugin = _load_plugin()
    ws = _make_ws(fail_sends={("group", "bot_001", 1)})
    Plugin._ws_server = ws
    plugin = Plugin()

    await plugin._run_broadcast(
        bot_id="bot_001", event=_make_event(),
        content="测试广播", target_bot="", scope="群聊",
    )
    group_sends = [(b, g) for kind, b, g, _ in ws.sent if kind == "group"]
    # 群 1 首选 bot_001 失败 → bot_002 兜底; 只成功一条
    assert ("bot_002", 1) in group_sends and ("bot_001", 1) not in group_sends, group_sends
    assert group_sends.count(("bot_002", 1)) == 1
    summary = ws.sent[-1][3]
    assert "失败 0 条" in summary, summary
    print("[3] 发送失败自动回退 OK")


async def main() -> None:
    await test_all_bots_dedup()
    await test_single_bot_no_dedup()
    await test_fail_fallback()
    print("\nALL BROADCAST TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())