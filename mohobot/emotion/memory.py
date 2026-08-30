"""长期互动记忆 — 移植自 astrbot-plugin-emotionai_pro memory.py。

显著度 ≥ 阈值的互动写入长期库(每用户上限 50 条), 并维护全局重要事件堆。
build_relationship_context() 生成注入 LLM 的「关系发展轨迹」文本。
"""

from __future__ import annotations

import heapq
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

MAX_PER_USER = 50
MAX_IMPORTANT_EVENTS = 50


@dataclass
class InteractionRecord:
    """一条互动记录(消息各截断 500 字)。"""
    user_msg: str = ""
    ai_response: str = ""
    timestamp: float = 0.0
    significance: int = 0
    emotional_changes: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_msg": self.user_msg,
            "ai_response": self.ai_response,
            "timestamp": self.timestamp,
            "significance": self.significance,
            "emotional_changes": self.emotional_changes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InteractionRecord":
        changes = data.get("emotional_changes") or {}
        return cls(
            user_msg=str(data.get("user_msg", "") or "")[:500],
            ai_response=str(data.get("ai_response", "") or "")[:500],
            timestamp=float(data.get("timestamp", 0) or 0),
            significance=int(data.get("significance", 0) or 0),
            emotional_changes={k: int(v) for k, v in changes.items() if isinstance(v, (int, float))},
        )


class MemorySystem:
    """长期记忆: {(bot_id, user_key) -> deque[InteractionRecord]} + 重要事件堆。"""

    def __init__(self) -> None:
        # bot_id -> user_key -> deque(每用户最多 50 条)
        self._memory: dict[str, dict[str, deque[InteractionRecord]]] = {}
        # (-significance, timestamp, 事件) 全局 top50
        self._important: list[tuple[int, float, dict[str, Any]]] = []
        self._dirty_bots: set[str] = set()

    # ── 加载 / 序列化 ────────────────────────────────────────

    def load_bot(self, bot_id: str, records: dict[str, list[dict]] | None) -> None:
        per_user: dict[str, deque[InteractionRecord]] = {}
        for user_key, items in (records or {}).items():
            if not isinstance(items, list):
                continue
            dq: deque[InteractionRecord] = deque(maxlen=MAX_PER_USER)
            for item in items[-MAX_PER_USER:]:
                try:
                    dq.append(InteractionRecord.from_dict(item))
                except (TypeError, ValueError):
                    continue
            if dq:
                per_user[user_key] = dq
        self._memory[bot_id] = per_user

    def records_for_save(self, bot_id: str) -> dict[str, list[dict]]:
        return {
            user_key: [r.to_dict() for r in records]
            for user_key, records in self._memory.get(bot_id, {}).items()
        }

    # ── 写入 / 查询 ──────────────────────────────────────────

    def add_interaction(
        self, bot_id: str, user_key: str, user_msg: str, ai_response: str,
        significance: int, emotional_changes: dict[str, int] | None,
        threshold: int,
    ) -> bool:
        """显著度达标才写长期库。返回是否写入。"""
        record = InteractionRecord(
            user_msg=(user_msg or "")[:500],
            ai_response=(ai_response or "")[:500],
            timestamp=time.time(),
            significance=int(significance),
            emotional_changes=dict(emotional_changes or {}),
        )

        per_user = self._memory.setdefault(bot_id, {})
        dq = per_user.setdefault(user_key, deque(maxlen=MAX_PER_USER))
        written = False
        if record.significance >= threshold:
            dq.append(record)
            heapq.heappush(self._important, (
                -record.significance, record.timestamp,
                {"bot_id": bot_id, "user_key": user_key,
                 "user_msg": record.user_msg[:100],
                 "significance": record.significance},
            ))
            while len(self._important) > MAX_IMPORTANT_EVENTS:
                heapq.heappop(self._important)
            self._dirty_bots.add(bot_id)
            written = True
        return written

    def build_relationship_context(self, bot_id: str, user_key: str) -> str:
        """「关系发展轨迹」注入文本(空记录时返回 "")。"""
        records = self._memory.get(bot_id, {}).get(user_key)
        if not records:
            return ""
        important_count = sum(
            1 for _, _, ev in self._important
            if ev.get("bot_id") == bot_id and ev.get("user_key") == user_key
        )
        avg = sum(r.significance for r in records) / len(records)
        recent_significant = sum(1 for r in records if r.significance >= 7)
        lines = ["【长期关系发展轨迹】"]
        lines.append(f"深度互动次数: {len(records)}")
        lines.append(f"平均情感深度: {avg:.1f}/10")
        if recent_significant:
            lines.append(f"近期重要互动: {recent_significant}次")
        if important_count:
            lines.append(f"重要时刻: {important_count}个")
        return "\n".join(lines)

    def user_memory_stats(self, bot_id: str, user_key: str) -> dict[str, Any]:
        records = self._memory.get(bot_id, {}).get(user_key)
        return {
            "long_term_count": len(records) if records else 0,
            "avg_significance": (
                sum(r.significance for r in records) / len(records) if records else 0.0
            ),
            "last_interaction": records[-1].timestamp if records else 0.0,
        }

    def clear_bot(self, bot_id: str) -> None:
        self._memory.pop(bot_id, None)
        self._important = [
            ev for ev in self._important if ev[2].get("bot_id") != bot_id
        ]
        heapq.heapify(self._important)
        self._dirty_bots.add(bot_id)

    def pop_dirty_bots(self) -> set[str]:
        dirty = self._dirty_bots
        self._dirty_bots = set()
        return dirty

    def stats(self) -> dict[str, int]:
        total = sum(len(u) for u in self._memory.values())
        return {"bots": len(self._memory), "records": total}
