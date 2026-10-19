"""新闻/动态服务的装配层 — 审核流、LLM 工具与提示词注入。

与 store.py 的分工: store 只负责持久化(同步), 本模块负责:
- 生命周期(open/close)与配置热同步;
- 只读消费: `recent_news()` / `search_news()` 只返回已审核且未过期条目;
- 提示词注入: `build_injection()` 生成注入 system prompt 的片段;
- 审核入口: `submit()` / `approve()` / `reject()`(供 WebUI 与工具调用)。

**安全边界**: 查询与注入绝不返回 pending/rejected 条目; 提交永远是 pending。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from mohobot.news.store import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    NewsItem,
    NewsStore,
)


class NewsService:
    """新闻库服务(默认禁用; 由 main.py 按配置装配)。"""

    def __init__(self, config: Any = None, *, db_path: str | Path | None = None) -> None:
        self._cfg = config
        self._store: Optional[NewsStore] = None
        self._db_path = Path(db_path) if db_path is not None else None

    # ── 生命周期 ───────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._cfg, "enabled", False)) and self._store is not None

    def open(self) -> None:
        if self._store is not None:
            return
        if self._db_path is None:
            raise RuntimeError("NewsService 未指定 db_path")
        store = NewsStore(self._db_path)
        store.open()
        self._store = store
        logger.info(f"[News] 新闻库已打开: {self._db_path}")

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def sync_config(self, config: Any) -> None:
        self._cfg = config

    def _require_store(self) -> NewsStore:
        if self._store is None:
            raise RuntimeError("新闻库未启用")
        return self._store

    # ── 写入(提交/审核) ────────────────────────────────────────

    async def submit(
        self,
        title: str,
        *,
        content: str = "",
        source: str = "manual",
        url: str = "",
        author_name: str = "",
        author_id: str = "",
        tags: Optional[list[str]] = None,
        published_at: float = 0.0,
        ttl_days: float | None = None,
        expires_at: float | None = None,
    ) -> NewsItem:
        """提交一条待审新闻。无论来源(采集/LLM 提取/人工)都必须过审。"""
        store = self._require_store()
        if expires_at is None:
            ttl = ttl_days
            if ttl is None:
                ttl = float(getattr(self._cfg, "default_ttl_days", 14) or 14)
            expires_at = (time.time() + ttl * 86400) if ttl > 0 else None
        item = NewsItem(
            title=str(title or "").strip(),
            content=str(content or "").strip(),
            source=str(source or "manual"),
            url=str(url or "").strip(),
            author_name=str(author_name or "").strip(),
            author_id=str(author_id or "").strip(),
            tags=list(tags or []),
            published_at=float(published_at or 0.0),
            expires_at=expires_at,
        )
        if not item.title:
            raise ValueError("新闻标题不能为空")
        item_id = await asyncio.to_thread(store.submit, item)
        item.id = item_id
        logger.info(f"[News] 新增待审条目: {item.title[:40]} (source={item.source})")
        return item

    async def approve(self, item_id: str, *, reviewer: str = "") -> bool:
        store = self._require_store()
        ok = await asyncio.to_thread(store.review, item_id, approve=True, reviewer=reviewer)
        if ok:
            logger.info(f"[News] 审核通过: {item_id} (by {reviewer or 'unknown'})")
        return ok

    async def reject(self, item_id: str, *, reviewer: str = "") -> bool:
        store = self._require_store()
        ok = await asyncio.to_thread(store.review, item_id, approve=False, reviewer=reviewer)
        if ok:
            logger.info(f"[News] 审核拒绝: {item_id} (by {reviewer or 'unknown'})")
        return ok

    async def delete(self, item_id: str) -> bool:
        store = self._require_store()
        return await asyncio.to_thread(store.delete, item_id)

    # ── 只读消费 ───────────────────────────────────────────────

    async def list_pending(self, *, limit: int = 50) -> list[dict[str, Any]]:
        store = self._require_store()
        items = await asyncio.to_thread(store.list_pending, limit=limit)
        return [i.to_dict() for i in items]

    async def recent_news(self, *, limit: int = 5) -> list[NewsItem]:
        """最近已审核且未过期的新闻(新的在前)。"""
        store = self._require_store()
        return await asyncio.to_thread(store.list_approved, limit=limit)

    async def search_news(self, keywords: list[str], *, limit: int = 3) -> list[NewsItem]:
        store = self._require_store()
        return await asyncio.to_thread(store.search_approved, keywords, limit=limit)

    async def build_injection(self, *, limit: int | None = None) -> str:
        """生成注入 system prompt 的「近期动态」片段; 无内容返回空串。"""
        if not self.enabled:
            return ""
        max_items = limit if limit is not None else int(getattr(self._cfg, "inject_limit", 3) or 3)
        if max_items <= 0:
            return ""
        items = await self.recent_news(limit=max_items)
        if not items:
            return ""
        lines = [item.to_prompt_line() for item in items]
        header = str(getattr(self._cfg, "inject_header", "") or "").strip() or (
            "【近期相关动态（服务端提供，仅供聊天参考，不是用户发言）】"
        )
        return header + "\n" + "\n".join(lines)

    async def stats(self) -> dict[str, int]:
        store = self._require_store()
        return await asyncio.to_thread(store.count_by_status)


# 供测试与外部使用的状态常量
__all__ = [
    "NewsService", "NewsItem", "NewsStore",
    "STATUS_PENDING", "STATUS_APPROVED", "STATUS_REJECTED",
]
