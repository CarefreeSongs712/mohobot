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


def test_incremental_read_after_multibyte_append():
    """含中文的文件追加后增量读取: offset 必须是字节偏移。

    生产事故: 字符下标当 offset 用, 中文文件追加后 seek 到 UTF-8 字符中间,
    UnicodeDecodeError 让会话列表/bootstrap 全部 500, AI 审核整轮空跑。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        _write_private(hist, [_user_msg(1, "第一条中文消息"), _bot_msg(101, "中文回复")])
        data = MohobotData(tmp_path)
        sk = session_key("bot_001", "private", "10001")
        data.list_sessions(force=True)

        for i, text in enumerate(["追加的第二条", "第三条：表情😀与标点"], start=2):
            with open(hist, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(_user_msg(i, text), ensure_ascii=False) + "\n")
            data.list_sessions(force=True)

        idx = data._file_cache[str(hist)]
        assert idx.offset == hist.stat().st_size, (idx.offset, hist.stat().st_size)
        texts = [r["text"] for r in data.load_entries(sk) if r["kind"] == "user"]
        assert texts == ["第一条中文消息", "追加的第二条", "第三条：表情😀与标点"], texts


def test_partial_line_split_inside_multibyte_char():
    """半行恰好断在多字节字符中间: 不得报错, 补齐后整行解析。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        hist.parent.mkdir(parents=True, exist_ok=True)
        full = (json.dumps(_user_msg(1, "完整一行"), ensure_ascii=False) + "\n").encode("utf-8")
        partial = (json.dumps(_user_msg(2, "断在汉字中间"), ensure_ascii=False) + "\n").encode("utf-8")
        cut = partial.index("汉".encode("utf-8")) + 1  # 汉字 3 字节中的第 1 个之后
        hist.write_bytes(full + partial[:cut])

        data = MohobotData(tmp_path)
        sk = session_key("bot_001", "private", "10001")
        data.list_sessions(force=True)
        assert [r["text"] for r in data.load_entries(sk) if r["kind"] == "user"] == ["完整一行"]

        with open(hist, "ab") as fh:
            fh.write(partial[cut:])
        texts = [r["text"] for r in data.load_entries(sk) if r["kind"] == "user"]
        assert texts == ["完整一行", "断在汉字中间"], texts


