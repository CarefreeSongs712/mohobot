"""新闻/动态知识库 — 采集入库、管理员审核、按需检索与提示词注入。

设计原则(REQ-6):
- **默认关闭**(`news.enabled=False`), 不改变既有部署行为;
- 所有新增条目初始状态为 `pending`, **必须经管理员审核**(approve)后才
  参与检索与注入, 防止不实/敏感内容直接污染 Bot 的知识面;
- 只读消费: 注入与工具查询都不会修改条目状态;
- 过期(`expires_at` 已过)的条目自动不再返回, 但保留在库中便于审计。

存储用纯 stdlib sqlite3(与 knowledge/index_store.py 一致), 同步 API,
重操作由调用方用 asyncio.to_thread 包裹。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_item(
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT,
    url TEXT,
    author_name TEXT,
    author_id TEXT,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    published_at REAL,
    created_at REAL NOT NULL,
    reviewed_at REAL,
    reviewed_by TEXT,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_news_status ON news_item(status);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_item(published_at);
"""


@dataclass
class NewsItem:
    """一条新闻/动态条目。"""

    title: str
    content: str = ""
    source: str = "manual"
    url: str = ""
    author_name: str = ""
    author_id: str = ""
    tags: list[str] = field(default_factory=list)
    status: str = STATUS_PENDING
    published_at: float = 0.0
    created_at: float = 0.0
    reviewed_at: Optional[float] = None
    reviewed_by: Optional[str] = None
    expires_at: Optional[float] = None
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex
        if not self.created_at:
            self.created_at = time.time()
        if not self.published_at:
            self.published_at = self.created_at
        if self.status not in _STATUSES:
            self.status = STATUS_PENDING

    def to_row(self) -> tuple:
        return (
            self.id, self.source, self.title, self.content, self.url,
            self.author_name, self.author_id,
            json.dumps(self.tags, ensure_ascii=False),
            self.status, self.published_at, self.created_at,
            self.reviewed_at, self.reviewed_by, self.expires_at,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "NewsItem":
        tags_raw = row["tags"] or "[]"
        try:
            tags = json.loads(tags_raw)
            if not isinstance(tags, list):
                tags = []
        except (ValueError, TypeError):
            tags = []
        return cls(
            id=row["id"], source=row["source"], title=row["title"],
            content=row["content"] or "", url=row["url"] or "",
            author_name=row["author_name"] or "", author_id=row["author_id"] or "",
            tags=tags, status=row["status"], published_at=row["published_at"] or 0.0,
            created_at=row["created_at"] or 0.0, reviewed_at=row["reviewed_at"],
            reviewed_by=row["reviewed_by"], expires_at=row["expires_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_prompt_line(self, *, max_chars: int = 120) -> str:
        """注入提示词用的一行摘要(标注来源与时间)。"""
        text = (self.content or "").strip().replace("\n", " ")
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        day = ""
        if self.published_at:
            day = time.strftime("%Y-%m-%d", time.localtime(self.published_at))
        origin = self.author_name or self.source
        parts = [f"- [{day}] {self.title}"]
        if text:
            parts.append(f"（{text}）")
        if origin:
            parts.append(f"来源: {origin}")
        return "".join(parts)


class NewsStore:
    """新闻条目持久化(同步 API)。"""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
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
            raise RuntimeError("NewsStore 未打开")
        return self._conn

    # ── 写入 ──────────────────────────────────────────────────

    def submit(self, item: NewsItem) -> str:
        """入库为待审状态; 同一 url 的重复条目直接返回已存在 id。"""
        with self._lock:
            conn = self._require_conn()
            if item.url:
                row = conn.execute(
                    "SELECT id FROM news_item WHERE url = ? AND url != '' LIMIT 1",
                    (item.url,),
                ).fetchone()
                if row is not None:
                    return str(row["id"])
            item.status = STATUS_PENDING
            conn.execute(
                "INSERT INTO news_item(id, source, title, content, url, author_name,"
                " author_id, tags, status, published_at, created_at, reviewed_at,"
                " reviewed_by, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                item.to_row(),
            )
            conn.commit()
            return item.id

    def review(self, item_id: str, *, approve: bool, reviewer: str = "") -> bool:
        """管理员审核: approve=True 入库生效, False 标记拒绝。"""
        status = STATUS_APPROVED if approve else STATUS_REJECTED
        with self._lock:
            conn = self._require_conn()
            cur = conn.execute(
                "UPDATE news_item SET status = ?, reviewed_at = ?, reviewed_by = ?"
                " WHERE id = ? AND status = ?",
                (status, time.time(), reviewer, item_id, STATUS_PENDING),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete(self, item_id: str) -> bool:
        with self._lock:
            conn = self._require_conn()
            cur = conn.execute("DELETE FROM news_item WHERE id = ?", (item_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── 读取 ──────────────────────────────────────────────────

    def get(self, item_id: str) -> Optional[NewsItem]:
        with self._lock:
            row = self._require_conn().execute(
                "SELECT * FROM news_item WHERE id = ?", (item_id,),
            ).fetchone()
        return NewsItem.from_row(row) if row is not None else None

    def list_pending(self, *, limit: int = 50) -> list[NewsItem]:
        return self._list(status=STATUS_PENDING, limit=limit)

    def list_approved(self, *, limit: int = 20, now: float | None = None) -> list[NewsItem]:
        """返回已审核且未过期的条目, 新的在前。"""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT * FROM news_item WHERE status = ?"
                " AND (expires_at IS NULL OR expires_at > ?)"
                " ORDER BY published_at DESC, created_at DESC LIMIT ?",
                (STATUS_APPROVED, now, int(limit)),
            ).fetchall()
        return [NewsItem.from_row(r) for r in rows]

    def search_approved(
        self, keywords: list[str], *, limit: int = 3, now: float | None = None,
    ) -> list[NewsItem]:
        """在已审核条目中按关键词(标题/正文/标签)宽松匹配。"""
        now = time.time() if now is None else now
        items = self.list_approved(limit=max(limit * 20, 50), now=now)
        keys = [k.strip().lower() for k in keywords if k and k.strip()]
        if not keys:
            return items[:limit]
        hits: list[NewsItem] = []
        for item in items:
            haystack = " ".join([
                item.title, item.content, " ".join(item.tags),
                item.author_name,
            ]).lower()
            if any(k in haystack for k in keys):
                hits.append(item)
            if len(hits) >= limit:
                break
        return hits

    def _list(self, *, status: str, limit: int) -> list[NewsItem]:
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT * FROM news_item WHERE status = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        return [NewsItem.from_row(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT status, COUNT(*) AS n FROM news_item GROUP BY status",
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}
