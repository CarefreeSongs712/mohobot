"""history 群聊合并布局测试 — 合并存储 / 写入去重 / bot_id 标注 / 双写 / 迁移脚本。

覆盖:
1. MessageHandler 群消息 → history/group/{群号}.jsonl(行内 bot_id), 私聊不变
2. 多 bot 收到同一条群消息(同 mid) → 合并文件只写一份
3. history_dual_write 开/关 → 旧布局 {bot_id}/group/ 是否仍有归档
4. WSServer bot 发言 → 群写合并文件 + bot_id, 私聊按 bot 分目录
5. 迁移脚本: 旧 per-bot 群文件去重合并 + bot_id 补全 + 时间排序 + 备份移动

运行: python tests/_run_all.py (从仓库根) / python tests/test_history_merged.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _group_raw(mid: int, group_id: int = 20002, user_id: int = 10001,
               text: str = "你好", self_id: int = 111) -> dict:
    return {
        "time": 1700000000 + mid, "self_id": self_id, "post_type": "message",
        "message_type": "group", "sub_type": "normal", "message_id": mid,
        "group_id": group_id, "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text, "font": 0,
        "sender": {"user_id": user_id, "nickname": "张三", "card": "张三"},
    }


def _private_raw(mid: int, user_id: int = 10001, self_id: int = 111) -> dict:
    return {
        "time": 1700000000 + mid, "self_id": self_id, "post_type": "message",
        "message_type": "private", "sub_type": "friend", "message_id": mid,
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": "私聊"}}],
        "raw_message": "私聊", "font": 0,
        "sender": {"user_id": user_id, "nickname": "张三"},
    }


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _make_handler(tmp: Path, dual_write: bool = True):
    """最小 MessageHandler: 只绑定 _archive_event 用到的属性。"""
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import GlobalConfig

    mh = MessageHandler.__new__(MessageHandler)
    mh._data_dir = str(tmp)
    cfg = GlobalConfig()
    cfg.history_dual_write = dual_write
    mh._global_config = cfg
    mh._writer_registry = {}
    mh._group_mid_window = {}
    return mh


async def _close_writers(mh) -> None:
    """关闭归档 writer(Windows 下不关会锁住临时目录, 清理失败)。"""
    for writer in mh._writer_registry.values():
        await writer.close()
    mh._writer_registry.clear()


def _group_event(raw: dict):
    from mohobot.models.onebot import GroupMessageEvent
    return GroupMessageEvent.from_dict(raw)


def _private_event(raw: dict):
    from mohobot.models.onebot import PrivateMessageEvent
    return PrivateMessageEvent.from_dict(raw)


async def test_group_merged_archive_and_dedup():
    """群消息写合并文件(带 bot_id); 多 bot 同 mid 只写一份; 私聊不变。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp)

        await mh._archive_event("bot_001", _group_event(_group_raw(1)), _group_raw(1))
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert len(merged) == 1
        assert merged[0]["bot_id"] == "bot_001"
        assert merged[0]["message"][0]["data"]["text"] == "你好"
        # 未双写关闭时, 旧布局也有一份
        legacy = _read_jsonl(tmp / "history" / "bot_001" / "group" / "20002.jsonl")
        assert len(legacy) == 1 and "bot_id" not in legacy[0]

        # 另一只 bot 收到同一条消息(同 mid, 不同 self_id) → 合并文件不重复
        raw2 = _group_raw(1, self_id=222)
        await mh._archive_event("bot_002", _group_event(raw2), raw2)
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert len(merged) == 1, "同 mid 的群消息不得重复归档"
        # 但旧布局各自归档(回滚保险, 不去重)
        assert len(_read_jsonl(tmp / "history" / "bot_002" / "group" / "20002.jsonl")) == 1

        # 不同 mid → 正常写入
        await mh._archive_event("bot_001", _group_event(_group_raw(2)), _group_raw(2))
        assert len(_read_jsonl(tmp / "history" / "group" / "20002.jsonl")) == 2

        # 私聊仍按 bot 分目录
        praw = _private_raw(3)
        await mh._archive_event("bot_001", _private_event(praw), praw)
        assert len(_read_jsonl(tmp / "history" / "bot_001" / "private" / "10001.jsonl")) == 1
        assert not (tmp / "history" / "private").exists()
        await _close_writers(mh)
        print("[1] 群消息合并归档 + 同 mid 去重 + 私聊不变 OK")


async def test_dual_write_off():
    """history_dual_write=false → 只写合并文件, 不写旧布局。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp, dual_write=False)
        await mh._archive_event("bot_001", _group_event(_group_raw(1)), _group_raw(1))
        assert len(_read_jsonl(tmp / "history" / "group" / "20002.jsonl")) == 1
        assert not (tmp / "history" / "bot_001" / "group").exists()
        await _close_writers(mh)
        print("[2] dual_write 关闭 → 旧布局不写入 OK")


async def test_dedup_window_eviction():
    """去重窗口容量有限: 超出窗口的 mid 再次出现会再写一次(近期窗口语义)。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp, dual_write=False)
        mh._GROUP_MID_WINDOW = 2  # 缩小窗口便于测试
        for mid in (1, 2, 3):
            await mh._archive_event("bot_001", _group_event(_group_raw(mid)), _group_raw(mid))
        assert len(_read_jsonl(tmp / "history" / "group" / "20002.jsonl")) == 3
        # mid=1 已被逐出窗口 → 重复出现会再写
        await mh._archive_event("bot_001", _group_event(_group_raw(1)), _group_raw(1))
        assert len(_read_jsonl(tmp / "history" / "group" / "20002.jsonl")) == 4
        # mid=3 仍在窗口内 → 不重复
        await mh._archive_event("bot_001", _group_event(_group_raw(3)), _group_raw(3))
        assert len(_read_jsonl(tmp / "history" / "group" / "20002.jsonl")) == 4
        await _close_writers(mh)
        print("[3] 去重窗口逐出语义 OK")


