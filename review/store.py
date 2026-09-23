"""审核状态存储 — 独立 SQLite (review/data/review.db)。

三张表:
  reviewed_entries  每条消息的审核结论(指纹去重; 改判 = 覆盖更新)
  abnormal_records  异常记录(专门界面的数据源; 可编辑标签/备注)
  review_log        全部操作留痕(判定/改判/跳过/编辑), 供统计与追溯

语义:
  跳过 = 不写 reviewed_entries, 只记日志 → 状态仍是"未审核"
  正常/异常 = upsert reviewed_entries(后写覆盖, 记录最后审核人)
  summary/system 条目为豁免, 不入库
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviewed_entries (
    session_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status      TEXT NOT NULL,              -- normal | abnormal
    reviewer    TEXT NOT NULL,
    reviewed_at REAL NOT NULL,
    PRIMARY KEY (session_key, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_reviewed_session ON reviewed_entries (session_key);

CREATE TABLE IF NOT EXISTS abnormal_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT '',
    speaker     TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    message_id  TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]', -- JSON 数组
    note        TEXT NOT NULL DEFAULT '',
    reviewer    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_abnormal_session ON abnormal_records (session_key);

CREATE TABLE IF NOT EXISTS review_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    time        REAL NOT NULL,
    reviewer    TEXT NOT NULL,
    action      TEXT NOT NULL,              -- judge / rejudge / skip / abnormal_edit
    session_key TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);
"""


