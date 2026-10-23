"""Issue #8 剩余项回归: 缓存身份、半行 offset、待审计数求交、前端会话竞态。

注意: 根 runner 以无参方式调用测试函数, 这里统一用模块内 tempdir,
不依赖 pytest 的 tmp_path fixture。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review.loader import MohobotData, session_key  # noqa: E402


def _write_private(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
        encoding="utf-8",
    )


def _user_msg(mid: int, text: str, ts: int = 1700000000, uid: int = 10001) -> dict:
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "message_id": mid, "user_id": uid, "self_id": 111, "time": ts,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": uid, "nickname": "小明"},
    }


def _bot_msg(mid: int, text: str, ts: int = 1700000000) -> dict:
    return {
        "post_type": "message_sent", "message_type": "private", "source": "llm",
        "message_id": mid, "self_id": 111, "time": ts, "bot_id": "bot_001",
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": 111, "nickname": "bot"},
    }


def test_equal_length_replacement_is_detected():
    """等长替换(内容变了但 size 相同)必须被 inode 检测出来并重读。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        _write_private(hist, [_user_msg(1, "原文AAAA")])
        data = MohobotData(tmp_path)

        data.list_sessions(force=True)
        rows = data.load_entries(session_key("bot_001", "private", "10001"))
        assert [r["text"] for r in rows if r["kind"] == "user"] == ["原文AAAA"]

        # 原子替换成"等长但不同内容"的文件(旧实现会因 size 相同而复用旧解析)
        tmp_new = hist.with_suffix(".new")
        _write_private(tmp_new, [_user_msg(2, "替换BBBB")])
        tmp_new.replace(hist)

        rows2 = data.load_entries(session_key("bot_001", "private", "10001"))
        texts = [r["text"] for r in rows2 if r["kind"] == "user"]
        assert "替换BBBB" in texts, f"等长替换后必须重读, 实际: {texts}"
        assert "原文AAAA" not in texts, f"旧内容不应残留: {texts}"


def test_partial_line_not_consumed_until_complete():
    """写入半行时不得推进 offset; 补齐后该行必须被解析。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        hist.parent.mkdir(parents=True, exist_ok=True)
        full = json.dumps(_user_msg(1, "完整一行"), ensure_ascii=False)
        partial = json.dumps(_user_msg(2, "半行消息"), ensure_ascii=False)
        hist.write_text(full + "\n" + partial[: len(partial) // 2], encoding="utf-8")

        data = MohobotData(tmp_path)
        data.list_sessions(force=True)
        rows1 = data.load_entries(session_key("bot_001", "private", "10001"))
        assert [r["text"] for r in rows1 if r["kind"] == "user"] == ["完整一行"], rows1

        with open(hist, "a", encoding="utf-8") as fh:
            fh.write(partial[len(partial) // 2:] + "\n")

        rows2 = data.load_entries(session_key("bot_001", "private", "10001"))
        texts = [r["text"] for r in rows2 if r["kind"] == "user"]
        assert texts == ["完整一行", "半行消息"], f"补齐后应解析出半行消息, 实际: {texts}"


def test_unreviewed_counts_uses_current_messages():
    """待审数必须基于当前文件消息, 而不是 total 相减(恢复短备份场景)。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        _write_private(hist, [
            _user_msg(1, "第一条"), _bot_msg(101, "回复一"),
            _user_msg(2, "第二条"), _bot_msg(102, "回复二"),
        ])
        data = MohobotData(tmp_path)
        sk = session_key("bot_001", "private", "10001")
        data.list_sessions(force=True)
        entries = data.enrich_entries(sk, {}, {})
        assert entries, "应有条目"

        # DB 里记录了"当前文件里不存在"的旧判定(恢复短备份后常见)
        statuses = {sk: {
            "stale-fingerprint-a": {"status": "normal"},
            "stale-fingerprint-b": {"status": "normal"},
        }}
        counts = data.unreviewed_counts(statuses)
        assert counts.get(sk) == len(entries), (
            f"当前 {len(entries)} 条都未判定 → 待审应为 {len(entries)}, 实际 {counts.get(sk)}"
        )

        statuses[sk][entries[0]["fingerprint"]] = {"status": "normal"}
        counts2 = data.unreviewed_counts(statuses)
        assert counts2.get(sk) == len(entries) - 1, counts2


def test_unreviewed_counts_never_negative():
    """极端场景: DB 判定数远大于当前文件消息数时, 待审数不得为负。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        _write_private(hist, [_user_msg(1, "唯一一条")])
        data = MohobotData(tmp_path)
        sk = session_key("bot_001", "private", "10001")
        data.list_sessions(force=True)
        statuses = {sk: {f"ghost-{i}": {"status": "normal"} for i in range(50)}}
        counts = data.unreviewed_counts(statuses)
        assert counts.get(sk) == 1, counts
        assert all(v >= 0 for v in counts.values())


def test_frontend_session_generation_guard():
    """前端必须有会话加载代际守卫, 防止旧回包覆盖当前会话。"""
    html = (
        Path(__file__).resolve().parent.parent / "review" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "sessionLoadGen" in html, "缺少会话加载代际计数"
    assert "gen !== sessionLoadGen" in html, "缺少过期回包丢弃判断"


if __name__ == "__main__":
    failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {_name}: {e}")
                import traceback
                traceback.print_exc()
    print("---")
    assert failed == 0, f"{failed} failed"
    print("ALL REVIEW CACHE IDENTITY TESTS PASSED")
