from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.file_store import _TAIL_READ_CHUNK, jsonl_read_tail
from review.loader import MohobotData, session_key
from review.store import ReviewStore


def _message(i: int, group: bool = False) -> dict:
    return {
        "post_type": "message_sent", "source": "llm", "self_id": 111,
        "message_type": "group" if group else "private", "group_id": 20002,
        "message_id": f"A{i:04}", "bot_id": "bot_001", "time": 1700000000 + i,
        "message": [{"type": "text", "data": {"text": f"OLD{i:04}" + "中" * 450}}],
        "sender": {"user_id": 111, "nickname": "bot"},
    }


def _encoded(message: dict) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def _archive(root: Path, group: bool = False) -> tuple[Path, str]:
    if group:
        path = root / "history" / "group" / "20002.jsonl"
        sk = session_key("_merged", "group", "20002")
    else:
        path = root / "history" / "bot_001" / "private" / "10001.jsonl"
        sk = session_key("bot_001", "private", "10001")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, sk


def test_rewrite_then_append_validates_full_prefix_hot_and_sidecar():
    """First/middle rewrites preserve old tail64, inode and length before append."""
    for cold in (False, True):
        for group in (False, True):
            for changed in (0, 500):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    path, sk = _archive(root, group)
                    original = b"".join(_encoded(_message(i, group)) for i in range(1000))
                    assert len(original) > 1 << 20
                    path.write_bytes(original)
                    cache = root / "loader-cache.json"
                    data = MohobotData(root, cache_path=cache)
                    before = data.enrich_entries(sk, {}, {})
                    reviewed = {sk: {e["fingerprint"]: {"status": "normal"} for e in before}}
                    assert data.unreviewed_counts(reviewed, revision=17).get(sk, 0) == 0
                    data.flush_cache()
                    if cold:
                        data = MohobotData(root, cache_path=cache)
                    st = path.stat()
                    rewritten = original.replace(f"A{changed:04}".encode(), f"B{changed:04}".encode())
                    rewritten = rewritten.replace(f"OLD{changed:04}".encode(), f"NEW{changed:04}".encode())
                    assert len(rewritten) == len(original)
                    assert rewritten[-64:] == original[-64:]
                    # Real in-place write, followed by append; no synthetic stat/inode.
                    with path.open("r+b") as stream:
                        stream.write(rewritten)
                        stream.write(_encoded(_message(1000, group)))
                    assert path.stat().st_ino == st.st_ino
                    after = data.enrich_entries(sk, reviewed[sk], {})
                    identities = {e["fingerprint"] for e in after}
                    assert f"mid:A{changed:04}" not in identities
                    assert f"mid:B{changed:04}" in identities
                    assert len(after) == 1001
                    assert any(e["content"].startswith(f"NEW{changed:04}") for e in after)
                    assert data.unreviewed_counts(reviewed, revision=17)[sk] == 2
                    data.flush_cache()
                    restarted = MohobotData(root, cache_path=cache)
                    assert restarted.enrich_entries(sk, reviewed[sk], {}) == after
                    assert restarted.unreviewed_counts(reviewed, revision=17)[sk] == 2
                    restarted.flush_cache()


