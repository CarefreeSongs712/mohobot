"""群聊 @ 定向指令测试:

规则: 群消息里 @ 了具体 bot 时, 指令只由被 @ 的 bot 执行。
- @ 一只: 只有它执行, 其余静默(不抽签、不走插件观察钩子、不进 LLM)
- @ 多只: 被 @ 的每只都执行
- @全体: 不算点名任何 bot, 维持原有抽签语义
- 没有 @: 维持原有抽签语义(全局指令抽一只 / 普通指令全执行)

运行: python tests/test_mention_directed.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotInstance, BotManager
from mohobot.message_handler import MessageHandler
from mohobot.models.config import BotConfig, GlobalConfig, ReplyConfig
from mohobot.models.onebot import GroupMessageEvent
from mohobot.utils.cq_code import extract_at_targets


GROUP_ID = 20002


# ── 构造 ───────────────────────────────────────────────────


class _Sock:
    async def send(self, payload: str) -> None:  # pragma: no cover - 仅满足接口
        pass


def _make_bot_manager(tmp: Path) -> BotManager:
    bm = BotManager(data_dir=str(tmp))
    for bot_id, qq in (("bot_001", 111), ("bot_002", 222), ("bot_003", 333)):
        inst = BotInstance(
            bot_id, _Sock(),
            BotConfig(bot_id=bot_id, qq=qq, nickname=bot_id),
        )
        bm._bots[bot_id] = inst
        bm.note_group_message(bot_id, GROUP_ID)
    return bm


class _WS:
    def __init__(self, bm: BotManager):
        self._bot_manager = bm
        self.sent: list[tuple] = []
        self.forwards: list[tuple] = []

    async def send_group_msg(self, bot_id, group_id, message, source="auto"):
        self.sent.append((bot_id, group_id, message))

    async def send_private_msg(self, bot_id, user_id, message, source="auto"):
        self.sent.append((bot_id, user_id, message))

    async def send_group_forward_msg(self, bot_id, group_id, nodes, **kwargs):
        self.forwards.append((bot_id, group_id, nodes))

    async def send_to_bot(self, bot_id, action, params, **kwargs):
        return {"status": "ok", "data": {}}


class _Plugins:
    """记录每个 bot 是否走到了观察钩子/拦截链。"""

    def __init__(self):
        self._plugins = []
        self.observed: list[str] = []
        self.intercepted: list[str] = []
        self.observed_reply: str | None = None

    async def collect_perception(self, bot_id, event, raw):
        return ""

    async def dispatch_observed(self, bot_id, event, raw):
        self.observed.append(bot_id)
        if self.observed_reply:
            return (True, self.observed_reply)
        return (False, None)

    async def intercept(self, bot_id, event, raw):
        self.intercepted.append(bot_id)
        text = _text(event)
        if text.startswith("/help"):
            return (True, f"help from {bot_id}")
        return (False, None)


class _Context:
    async def load_context(self, *args):
        return []

    async def capture_session(self, *args):
        return {}

    async def append_context(self, *args):
        return True


class _LLM:
    def __init__(self):
        self.calls: list[str] = []

    async def chat_stream(self, **kwargs):
        self.calls.append(kwargs.get("bot_id", "?"))
        yield ("llm reply", True)


def _text(event) -> str:
    from mohobot.utils.cq_code import extract_plain_text
    return extract_plain_text(event.message)


def _event(message, *, self_id: int, mid: int = 1) -> GroupMessageEvent:
    return GroupMessageEvent.from_dict({
        "time": 1700000000,
        "self_id": self_id,
        "post_type": "message",
        "message_type": "group",
        "message_id": mid,
        "group_id": GROUP_ID,
        "user_id": 9999,
        "message": message,
        "raw_message": "",
        "sender": {"user_id": 9999, "nickname": "用户"},
    })


def _make_handler(tmp: Path, ws: _WS, plugins: _Plugins, llm: _LLM) -> MessageHandler:
    cfg = GlobalConfig(
        data_dir=str(tmp),
        reply=ReplyConfig(segment_reply=False, reply_quote=False),
        group_recent_msgs_count=0,
    )
    handler = MessageHandler(
        ws_server=ws,
        context_manager=_Context(),
        llm_service=llm,
        plugin_system=plugins,
        data_dir=str(tmp),
        reply_config=cfg.reply,
        global_config=cfg,
    )
    handler.set_interceptors([plugins])
    return handler


def _at(qq: int) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def _txt(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


# ── 单元: CQ 解析 ──────────────────────────────────────────


def test_extract_at_targets():
    """@ 目标解析: 数组/CQ 字符串/多个/去重/@全体/畸形输入。"""
    assert extract_at_targets([_at(111), _txt(" /help")]) == ["111"]
    assert extract_at_targets([_at(111), _at(222)]) == ["111", "222"]
    assert extract_at_targets([_at(111), _at(111)]) == ["111"], "@ 同一只两次只算一次"
    assert extract_at_targets([{"type": "at", "data": {"qq": "all"}}]) == [], "@全体不点名"
    assert extract_at_targets("​[CQ:at,qq=111] /help") == ["111"], "CQ 字符串同样可解析"
    assert extract_at_targets([_txt("无 @")]) == []
    assert extract_at_targets("") == []
    assert extract_at_targets(None) == []
    assert extract_at_targets("[CQ:at,qq=]") == [], "空 qq 忽略"
    print("[1] extract_at_targets 解析 OK")


def test_bot_ids_for_qqs():
    """QQ → bot_id: 只返回已绑定在线的 bot; 未知 QQ / qq=0 忽略。"""
    with tempfile.TemporaryDirectory() as td:
        bm = _make_bot_manager(Path(td))
        assert bm.bot_ids_for_qqs(["111"]) == {"bot_001"}
        assert bm.bot_ids_for_qqs(["111", "333"]) == {"bot_001", "bot_003"}
        assert bm.bot_ids_for_qqs(["999"]) == set(), "未绑定 QQ 不映射"
        assert bm.bot_ids_for_qqs(["all"]) == set()
        assert bm.bot_ids_for_qqs([]) == set()
        # qq=0 表示未绑定, 不能被 @0 命中
        unbound = BotInstance(
            "bot_009", _Sock(), BotConfig(bot_id="bot_009", qq=0),
        )
        bm._bots["bot_009"] = unbound
        assert bm.bot_ids_for_qqs(["0"]) == set(), "qq=0 不应被 @ 命中"
        print("[2] bot_ids_for_qqs 映射 OK")


# ── 集成: 主链行为 ─────────────────────────────────────────


async def test_at_single_bot_only_it_responds():
    """@ 一只: 只有它执行指令, 其余 bot 完全不进入处理。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bm = _make_bot_manager(tmp)
        ws, plugins, llm = _WS(bm), _Plugins(), _LLM()
        handler = _make_handler(tmp, ws, plugins, llm)
        try:
            # 同一条消息经三只 bot 各推送一次(多账号 id 不同)
            for i, (bot_id, qq) in enumerate(
                (("bot_001", 111), ("bot_002", 222), ("bot_003", 333))
            ):
                event = _event([_at(222), _txt(" /help")], self_id=qq, mid=100 + i)
                await handler.handle_event(bot_id, event, {
                    "time": 1700000000, "self_id": qq, "post_type": "message",
                    "message_type": "group", "message_id": 100 + i,
                    "group_id": GROUP_ID, "user_id": 9999,
                    "message": [_at(222), _txt(" /help")],
                })
            assert plugins.intercepted == ["bot_002"], (
                f"只有被 @ 的 bot 执行指令, 实际: {plugins.intercepted}"
            )
            # 观察钩子对三只 bot 都运行(保留活跃记录等副作用), 但只有被 @ 的能消费
            assert sorted(plugins.observed) == ["bot_001", "bot_002", "bot_003"], (
                f"观察钩子应对所有 bot 运行, 实际: {plugins.observed}"
            )
            assert llm.calls == [], "被 @ 的 bot 命中命令, 不应进 LLM"
        finally:
            await handler.close()
        print("[3] @ 单只 → 仅它执行 OK")