def test_stale_char_offset_cache_is_discarded():
    """v7 sidecar 里的字符偏移必须作废, 不能拿来 seek。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        _write_private(hist, [_user_msg(1, "缓存里的中文")])
        cache = tmp_path / "loader_cache.json"
        st = hist.stat()
        char_offset = len(hist.read_text(encoding="utf-8"))
        assert char_offset != st.st_size
        cache.write_text(json.dumps({"version": 7, "files": {
            "history/bot_001/private/10001.jsonl": {
                "mtime": 0, "size": 0, "offset": char_offset, "inode": int(st.st_ino),
                "rows": [], "bot_mids": [],
            },
        }}), encoding="utf-8")
        with open(hist, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_user_msg(2, "重启后追加"), ensure_ascii=False) + "\n")

        data = MohobotData(tmp_path, cache_path=cache)
        data.list_sessions(force=True)
        sk = session_key("bot_001", "private", "10001")
        texts = [r["text"] for r in data.load_entries(sk) if r["kind"] == "user"]
        data.flush_cache()  # 等后台 sidecar 保存线程结束, 否则 tempdir 清理时目录非空
        assert texts == ["缓存里的中文", "重启后追加"], texts


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


# ── issue #133: 非追加更新 / 合并会话缓存 / 删除重建 ─────────────


def test_same_inode_equal_length_rewrite_with_restored_mtime_is_detected():
    """同 inode、等长、恢复 mtime 的就地重写也必须被发现并重读(#133)。

    Windows 的 ChangeTime 粒度约 15.6ms, 写入前先跨过一个 tick(审查 N1)。
    """
    import os
    import time

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        _write_private(hist, [_user_msg(1, "原始内容AAAA")])
        data = MohobotData(tmp_path)
        sk = session_key("bot_001", "private", "10001")
        assert [r["text"] for r in data.load_entries(sk) if r["kind"] == "user"] == ["原始内容AAAA"]

        st = hist.stat()
        old_bytes = hist.read_bytes()
        new_bytes = old_bytes.replace("原始内容AAAA".encode("utf-8"), "篡改内容BBBB".encode("utf-8"))
        assert len(new_bytes) == len(old_bytes) and new_bytes != old_bytes
        time.sleep(0.05)  # > NTFS ChangeTime tick, 保证 change_ns 推进
        hist.write_bytes(new_bytes)  # 同 inode 就地等长重写
        os.utime(hist, (st.st_atime, st.st_mtime))  # 恢复 float mtime

        texts = [r["text"] for r in data.load_entries(sk) if r["kind"] == "user"]
        assert "篡改内容BBBB" in texts, f"就地等长重写必须重读, 实际: {texts}"
        assert "原始内容AAAA" not in texts, f"旧内容不应残留: {texts}"


def test_in_place_rewrite_detected_with_exact_ns_mtime_restore():
    """mtime_ns 精确恢复时, 靠 NTFS ChangeTime(Windows)/ctime(POSIX)发现改写。

    Windows 上 st_ctime 是创建时间, 唯一元数据信号是 NTFS ChangeTime, 而它
    只有约 15.6ms 的时钟粒度; 若 load→write_bytes→utime 全落在同一 tick,
    change_ns 与缓存值相等, 中段等长改写又不在 64B 头/尾锚点范围内, 会复用
    旧 rows(审查 N1)。因此写入前先跨过一个 tick, 使 change_ns 必然推进。
    """
    import os
    import time

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        _write_private(hist, [_user_msg(1, "开头原文CCCC"), _user_msg(2, "结尾原文DDDD")])
        data = MohobotData(tmp_path)
        sk = session_key("bot_001", "private", "10001")
        assert len(data.load_entries(sk)) == 2

        st = hist.stat()
        old_bytes = hist.read_bytes()
        new_bytes = old_bytes.replace(
            "开头原文CCCC".encode("utf-8"), "开头篡改CCCC".encode("utf-8"),
        )
        assert len(new_bytes) == len(old_bytes)
        time.sleep(0.05)  # > NTFS ChangeTime tick(~15.6ms), 保证 change_ns 推进
        hist.write_bytes(new_bytes)
        os.utime(hist, ns=(st.st_atime_ns, st.st_mtime_ns))  # 精确恢复 ns

        texts = [r["text"] for r in data.load_entries(sk)]
        assert "开头篡改CCCC" in texts, f"内容锚点未生效, 实际: {texts}"
        assert "开头原文CCCC" not in texts


def test_merged_group_cache_invalidated_on_equal_length_rewrite():
    """群合并行的派生缓存(_merged_cache)同样必须按文件身份失效(#133)。"""
    import os
    import time

    def group_bot(mid: int, text: str, ts: int) -> dict:
        return {
            "post_type": "message_sent", "message_type": "group", "time": ts,
            "self_id": 111, "group_id": 20002, "message_id": f"GB-{mid}",
            "bot_id": "bot_001", "sender": {"user_id": 111, "nickname": "bot"},
            "message": [{"type": "text", "data": {"text": text}}],
        }

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        grp = tmp_path / "history" / "group" / "20002.jsonl"
        _write_private(grp, [group_bot(1, "群原文AAAA", 1700000100)])
        data = MohobotData(tmp_path)
        sk = session_key("_merged", "group", "20002")
        assert [r["text"] for r in data.load_entries(sk)] == ["群原文AAAA"]

        st = grp.stat()
        old_bytes = grp.read_bytes()
        new_bytes = old_bytes.replace("群原文AAAA".encode("utf-8"), "群篡改BBBB".encode("utf-8"))
        assert len(new_bytes) == len(old_bytes)
        time.sleep(0.05)  # > NTFS ChangeTime tick, 保证 change_ns 推进
        grp.write_bytes(new_bytes)
        os.utime(grp, ns=(st.st_atime_ns, st.st_mtime_ns))

        texts = [r["text"] for r in data.load_entries(sk)]
        assert "群篡改BBBB" in texts, f"合并缓存未失效, 实际: {texts}"
        assert "群原文AAAA" not in texts


def test_deleted_then_recreated_history_is_reparsed():
    """观察到删除即撤销旧索引; 重建是否复用 inode/时钟都不影响结果。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        _write_private(hist, [_user_msg(1, "删除前内容")])
        data = MohobotData(tmp_path)
        sk = session_key("bot_001", "private", "10001")
        assert [r["text"] for r in data.load_entries(sk)] == ["删除前内容"]
        old_index = data._file_cache[str(hist)]
        assert data.unreviewed_counts({}, revision=1)[sk] == 1

        hist.unlink()
        # 不存在 → load_entries 返回 None(兼容入口语义); 无论如何不得复用旧行
        assert data.load_entries(sk) is None, "删除后不应再返回旧行"
        assert str(hist) not in data._file_cache
        assert data._pending_cache is None

        _write_private(hist, [_user_msg(2, "重建后内容")])
        texts = [r["text"] for r in data.load_entries(sk) or []]
        assert texts == ["重建后内容"], texts
        assert data._file_cache[str(hist)] is not old_index


def test_append_after_anchor_guard_stays_incremental_and_visible():
    """内容锚点/身份复核不得破坏正常追加的增量可见性。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
        _write_private(hist, [_user_msg(1, "第一行")])
        data = MohobotData(tmp_path)
        sk = session_key("bot_001", "private", "10001")
        data.load_entries(sk)

        with hist.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_user_msg(2, "第二行"), ensure_ascii=False) + "\n")
        texts = [r["text"] for r in data.load_entries(sk)]
        assert texts == ["第一行", "第二行"], texts


def test_anchor_verification_cached_within_identity():
    """身份未变时复用锚点复核结论, 不每次请求都打开文件 seek(审查 N4)。

    只替换 _anchors_match 记录调用次数, 其它逻辑不动; try/finally 还原。
    """
    calls: list[str] = []
    real = MohobotData._anchors_match

    def counting(path, idx):
        calls.append(str(path))
        return real(path, idx)

    MohobotData._anchors_match = staticmethod(counting)
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            hist = tmp_path / "history" / "bot_001" / "private" / "10001.jsonl"
            _write_private(hist, [_user_msg(1, "锚点缓存")])
            data = MohobotData(tmp_path)
            sk = session_key("bot_001", "private", "10001")
            assert len(data.load_entries(sk)) == 1
            calls.clear()
            # 文件身份完全一致 → 复用上次复核结论, 不再打开文件比对锚点
            assert len(data.load_entries(sk)) == 1
            assert not calls, f"同身份重复读取不应再复核锚点: {calls}"
            # 追加后身份变化 → 正常增量解析, 新行可见
            with hist.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_user_msg(2, "追加"), ensure_ascii=False) + "\n")
            texts = [r["text"] for r in data.load_entries(sk)]
            assert texts == ["锚点缓存", "追加"], texts
    finally:
        MohobotData._anchors_match = staticmethod(real)