def test_pure_append_only_parses_new_lines_and_hot_reads_do_not_parse():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path, sk = _archive(root)
        path.write_bytes(b"".join(_encoded(_message(i)) for i in range(1000)))
        data = MohobotData(root)
        before = data.filtered_rows(sk)
        calls, prefix_reads = [], []

        def observe(frame, event, arg):
            if event == "call" and frame.f_code.co_name == "_parse_line":
                calls.append(frame.f_locals["line"])
            if (event == "c_call" and frame.f_code.co_name == "_is_append_only"
                    and getattr(arg, "__name__", "") == "update"):
                prefix_reads.append(len(frame.f_locals["block"]))

        old_profile = sys.getprofile()
        sys.setprofile(observe)
        try:
            for _ in range(5):
                assert data.filtered_rows(sk)[0] is before[0]
            assert calls == []
            old_size = path.stat().st_size
            with path.open("ab") as stream:
                stream.write(_encoded(_message(1000)))
            after = data.filtered_rows(sk)
        finally:
            sys.setprofile(old_profile)
        assert len(calls) == 1
        assert sum(prefix_reads) == old_size
        assert max(prefix_reads) <= data._PREFIX_READ_CHUNK
        assert len(after) == 1001 and after[0] is before[0]
        idx = data._file_cache[str(path)]
        assert idx.offset > old_size
        assert idx.prefix_digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_status_snapshot_survives_real_local_and_external_async_writes():
    async def scenario(root: Path):
        path, sk = _archive(root)
        path.write_bytes(_encoded(_message(0)))
        data = MohobotData(root)
        store = ReviewStore(root / "review.db")
        try:
            await asyncio.to_thread(store.judge, sk, ["mid:A0000"], "normal", "first")
            old_statuses, old_counts, old_revision = await asyncio.to_thread(store.statuses_snapshot)
            ready, committed = asyncio.Event(), asyncio.Event()

            async def reader():
                ready.set()
                await committed.wait()
                assert old_statuses[sk]["mid:A0000"]["status"] == "normal"
                assert old_counts[sk] == (1, 0)
                assert await asyncio.to_thread(data.unreviewed_counts, old_statuses, old_revision) == {}

            async def writer():
                await ready.wait()
                await asyncio.to_thread(store.judge, sk, ["mid:A0000"], "abnormal", "second")
                committed.set()

            await asyncio.gather(reader(), writer())
            local_statuses, local_counts, local_revision = await asyncio.to_thread(store.statuses_snapshot)
            assert local_statuses[sk]["mid:A0000"]["status"] == "abnormal"
            assert local_counts[sk] == (0, 1) and local_revision > old_revision

            def external_swap(fingerprint="mid:stale"):
                with closing(sqlite3.connect(root / "review.db")) as connection:
                    connection.execute(
                        "UPDATE reviewed_entries SET fingerprint=?, status=? WHERE session_key=?",
                        (fingerprint, "normal", sk),
                    )
                    connection.commit()

            await asyncio.to_thread(external_swap)
            new_statuses, new_counts, new_revision = await asyncio.to_thread(store.statuses_snapshot)
            assert set(new_statuses[sk]) == {"mid:stale"}
            assert new_counts[sk] == (1, 0) and new_revision > local_revision
            assert local_statuses[sk]["mid:A0000"]["status"] == "abnormal"
            assert await asyncio.to_thread(data.unreviewed_counts, new_statuses, new_revision) == {sk: 1}
            # Same revision and cardinality cannot hide different fingerprints.
            assert data.unreviewed_counts(old_statuses, new_revision).get(sk, 0) == 0
            assert data.unreviewed_counts(new_statuses, new_revision)[sk] == 1
            page = await asyncio.to_thread(data.page_entries, sk, new_statuses[sk], {})
            assert page["unreviewed"] == 1

            # An external commit followed by a local incremental patch must retain both.
            await asyncio.to_thread(external_swap, "mid:outside")
            await asyncio.to_thread(store.judge, sk, ["mid:local"], "abnormal", "third")
            final, counts, _ = await asyncio.to_thread(store.statuses_snapshot)
            assert set(final[sk]) == {"mid:outside", "mid:local"}
            assert counts[sk] == (1, 1)
        finally:
            store.close()

    with tempfile.TemporaryDirectory() as td:
        asyncio.run(scenario(Path(td)))


def test_observed_deletion_invalidates_group_and_sidecar_before_recreation():
    for via_scan in (False, True):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path, sk = _archive(root, True)
            original = _encoded(_message(0, True))
            path.write_bytes(original)
            st = path.stat()
            cache = root / "loader-cache.json"
            data = MohobotData(root, cache_path=cache)
            assert data.filtered_rows(sk)
            old_index = data._file_cache[str(path)]
            assert "20002" in data._merged_cache
            assert data.unreviewed_counts({}, revision=1)[sk] == 1
            data.flush_cache()
            path.unlink()
            if via_scan:
                assert data.list_sessions(force=True) == []
            else:
                assert data.filtered_rows(sk) == []
            assert str(path) not in data._file_cache
            assert "20002" not in data._merged_cache
            assert data._pending_cache is None
            data.flush_cache()
            assert json.loads(cache.read_text(encoding="utf-8"))["files"] == {}
            # Same size/restored mtime; no assumption that the FS allocates a new inode.
            path.write_bytes(original.replace(b"A0000", b"B0000").replace(b"OLD0000", b"NEW0000"))
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            assert data.enrich_entries(sk, {}, {})[0]["fingerprint"] == "mid:B0000"
            assert data._file_cache[str(path)] is not old_index
            data.flush_cache()
            cold = MohobotData(root, cache_path=cache)
            assert cold.enrich_entries(sk, {}, {})[0]["fingerprint"] == "mid:B0000"
            cold.flush_cache()


def test_cold_sidecar_missing_file_is_forgotten_before_recreation():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path, sk = _archive(root)
        path.write_bytes(_encoded(_message(0)))
        cache = root / "loader-cache.json"
        data = MohobotData(root, cache_path=cache)
        assert data.load_entries(sk)
        data.flush_cache()
        path.unlink()
        cold = MohobotData(root, cache_path=cache)
        assert cold.load_entries(sk) is None
        assert str(path) not in cold._file_cache
        cold.flush_cache()
        assert json.loads(cache.read_text(encoding="utf-8"))["files"] == {}


