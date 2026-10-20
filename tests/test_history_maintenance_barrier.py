"""Issue #8 回归: 历史热维护屏障 + review 面板 auto 消息 ID 登记。

1. 热清理/恢复 history 时, 活跃 JSONLWriter 仍指向已 unlink 的旧 inode,
   继续写入会静默丢失。屏障在维护窗口冻结写入并关闭 writer。
2. review 面板把 ``source=auto`` 的 bot 消息在登记 message_id **之前** 就丢弃,
   导致用户引用这类消息提问时被当成用户消息漏审。修复后先登记 ID 再按
   source 决定是否入审。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.services.history_barrier import HistoryBarrier  # noqa: E402


# ── 1. 屏障核心语义 ─────────────────────────────────────────

async def test_barrier_freeze_thaw_and_closers():
    barrier = HistoryBarrier()
    calls: list[str] = []

    async def closer():
        calls.append("closed")

    barrier.register_closer(closer)
    assert barrier.frozen is False

    await barrier.freeze()
    assert barrier.frozen is True
    assert calls == ["closed"], calls

    # 重复 freeze 不重复关闭
    await barrier.freeze()
    assert calls == ["closed"], calls

    await barrier.thaw()
    assert barrier.frozen is False

    # 上下文管理器形式
    async with barrier:
        assert barrier.frozen is True
    assert barrier.frozen is False


async def test_barrier_closer_failure_does_not_block():
    """某个组件关闭失败不得阻止其它组件关闭或冻结流程。"""
    barrier = HistoryBarrier()
    ok: list[str] = []

    async def bad():
        raise RuntimeError("boom")

    async def good():
        ok.append("good")

    barrier.register_closer(bad)
    barrier.register_closer(good)
    await barrier.freeze()
    assert barrier.frozen is True
    assert ok == ["good"]
    await barrier.thaw()


async def test_barrier_registered_once():
    barrier = HistoryBarrier()
    calls: list[int] = []

    async def closer():
        calls.append(1)

    barrier.register_closer(closer)
    barrier.register_closer(closer)  # 重复注册去重
    await barrier.freeze()
    assert len(calls) == 1


# ── 2. MessageHandler 接入 ───────────────────────────────────

def _archive_handler(tmp: Path):
    from mohobot.message_handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._data_dir = str(tmp)
    handler._writer_registry = {}
    handler._global_config = None
    handler._history_barrier = None
    return handler


def _private_event(user_id: int):
    from mohobot.models.onebot import PrivateMessageEvent

    return PrivateMessageEvent.from_dict({
        "time": 1700000000, "self_id": 111, "post_type": "message",
        "message_type": "private", "sub_type": "friend", "message_id": 1,
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "raw_message": "hi", "font": 0,
        "sender": {"user_id": user_id, "nickname": "u"},
    })


async def test_handler_skips_archive_when_frozen():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        handler = _archive_handler(tmp)
        barrier = HistoryBarrier()
        handler.set_history_barrier(barrier)

        raw = {"post_type": "message", "user_id": 10001}
        await handler._archive_event("bot_001", _private_event(10001), raw)
        path = tmp / "history" / "bot_001" / "private" / "10001.jsonl"
        assert path.exists() and path.read_text(encoding="utf-8").strip(), "正常应写入"

        # 冻结后不再写入
        before = path.read_text(encoding="utf-8")
        async with barrier:
            await handler._archive_event("bot_001", _private_event(10001), raw)
        assert path.read_text(encoding="utf-8") == before, "冻结期间不得写入"

        # 解冻后恢复写入
        await handler._archive_event("bot_001", _private_event(10001), raw)
        assert path.read_text(encoding="utf-8") != before


async def test_handler_freeze_closes_writers():
    """freeze 必须关掉 writer 句柄, 否则恢复后仍写旧 inode。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        handler = _archive_handler(tmp)
        barrier = HistoryBarrier()
        handler.set_history_barrier(barrier)

        raw = {"post_type": "message", "user_id": 10001}
        await handler._archive_event("bot_001", _private_event(10001), raw)
        assert handler._writer_registry, "应创建 writer"

        await barrier.freeze()
        assert handler._writer_registry == {}, "冻结后 writer 缓存必须清空"

        # 解冻后重新写入会新建 writer 并指向当前路径
        await barrier.thaw()
        await handler._archive_event("bot_001", _private_event(10001), raw)
        assert handler._writer_registry, "解冻后应重建 writer"


