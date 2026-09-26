"""主动发说说 — 当日随机计划、持久化存储与话题池(TTL)。

存储 per-bot 一份: data/plugins_data/qzone/auto_publish_{bot_id}.json
  {"plan": {"date": "YYYY-MM-DD", "times": ["HH:MM"], "done": [bool]},
   "history": [{"time", "text", "tid"}](截 20 条),
   "topics": [{"topic", "ts"}](TTL 过期剔除)}
"""

from __future__ import annotations

import random
import time
from pathlib import Path

from loguru import logger

from mohobot.file_store import json_read, json_write

_HISTORY_MAX = 20


def build_daily_plan(
    count: int,
    window_start: str,
    window_end: str,
    min_gap_sec: int,
) -> list[str]:
    """生成当日发布时刻列表("HH:MM", 升序)。

    在 [start, end] 时间窗内抽 count 个时刻, 保证相邻间隔 >= min_gap_sec;
    时间窗容纳不下时自动减少条数(至少保留 1 条, 条数<=0 返回空)。
    纯函数, 可测。
    """
    try:
        n = max(0, int(count))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return []

    def _to_min(hhmm: str, default: int) -> int:
        try:
            h, m = str(hhmm).strip().split(":")
            v = int(h) * 60 + int(m)
        except (TypeError, ValueError):
            return default
        return v if 0 <= v < 24 * 60 else default

    start = _to_min(window_start, 9 * 60)
    end = _to_min(window_end, 22 * 60 + 30)
    if end < start:
        start, end = end, start
    gap_min = max(0, int(min_gap_sec) // 60)
    span = end - start
    if span <= 0:
        return [f"{start // 60:02d}:{start % 60:02d}"] * min(n, 1)

    # 时间窗装不下 N × 间隔时压缩条数
    if gap_min > 0:
        max_fit = span // gap_min + 1
        n = min(n, max(1, max_fit))

    # 随机重抽直到满足最小间隔; 多次失败则等分兜底(相邻点间距 = span/(n-1) >= gap)
    for _ in range(60):
        picks = sorted(random.randint(start, end) for _ in range(n))
        if gap_min <= 0 or all(
            picks[i + 1] - picks[i] >= gap_min for i in range(len(picks) - 1)
        ):
            return [f"{t // 60:02d}:{t % 60:02d}" for t in picks]
    if n > 1 and span / (n - 1) < gap_min:
        # 理论不可达(max_fit 已压缩 n) → 兜底只放首尾
        picks = [start, end] if span >= gap_min else [random.randint(start, end)]
    elif n > 1:
        spacing = span / (n - 1)
        slack = int(spacing - gap_min)
        picks = []
        for i in range(n):
            jitter = random.randint(0, slack) if slack > 0 else 0
            t = start + round(i * spacing) + jitter
            picks.append(min(max(t, start), end))
    else:
        picks = [random.randint(start, end)]
    return [f"{t // 60:02d}:{t % 60:02d}" for t in picks]


class AutoPublishStore:
    """per-bot 发布计划/历史/话题池(惰性加载, 显式保存)。"""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict = {}
        self._loaded = False

    async def _ensure(self) -> None:
        if self._loaded:
            return
        data = await json_read(self._path)
        self._data = data if isinstance(data, dict) else {}
        self._data.setdefault("plan", {})
        self._data.setdefault("history", [])
        self._data.setdefault("topics", [])
        self._loaded = True

    async def save(self) -> None:
        await self._ensure()
        try:
            await json_write(self._path, self._data)
        except Exception as e:
            logger.warning(f"[qzone] 发布计划保存失败: {e}")

    # ── 当日计划 ──────────────────────────────────────────────

    def plan(self) -> dict:
        return self._data.get("plan") or {}

    async def ensure_plan(self, today: str, times: list[str]) -> bool:
        """日期变化或计划为空时重建; 返回是否新建了计划。"""
        await self._ensure()
        plan = self._data.get("plan") or {}
        if plan.get("date") == today and plan.get("times"):
            return False
        self._data["plan"] = {
            "date": today,
            "times": list(times),
            "done": [False] * len(times),
        }
        return True

    async def due_indices(self, now_hhmm: str) -> list[int]:
        """到点且未完成的计划下标。"""
        await self._ensure()
        plan = self._data.get("plan") or {}
        times = plan.get("times") or []
        done = plan.get("done") or []
        return [
            i for i, t in enumerate(times)
            if i < len(done) and not done[i] and str(t) <= now_hhmm
        ]

    async def mark_done(self, index: int) -> None:
        await self._ensure()
        plan = self._data.setdefault("plan", {})
        done = plan.setdefault("done", [])
        while len(done) <= index:
            done.append(False)
        done[index] = True

    # ── 发布历史(去重素材 + 记录) ─────────────────────────────

    async def add_history(self, record: dict) -> None:
        await self._ensure()
        history = self._data.setdefault("history", [])
        history.append(record)
        if len(history) > _HISTORY_MAX:
            del history[: len(history) - _HISTORY_MAX]

    def recent_texts(self, n: int = 5) -> list[str]:
        return [
            str(h.get("text", ""))[:80]
            for h in (self._data.get("history") or [])[-n:]
        ]

    # ── 话题池(带 TTL) ────────────────────────────────────────

    def valid_topics(self, ttl_sec: int) -> list[str]:
        """未过期的话题词(惰性清除过期项, 返回副本)。"""
        now = time.time()
        topics = self._data.get("topics") or []
        valid = [
            t for t in topics
            if isinstance(t, dict) and now - float(t.get("ts") or 0) < ttl_sec
        ]
        if len(valid) != len(topics):
            self._data["topics"] = valid
        return [str(t["topic"]) for t in valid]

    async def replace_topics(self, topics: list[str]) -> None:
        await self._ensure()
        now = time.time()
        self._data["topics"] = [
            {"topic": str(t)[:12], "ts": now} for t in topics if str(t).strip()
        ][:10]