async def test_at_multiple_bots_all_respond():
    """@ 多只: 被 @ 的每只都执行一次。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bm = _make_bot_manager(tmp)
        ws, plugins, llm = _WS(bm), _Plugins(), _LLM()
        handler = _make_handler(tmp, ws, plugins, llm)
        msg = [_at(111), _at(333), _txt(" /help")]
        try:
            for i, (bot_id, qq) in enumerate(
                (("bot_001", 111), ("bot_002", 222), ("bot_003", 333))
            ):
                event = _event(msg, self_id=qq, mid=200 + i)
                await handler.handle_event(bot_id, event, {
                    "time": 1700000000, "self_id": qq, "post_type": "message",
                    "message_type": "group", "message_id": 200 + i,
                    "group_id": GROUP_ID, "user_id": 9999, "message": msg,
                })
            assert sorted(plugins.intercepted) == ["bot_001", "bot_003"], (
                f"被 @ 的两只都应执行, 实际: {plugins.intercepted}"
            )
            # 未被 @ 的 bot 仍跑观察钩子(副作用), 但不消费、不回复
            assert "bot_002" in plugins.observed
            assert all(bot in ("bot_001", "bot_003") for bot, *_ in ws.sent), (
                f"未被 @ 的 bot 不得发送消息: {ws.sent}"
            )
        finally:
            await handler.close()
        print("[4] @ 多只 → 各自执行 OK")


async def test_at_all_keeps_lottery():
    """@全体 不算点名: 维持原有抽签语义(全局指令只由抽中的一只回复)。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bm = _make_bot_manager(tmp)
        bm.pick_bot_for_group = lambda gid, mid=None: "bot_002"  # 固定抽签结果
        ws, plugins, llm = _WS(bm), _Plugins(), _LLM()
        handler = _make_handler(tmp, ws, plugins, llm)
        msg = [{"type": "at", "data": {"qq": "all"}}, _txt(" /help")]
        try:
            for i, (bot_id, qq) in enumerate(
                (("bot_001", 111), ("bot_002", 222), ("bot_003", 333))
            ):
                event = _event(msg, self_id=qq, mid=300 + i)
                await handler.handle_event(bot_id, event, {
                    "time": 1700000000, "self_id": qq, "post_type": "message",
                    "message_type": "group", "message_id": 300 + i,
                    "group_id": GROUP_ID, "user_id": 9999, "message": msg,
                })
            assert plugins.intercepted == ["bot_002"], (
                f"@全体 应走抽签, 实际: {plugins.intercepted}"
            )
        finally:
            await handler.close()
        print("[5] @全体 → 仍走抽签 OK")