def test_sessions_external_commit_after_status_read_does_not_poison_pending_cache():
    """Commit at the real getter's return, before the request resumes its worker."""
    import threading

    import httpx

    from review.app import create_app
    from review.config import ReviewConfig, ReviewUser
    from review.hash_password import hash_password

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path, sk = _archive(root)
        path.write_bytes(_encoded(_message(0)))
        store = ReviewStore(root / "review.db")
        store.judge(sk, ["mid:A0000"], "normal", "before")
        data = MohobotData(root)
        cfg = ReviewConfig(users=[ReviewUser("tester", hash_password("temporary-password"))])
        app = create_app(cfg, data, store)
        commits = []

        def commit_at_return(frame, event, arg):
            if (not commits and event == "return"
                    and frame.f_globals.get("__name__") == "review.store"
                    and frame.f_code.co_name in ("statuses_snapshot", "statuses_by_session")):
                commits.append(True)
                with closing(sqlite3.connect(root / "review.db")) as connection:
                    connection.execute(
                        "UPDATE reviewed_entries SET fingerprint=? WHERE session_key=?",
                        ("mid:stale", sk),
                    )
                    connection.commit()

        async def requests():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                login = await client.post("/api/login", json={
                    "username": "tester", "password": "temporary-password",
                })
                assert login.status_code == 200
                client.headers["Authorization"] = "Bearer " + login.json()["token"]
                # The first request can legitimately use its pre-commit snapshot.
                first = await client.get("/api/sessions?status=unreviewed")
                assert first.status_code == 200 and first.json()["sessions"] == []
                assert commits == [True]
                second = await client.get("/api/sessions?status=unreviewed")
                assert second.status_code == 200
                sessions = second.json()["sessions"]
                assert len(sessions) == 1 and sessions[0]["unreviewed"] == 1
                detail = await client.get("/api/session/bot_001/private/10001")
                assert detail.status_code == 200 and detail.json()["unreviewed"] == 1
                third = await client.get("/api/sessions?status=unreviewed")
                assert third.json() == second.json()

        # asyncio.run creates its actual default executor after this is installed.
        old_profile = threading.getprofile()
        threading.setprofile(commit_at_return)
        try:
            asyncio.run(requests())
        finally:
            threading.setprofile(old_profile)
            store.close()


def _tail_with_actual_read_metrics(path: Path, n: int) -> tuple[list[dict], list[int]]:
    reads = []

    def observe(frame, event, arg):
        if (event == "return" and isinstance(arg, bytes)
                and frame.f_globals.get("__name__") == "aiofiles.threadpool.utils"
                and frame.f_locals.get("attr_name") == "read"):
            reads.append(len(arg))

    old_profile = sys.getprofile()
    sys.setprofile(observe)
    try:
        result = asyncio.run(jsonl_read_tail(path, n))
    finally:
        sys.setprofile(old_profile)
    assert reads, "must observe actual aiofiles reads, not silently skip the cost assertion"
    return result, reads


def test_tail_skips_more_than_a_chunk_of_blank_and_whitespace_lines():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "blank-tail.jsonl"
        path.write_bytes(b'{"n":1}\n{"n":2}\n{"n":3}\n' + b" \t\r\n\n" * 70000)
        result, reads = _tail_with_actual_read_metrics(path, 2)
        assert result == [{"n": 2}, {"n": 3}]
        assert sum(reads) <= path.stat().st_size
        assert all(size <= _TAIL_READ_CHUNK for size in reads)


def test_cr_only_large_tail_reads_bounded_chunks():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "cr-only.jsonl"
        row = {"n": 1, "text": "x" * 64}
        line = json.dumps(row).encode() + b"\r"
        path.write_bytes(line * 200000)
        assert path.stat().st_size > 16_000_000
        result, reads = _tail_with_actual_read_metrics(path, 3)
        assert result == [row, row, row]
        assert sum(reads) <= 2 * _TAIL_READ_CHUNK


def test_tail_multichunk_utf8_mixed_newlines_and_malformed_slot_semantics():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "mixed.jsonl"
        big = {"text": "中文😀" * _TAIL_READ_CHUNK}
        payload = json.dumps(big, ensure_ascii=False).encode("utf-8")
        path.write_bytes(b'{"n":0}\r\n' + payload + b'\r  \ninvalid\r\n{"n":2}')
        assert asyncio.run(jsonl_read_tail(path, 3)) == [big, {"n": 2}]
        assert asyncio.run(jsonl_read_tail(path, 2)) == [{"n": 2}]