# ── 3. WSServer 接入 ────────────────────────────────────────

async def test_ws_server_skips_archive_when_frozen():
    from mohobot.ws_server import WSServer

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws = object.__new__(WSServer)
        ws._data_dir = str(tmp)
        ws._archive_writers = {}
        ws._history_barrier = None
        barrier = HistoryBarrier()
        ws.set_history_barrier(barrier)

        class _Bm:
            @staticmethod
            def get(bot_id):
                return None

        ws._bot_manager = _Bm()
        ws._global_config = None

        await ws._write_archive_line("bot_001", "group", 20002, "mid-1", "hi", "llm")
        path = tmp / "history" / "group" / "20002.jsonl"
        assert path.exists(), "正常应写 bot 发言归档"

        before = path.read_text(encoding="utf-8")
        async with barrier:
            await ws._write_archive_line("bot_001", "group", 20002, "mid-2", "hi2", "llm")
        assert path.read_text(encoding="utf-8") == before, "冻结期间不得写入"
# ── 4. review loader: auto 消息 ID 登记 ─────────────────────

def _loader_for(td: str):
    from review.loader import MohobotData

    return MohobotData(td)


def test_parse_line_registers_auto_message_id():
    """私聊/旧布局路径: auto 消息不入审, 但 ID 必须登记。"""
    import json

    data = _loader_for("/tmp/unused-barrier-test")
    bot_mids: set[str] = set()
    line = json.dumps({
        "post_type": "message_sent", "source": "auto", "message_id": "auto-1",
        "bot_id": "bot_001", "self_id": 111, "time": 1700000000,
        "message": [{"type": "text", "data": {"text": "/status"}}],
    }, ensure_ascii=False)
    item = data._parse_line(line, "private", "111", bot_mids, "bot_001")
    assert item is None, "auto 内容本身不入审"
    assert "auto-1" in bot_mids, "但 message_id 必须登记(否则引用识别漏审)"


def test_parse_merged_line_registers_auto_message_id():
    """群聊合并布局路径: 同样先登记 ID 再过滤。"""
    import json

    from review.loader import _FileIndex

    data = _loader_for("/tmp/unused-barrier-test")
    idx = _FileIndex()
    line = json.dumps({
        "post_type": "message_sent", "source": "auto", "message_id": "auto-2",
        "bot_id": "bot_001", "self_id": 111, "time": 1700000000, "group_id": 20002,
        "message": [{"type": "text", "data": {"text": "欢迎加入"}}],
    }, ensure_ascii=False)
    item = data._parse_merged_line(line, idx)
    assert item is None, "auto 内容不入审"
    assert "auto-2" in idx.bot_mids, "但群合并路径也必须登记 message_id"
    assert "bot_001" in idx.bots, "bot 身份也要登记"


def test_quoted_auto_message_is_attributed_to_bot():
    """用户引用一条 auto 消息时, 该引用应被识别为 bot 发言(不再漏审)。"""
    import json

    data = _loader_for("/tmp/unused-barrier-test")
    bot_mids: set[str] = set()
    auto_line = json.dumps({
        "post_type": "message_sent", "source": "auto", "message_id": "auto-3",
        "bot_id": "bot_001", "self_id": 111, "time": 1700000000,
        "message": [{"type": "text", "data": {"text": "/status"}}],
    }, ensure_ascii=False)
    data._parse_line(auto_line, "private", "111", bot_mids, "bot_001")

    user_line = json.dumps({
        "post_type": "message", "message_type": "group", "message_id": 9001,
        "group_id": 20002, "user_id": 10001, "self_id": 111, "time": 1700000001,
        "message": [
            {"type": "reply", "data": {"id": "auto-3"}},
            {"type": "text", "data": {"text": "这个状态是什么意思"}},
        ],
        "sender": {"user_id": 10001, "nickname": "小明"},
    }, ensure_ascii=False)
    item = data._parse_line(user_line, "group", "111", bot_mids, "bot_001")
    assert item is not None, "引用 bot 消息的提问必须入审, 不能因 auto 未登记而被丢弃"
    assert item["kind"] == "user"