def _make_ws(tmp: Path, dual_write: bool = True):
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.models.config import BotConfig
    from mohobot.ws_server import WSServer

    class _Sock:
        async def send(self, payload: str) -> None:
            pass

    bm = BotManager(data_dir=str(tmp))
    inst = BotInstance("bot_001", _Sock(),
                       BotConfig(bot_id="bot_001", qq=111, nickname="天依"))
    bm._bots["bot_001"] = inst
    cfg = BotConfig and type("C", (), {"history_dual_write": dual_write})()
    ws = WSServer(bot_manager=bm, data_dir=str(tmp), global_config=cfg)
    ws._ARCHIVE_ECHO_TIMEOUT = 0.05
    return ws


async def test_ws_sent_archive_merged():
    """WSServer bot 发言: 群写合并文件(带 bot_id + 双写旧布局), 私聊按 bot 分目录。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws = _make_ws(tmp)
        try:
            await ws._write_archive_line("bot_001", "group", 20002, "555",
                                         [{"type": "text", "data": {"text": "回复"}}])
            merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
            assert len(merged) == 1
            assert merged[0]["post_type"] == "message_sent"
            assert merged[0]["bot_id"] == "bot_001"
            assert merged[0]["message_id"] == "555"
            legacy = _read_jsonl(tmp / "history" / "bot_001" / "group" / "20002.jsonl")
            assert len(legacy) == 1 and legacy[0]["bot_id"] == "bot_001"

            await ws._write_archive_line("bot_001", "private", 10001, "556",
                                         [{"type": "text", "data": {"text": "私回"}}])
            priv = _read_jsonl(tmp / "history" / "bot_001" / "private" / "10001.jsonl")
            assert len(priv) == 1 and priv[0]["bot_id"] == "bot_001"
            assert not (tmp / "history" / "private").exists()
            print("[4] WSServer bot 发言归档路径 OK")
        finally:
            await ws.stop()


def test_migrate_script():
    """迁移脚本: 多 bot 旧群文件去重合并、bot_id 补全、时间排序、旧文件移入备份。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data = tmp / "data"
        gid = 20002
        # bot_001 归档: 3 条(1 条与 bot_002 重复)
        rows_a = [
            {"time": 100, "post_type": "message", "message_id": 1,
             "user_id": 11, "message": [{"type": "text", "data": {"text": "甲"}}]},
            {"time": 200, "post_type": "message", "message_id": 2,
             "user_id": 11, "message": [{"type": "text", "data": {"text": "乙"}}]},
            {"time": 400, "post_type": "message", "message_id": 3,
             "user_id": 11, "message": [{"type": "text", "data": {"text": "重复"}}]},
        ]
        # bot_002 归档: 2 条(1 条与 bot_001 重复, 1 条独有)
        rows_b = [
            {"time": 400, "post_type": "message", "message_id": 3,
             "user_id": 11, "message": [{"type": "text", "data": {"text": "重复"}}]},
            {"time": 300, "post_type": "message_sent", "message_id": 9,
             "user_id": 222, "message": [{"type": "text", "data": {"text": "bot 发言"}}]},
        ]
        for bot, rows in (("bot_001", rows_a), ("bot_002", rows_b)):
            p = data / "history" / bot / "group" / f"{gid}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                         encoding="utf-8")
        # 私聊不动
        priv = data / "history" / "bot_001" / "private" / "10001.jsonl"
        priv.parent.mkdir(parents=True, exist_ok=True)
        priv.write_text('{"post_type": "message"}\n', encoding="utf-8")

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_history_layout",
            Path(__file__).resolve().parent.parent / "scripts" / "migrate_history_layout.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 用参数调用 main 的逻辑: 直接以 argv 方式跑
        sys_argv = sys.argv
        try:
            sys.argv = ["migrate", "--data-dir", str(data)]
            rc = mod.main()
        finally:
            sys.argv = sys_argv
        assert rc == 0

        target = data / "history" / "group" / f"{gid}.jsonl"
        merged = _read_jsonl(target)
        assert len(merged) == 4, f"重复 mid=3 应去重: {len(merged)}"
        assert [r["time"] for r in merged] == [100, 200, 300, 400], "应按时间排序"
        bots = {r["time"]: r["bot_id"] for r in merged}
        assert bots[100] == "bot_001" and bots[300] == "bot_002"
        assert bots[400] == "bot_001", "重复行保留先见者(bot_001)"
        # 旧文件移入备份, 私聊不动, group/ 空目录被清掉
        assert not (data / "history" / "bot_001" / "group").exists()
        assert not (data / "history" / "bot_002" / "group").exists()
        assert priv.exists()
        backups = list(data.glob("history_legacy_*"))
        assert len(backups) == 1
        assert (backups[0] / "bot_001" / "group" / f"{gid}.jsonl").exists()

        # 幂等: 再跑一次无事可做
        before = target.read_text(encoding="utf-8")
        sys.argv = ["migrate", "--data-dir", str(data)]
        try:
            assert mod.main() == 0
        finally:
            sys.argv = sys_argv
        assert target.read_text(encoding="utf-8") == before
        print("[5] 迁移脚本去重合并 + 备份 + 幂等 OK")


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
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
    print("ALL HISTORY MERGED TESTS PASSED")