class ReviewStore:
    """审核状态读写(单连接 + 线程锁; 操作都很小, 同步执行足够)。

    statuses_by_session() 结果带进程内缓存: 全量重建在 7 万行量级要 ~0.9s,
    而每个页面请求都要它。缓存失效策略:
      - 本地写入(judge) → 增量补丁缓存 + 刷新戳记(无需重建)
      - 外部写入(运维脚本等) → 比对 review.db / -wal 的 (mtime,size) 戳记
    """

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # 状态缓存: {session_key: {fingerprint: {...}}} 与逐会话计数
        self._statuses_cache: dict[str, dict[str, dict[str, Any]]] | None = None
        self._counts_cache: dict[str, tuple[int, int]] | None = None
        self._statuses_stamp: tuple = ()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _db_stamp(self) -> tuple:
        """review.db 与 -wal 的 (mtime_ns, size) 戳记 — 检测外部写入。"""
        out = []
        for suffix in ("", "-wal"):
            try:
                st = Path(str(self._path) + suffix).stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((0, 0))
        return tuple(out)

    def _build_statuses_locked(self) -> dict[str, dict[str, dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT session_key, fingerprint, status, reviewer, reviewed_at FROM reviewed_entries"
        ).fetchall()
        result: dict[str, dict[str, dict[str, Any]]] = {}
        counts: dict[str, list[int]] = {}
        for r in rows:
            result.setdefault(r["session_key"], {})[r["fingerprint"]] = {
                "status": r["status"],
                "reviewer": r["reviewer"],
                "reviewed_at": r["reviewed_at"],
            }
            c = counts.setdefault(r["session_key"], [0, 0])
            c[0 if r["status"] == "normal" else 1] += 1
        self._statuses_cache = result
        self._counts_cache = {k: (v[0], v[1]) for k, v in counts.items()}
        self._statuses_stamp = self._db_stamp()
        return result

    def _ensure_statuses_locked(self) -> dict[str, dict[str, dict[str, Any]]]:
        if (self._statuses_cache is not None
                and self._db_stamp() == self._statuses_stamp):
            return self._statuses_cache
        return self._build_statuses_locked()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 判定 ─────────────────────────────────────────────────

    def judge(
        self, session_key: str, fingerprints: list[str], status: str, reviewer: str,
    ) -> int:
        """把若干指纹判定为 normal/abnormal(已审过且结论不同 → 改判)。返回条数。

        单次查询取本会话既有结论 + executemany 批量写入(原先逐条 SELECT/INSERT),
        写完增量补丁状态缓存(避免下次读取全量重建 0.9s)。
        """
        now = time.time()
        changed = 0
        updates: list[tuple[str, str, str, float]] = []
        with self._lock:
            cur = self._conn.cursor()
            existing = {
                r["fingerprint"]: r["status"]
                for r in cur.execute(
                    "SELECT fingerprint, status FROM reviewed_entries WHERE session_key=?",
                    (session_key,),
                ).fetchall()
            }
            row_values: list[tuple] = []
            log_values: list[tuple] = []
            for fp in fingerprints:
                prev = existing.get(fp)
                action = "judge"
                if prev is not None:
                    if prev == status:
                        continue  # 同结论重复提交 → 无操作
                    action = "rejudge"
                row_values.append((session_key, fp, status, reviewer, now))
                log_values.append((now, reviewer, action, session_key, fp, status))
                updates.append((fp, status, reviewer, now))
                changed += 1
            if row_values:
                cur.executemany(
                    "INSERT INTO reviewed_entries (session_key, fingerprint, status, reviewer, reviewed_at) "
                    "VALUES (?,?,?,?,?) "
                    "ON CONFLICT(session_key, fingerprint) DO UPDATE SET "
                    "status=excluded.status, reviewer=excluded.reviewer, reviewed_at=excluded.reviewed_at",
                    row_values,
                )
                cur.executemany(
                    "INSERT INTO review_log (time, reviewer, action, session_key, fingerprint, detail) "
                    "VALUES (?,?,?,?,?,?)",
                    log_values,
                )
                self._conn.commit()
            self._patch_statuses_locked(session_key, updates)
        return changed

    def _patch_statuses_locked(
        self, session_key: str, updates: list[tuple[str, str, str, float]],
        removed: list[str] | None = None,
    ) -> None:
        """本地写入后增量更新状态缓存(缓存未建立时无需处理)。

        同时刷新 db 戳记 —— 否则本地写 wal 会让下次读取误判为"外部写入"而重建。
        """
        removed = removed or []
        if self._statuses_cache is None or (not updates and not removed):
            return
        bucket = self._statuses_cache.setdefault(session_key, {})
        counts = None
        if self._counts_cache is not None:
            prev_counts = self._counts_cache.get(session_key, (0, 0))
            counts = [prev_counts[0], prev_counts[1]]
        for fp in removed:
            prev = bucket.pop(fp, None)
            if counts is not None and prev is not None:
                counts[0 if prev["status"] == "normal" else 1] -= 1
        for fp, status, reviewer, ts in updates:
            prev = bucket.get(fp)
            if counts is not None and prev is not None:
                counts[0 if prev["status"] == "normal" else 1] -= 1
            bucket[fp] = {"status": status, "reviewer": reviewer, "reviewed_at": ts}
            if counts is not None:
                counts[0 if status == "normal" else 1] += 1
        if counts is not None and self._counts_cache is not None:
            self._counts_cache[session_key] = (counts[0], counts[1])
        self._statuses_stamp = self._db_stamp()

    def skip(self, session_key: str, reviewer: str, detail: str = "") -> None:
        """跳过: 只留痕, 不改任何审核状态。"""
        self._log(reviewer, "skip", session_key, "", detail)

    def _log(self, reviewer: str, action: str, session_key: str, fp: str, detail: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO review_log (time, reviewer, action, session_key, fingerprint, detail) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), reviewer, action, session_key, fp, detail),
            )
            self._conn.commit()

    # ── 查询 ─────────────────────────────────────────────────

    def statuses_by_session(self) -> dict[str, dict[str, dict[str, Any]]]:
        """全部已审条目: {session_key: {fingerprint: {status, reviewer, reviewed_at}}}。

        进程内缓存(全量重建 ~0.9s / 7 万行); 外部写入由 db 戳记检测后自动重建。
        """
        with self._lock:
            return self._ensure_statuses_locked()

    def counts_by_session(self) -> dict[str, tuple[int, int]]:
        """{session_key: (normal 数, abnormal 数)} — 与 statuses_by_session 同一次构建。"""
        with self._lock:
            self._ensure_statuses_locked()
            return dict(self._counts_cache or {})

    def abnormal_by_fingerprint(self) -> dict[str, dict[str, Any]]:
        """{fingerprint: 最新一条异常记录} — 会话明细里给 abnormal 条目附带标签/备注。

        tags 必须解析成数组(前端按列表渲染; 原始行里是 JSON 文本, 直接透传
        会让前端 .map 抛错 → 整个会话渲染失败, 表现为"会话打不开")。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM abnormal_records ORDER BY id ASC"
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for r in rows:
            item = dict(r)
            try:
                tags = json.loads(item.get("tags") or "[]")
            except (ValueError, TypeError):
                tags = []
            item["tags"] = tags if isinstance(tags, list) else []
            result[r["fingerprint"]] = item
        return result

    # ── 异常记录 ─────────────────────────────────────────────

    def add_abnormal(
        self, session_key: str, fingerprint: str, role: str, speaker: str,
        content: str, message_id: str, tags: list[str], note: str, reviewer: str,
    ) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO abnormal_records "
                "(session_key, fingerprint, role, speaker, content, message_id, tags, note, reviewer, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (session_key, fingerprint, role, speaker, content, message_id,
                 json.dumps(tags, ensure_ascii=False), note, reviewer, now, now),
            )
            rid = int(cur.lastrowid or 0)
            # 判定留痕由 judge() 负责, 这里只写异常记录
            self._conn.commit()
        return rid

    def list_abnormal(self, bot: str = "", tag: str = "") -> list[dict[str, Any]]:
        """异常记录列表(可选按 bot 前缀/标签过滤; 标签在应用层过滤)。"""
        sql = "SELECT * FROM abnormal_records"
        args: list[Any] = []
        if bot:
            sql += " WHERE session_key LIKE ?"
            args.append(f"{bot}/%")
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["tags"] = json.loads(item.get("tags") or "[]")
            if tag and tag not in item["tags"]:
                continue
            result.append(item)
        return result

    def update_abnormal(self, record_id: int, tags: list[str], note: str, reviewer: str) -> bool:
        """编辑标签/备注; 不改 reviewer(保留原标记人, 编辑动作由 review_log 留痕)。"""
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT id FROM abnormal_records WHERE id=?", (record_id,)
            ).fetchone()
            if row is None:
                return False
            cur.execute(
                "UPDATE abnormal_records SET tags=?, note=?, updated_at=? WHERE id=?",
                (json.dumps(tags, ensure_ascii=False), note, time.time(), record_id),
            )
            cur.execute(
                "INSERT INTO review_log (time, reviewer, action, session_key, fingerprint, detail) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), reviewer, "abnormal_edit", "", "", f"#{record_id}"),
            )
            self._conn.commit()
        return True

    def get_abnormal(self, record_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM abnormal_records WHERE id=?", (record_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["tags"] = json.loads(item.get("tags") or "[]")
        return item

    def delete_abnormal(self, record_id: int, reviewer: str) -> dict[str, Any] | None:
        """删除一条异常记录, 并把该消息的审核结论一并撤销(回到未审核)。

        撤销结论是必要的: 否则会话里会残留一条"没有标签/备注的异常"。
        返回被删记录(不存在返回 None)。操作留痕 action="abnormal_delete"。
        """
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT * FROM abnormal_records WHERE id=?", (record_id,)
            ).fetchone()
            if row is None:
                return None
            rec = dict(row)
            cur.execute("DELETE FROM abnormal_records WHERE id=?", (record_id,))
            cur.execute(
                "DELETE FROM reviewed_entries WHERE session_key=? AND fingerprint=?",
                (rec["session_key"], rec["fingerprint"]),
            )
            cur.execute(
                "INSERT INTO review_log (time, reviewer, action, session_key, fingerprint, detail) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), reviewer, "abnormal_delete", rec["session_key"],
                 rec["fingerprint"], f"#{record_id}"),
            )
            self._conn.commit()
            self._patch_statuses_locked(
                rec["session_key"], [], removed=[rec["fingerprint"]],
            )
        return rec

    # ── 统计 ─────────────────────────────────────────────────

    def reviewer_stats(self) -> list[dict[str, Any]]:
        """各审核员工作量: 判定条数(按 reviewed_entries 最新结论) + 异常标记数。"""
        with self._lock:
            judged = self._conn.execute(
                "SELECT reviewer, COUNT(*) AS n, MAX(reviewed_at) AS last_at "
                "FROM reviewed_entries GROUP BY reviewer ORDER BY n DESC"
            ).fetchall()
            marked = self._conn.execute(
                "SELECT reviewer, COUNT(*) AS n FROM abnormal_records GROUP BY reviewer"
            ).fetchall()
        abnormal_map = {r["reviewer"]: r["n"] for r in marked}
        return [
            {
                "reviewer": r["reviewer"],
                "judged": r["n"],
                "abnormal": abnormal_map.get(r["reviewer"], 0),
                "last_active": r["last_at"],
            }
            for r in judged
        ]

    def recent_log(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM review_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def log_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM review_log").fetchone()
        return int(row["n"]) if row else 0