async def test_no_at_keeps_lottery():
    """没有 @: 全局指令仍只由抽中的一只执行(回归保护)。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bm = _make_bot_manager(tmp)
        bm.pick_bot_for_group = lambda gid, mid=None: "bot_003"
        ws, plugins, llm = _WS(bm), _Plugins(), _LLM()
        handler = _make_handler(tmp, ws, plugins, llm)
        msg = [_txt("/help")]
        try:
            for i, (bot_id, qq) in enumerate(
                (("bot_001", 111), ("bot_002", 222), ("bot_003", 333))
            ):
                event = _event(msg, self_id=qq, mid=400 + i)
                await handler.handle_event(bot_id, event, {
                    "time": 1700000000, "self_id": qq, "post_type": "message",
                    "message_type": "group", "message_id": 400 + i,
                    "group_id": GROUP_ID, "user_id": 9999, "message": msg,
                })
            assert plugins.intercepted == ["bot_003"], (
                f"无 @ 应走抽签, 实际: {plugins.intercepted}"
            )
        finally:
            await handler.close()
        print("[6] 无 @ → 仍走抽签 OK")


async def test_at_bot_skips_observer_for_others():
    """@ 了 A 时, B 的观察钩子仍运行(保留副作用), 但不得消费消息。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bm = _make_bot_manager(tmp)
        ws, plugins, llm = _WS(bm), _Plugins(), _LLM()
        plugins.observed_reply = "observer consumed"
        handler = _make_handler(tmp, ws, plugins, llm)
        msg = [_at(111), _txt(" 某个关键词")]
        try:
            for i, (bot_id, qq) in enumerate(
                (("bot_001", 111), ("bot_002", 222))
            ):
                event = _event(msg, self_id=qq, mid=500 + i)
                await handler.handle_event(bot_id, event, {
                    "time": 1700000000, "self_id": qq, "post_type": "message",
                    "message_type": "group", "message_id": 500 + i,
                    "group_id": GROUP_ID, "user_id": 9999, "message": msg,
                })
            # 两只都跑观察钩子(活跃记录等副作用不能丢), 但只有被 @ 的能消费
            assert sorted(plugins.observed) == ["bot_001", "bot_002"], (
                f"观察钩子应对所有 bot 运行以保留副作用, 实际: {plugins.observed}"
            )
            assert all(bot == "bot_001" for bot, *_ in ws.sent), (
                f"未被 @ 的 bot 不得发送任何消息: {ws.sent}"
            )
        finally:
            await handler.close()
        print("[7] @ A → B 的观察钩子运行但不消费 OK")


