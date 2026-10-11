"""向量索引存储 — SQLite(chunks/files/meta 三表) + 文件级增量。

纯 stdlib sqlite3 + 线程锁, 方法皆为同步(重操作由 service 用
asyncio.to_thread 包裹)。设计假设: 库很小(数百块), 全量载入内存
做 numpy 余弦检索; SQLite 只负责增量持久化, 可随时整库删除重建。

文件级增量: files 表记录每个知识源文件的 (mtime, size);
文件变化 → 整文件重切块重嵌入并原子替换该文件的 chunks,
文件消失 → 连带删除其 chunks。chunk 级 hash 对比在这个量级
收益为零, 反而多一个状态层。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY,
    mtime REAL,
    size INTEGER,
    chunk_count INTEGER,
    indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS chunks(
    chunk_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT,
    text TEXT,
    embedding BLOB,
    model TEXT,
    indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
"""


class KnowledgeIndexStore:
    """知识向量索引的持久化层(同步 API)。"""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("KnowledgeIndexStore 未打开")
        return self._conn

    # ── meta ──────────────────────────────────────────────────

    def get_meta(self, key: str) -> Optional[str]:
        # 读写同连接跨线程使用, 读也加锁: 避免与写事务交错读到半程状态
        with self._lock:
            row = self._require_conn().execute(
                "SELECT value FROM meta WHERE key = ?", (key,),
            ).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._require_conn().execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # ── files / chunks ────────────────────────────────────────

    def list_file_states(self) -> dict[str, tuple[float, int]]:
        """path → (mtime, size)。"""
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT path, mtime, size FROM files"
            ).fetchall()
        return {path: (float(mtime or 0), int(size or 0)) for path, mtime, size in rows}

    def replace_file(
        self, path: str, mtime: float, size: int,
        rows: list[dict], model: str, indexed_at: str,
    ) -> None:
        """原子替换一个文件的索引: 删旧 chunks → 插新 chunks + files 行。

        rows: [{chunk_id, title, text, embedding(bytes)}]。
        """
        with self._lock:
            conn = self._require_conn()
            try:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM chunks WHERE source = ?", (path,))
                for row in rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO chunks"
                        "(chunk_id, source, title, text, embedding, model, indexed_at) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["chunk_id"], path, row.get("title", ""),
                            row.get("text", ""), row.get("embedding"),
                            model, indexed_at,
                        ),
                    )
                conn.execute(
                    "INSERT INTO files(path, mtime, size, chunk_count, indexed_at) "
                    "VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, "
                    "size = excluded.size, chunk_count = excluded.chunk_count, "
                    "indexed_at = excluded.indexed_at",
                    (path, mtime, size, len(rows), indexed_at),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def remove_file(self, path: str) -> None:
        with self._lock:
            conn = self._require_conn()
            conn.execute("DELETE FROM chunks WHERE source = ?", (path,))
            conn.execute("DELETE FROM files WHERE path = ?", (path,))
            conn.commit()

    def load_chunks(self) -> list[dict]:
        """全量载入(含 embedding 二进制), 供内存矩阵构建。

        库为数百块量级, 全量带向量载入(<几 MB)远比二次查询简单。
        """
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT chunk_id, source, title, text, embedding, model FROM chunks "
                "ORDER BY source, rowid"
            ).fetchall()
        return [
            {
                "chunk_id": chunk_id, "source": source,
                "title": title or "", "text": text or "",
                "embedding": embedding, "model": model or "",
            }
            for chunk_id, source, title, text, embedding, model in rows
        ]

    def count_chunks(self) -> int:
        with self._lock:
            return int(self._require_conn().execute(
                "SELECT COUNT(*) FROM chunks").fetchone()[0])