async def test_at_bot_bypasses_merged_reply():
    """@ 具体 bot 时不合并转发, 由被 @ 的 bot 各自正常回复。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bm = _make_bot_manager(tmp)
        ws, plugins, llm = _WS(bm), _Plugins(), _LLM()
        handler = _make_handler(tmp, ws, plugins, llm)
        msg = [_at(111), _txt(" 赞我")]
        try:
            for i, (bot_id, qq) in enumerate(
                (("bot_001", 111), ("bot_002", 222))
            ):
                event = _event(msg, self_id=qq, mid=600 + i)
                await handler.handle_event(bot_id, event, {
                    "time": 1700000000, "self_id": qq, "post_type": "message",
                    "message_type": "group", "message_id": 600 + i,
                    "group_id": GROUP_ID, "user_id": 9999, "message": msg,
                })
            assert ws.forwards == [], f"@ 定向不应产生合并转发: {ws.forwards}"
            # 观察钩子对两只都运行(副作用), 但只有被 @ 的继续往下走
            assert sorted(plugins.observed) == ["bot_001", "bot_002"]
        finally:
            await handler.close()
        print("[8] @ 定向绕过合并回复 OK")


async def test_mention_unknown_qq_does_not_restrict():
    """@ 了非 bot 的 QQ 时, 不视为定向: 仍按原有抽签语义处理。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bm = _make_bot_manager(tmp)
        bm.pick_bot_for_group = lambda gid, mid=None: "bot_001"  # 固定抽签
        ws, plugins, llm = _WS(bm), _Plugins(), _LLM()
        handler = _make_handler(tmp, ws, plugins, llm)
        msg = [_at(88888), _txt(" /help")]
        try:
            for i, (bot_id, qq) in enumerate(
                (("bot_001", 111), ("bot_002", 222))
            ):
                event = _event(msg, self_id=qq, mid=700 + i)
                await handler.handle_event(bot_id, event, {
                    "time": 1700000000, "self_id": qq, "post_type": "message",
                    "message_type": "group", "message_id": 700 + i,
                    "group_id": GROUP_ID, "user_id": 9999, "message": msg,
                })
            # @ 的不是任何 bot → 不作定向限制, 走抽签(此处固定为 bot_001)
            assert plugins.intercepted == ["bot_001"], (
                f"@ 非 bot 应按抽签处理, 实际: {plugins.intercepted}"
            )
        finally:
            await handler.close()
        print("[9] @ 非 bot QQ 不触发定向 OK")


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            if asyncio.iscoroutinefunction(_fn):
                asyncio.run(_fn())
            else:
                _fn()
            print(f"PASS {_name}")
        except Exception as e:  # noqa: BLE001
            _failed += 1
            print(f"FAIL {_name}: {e}")
            import traceback
            traceback.print_exc()
    print("---")
    assert _failed == 0, f"{_failed} 个测试失败"
    print("ALL MENTION-DIRECTED TESTS PASSED")
