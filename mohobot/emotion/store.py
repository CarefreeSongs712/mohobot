"""情感数据存储 — 基于 mohobot.file_store 的原子 JSON 读写。

每 bot 一个目录: data/emotion/{bot_id}/
  - user_states.json: {user_key: EmotionalState.to_dict()}
  - memory.json:      {user_key: [InteractionRecord...]}

内存缓存 + 脏 bot 集合, 由 EmotionManager 的周期任务批量落盘。
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from mohobot.file_store import json_read, json_write

from .models import EmotionalState
from .memory import MemorySystem

USER_STATES_FILE = "user_states.json"
MEMORY_FILE = "memory.json"


class EmotionStore:
    """per-bot 情感状态 + 记忆的加载/缓存/落盘。"""

    def __init__(self, base_dir: str, memory: MemorySystem) -> None:
        self._base = base_dir
        self._memory = memory
        # bot_id -> {user_key: EmotionalState}
        self._states: dict[str, dict[str, EmotionalState]] = {}
        self._dirty: set[str] = set()
        self._loaded: set[str] = set()

    def _bot_dir(self, bot_id: str) -> str:
        return f"{self._base}/{bot_id}"

    # ── 加载 ─────────────────────────────────────────────────

    async def ensure_loaded(self, bot_id: str) -> None:
        """首次访问某 bot 时从磁盘加载(幂等)。"""
        if bot_id in self._loaded:
            return
        self._loaded.add(bot_id)
        try:
            raw = await json_read(f"{self._bot_dir(bot_id)}/{USER_STATES_FILE}")
        except Exception as e:
            logger.warning(f"情感状态加载失败({bot_id}): {e}")
            raw = None
        states: dict[str, EmotionalState] = {}
        for user_key, data in (raw or {}).items():
            if isinstance(data, dict):
                try:
                    states[user_key] = EmotionalState.from_dict(data)
                except (TypeError, ValueError):
                    continue
        self._states[bot_id] = states

        try:
            mem = await json_read(f"{self._bot_dir(bot_id)}/{MEMORY_FILE}")
        except Exception as e:
            logger.warning(f"情感记忆加载失败({bot_id}): {e}")
            mem = None
        self._memory.load_bot(bot_id, mem if isinstance(mem, dict) else None)

    # ── 状态访问 ─────────────────────────────────────────────

    def get_state(self, bot_id: str, user_key: str) -> EmotionalState | None:
        return self._states.get(bot_id, {}).get(user_key)

    def set_state(self, bot_id: str, user_key: str, state: EmotionalState) -> None:
        self._states.setdefault(bot_id, {})[user_key] = state
        self.mark_dirty(bot_id)

    def all_states(self, bot_id: str) -> dict[str, EmotionalState]:
        return dict(self._states.get(bot_id, {}))

    def remove_user(self, bot_id: str, user_key: str) -> bool:
        states = self._states.get(bot_id, {})
        if user_key in states:
            del states[user_key]
            self.mark_dirty(bot_id)
            return True
        return False

    def clear_bot(self, bot_id: str) -> None:
        self._states[bot_id] = {}
        self._memory.clear_bot(bot_id)
        self.mark_dirty(bot_id)

    def mark_dirty(self, bot_id: str) -> None:
        self._dirty.add(bot_id)

    def touch_memory_dirty(self, bot_id: str) -> None:
        self._dirty.add(bot_id)

    # ── 落盘 ─────────────────────────────────────────────────

    async def save(self, bot_id: str) -> None:
        """把该 bot 的状态 + 记忆写盘(内存中的就是权威版本)。"""
        states = self._states.get(bot_id, {})
        payload = {k: s.to_dict() for k, s in states.items()}
        await json_write(f"{self._bot_dir(bot_id)}/{USER_STATES_FILE}", payload)
        await json_write(
            f"{self._bot_dir(bot_id)}/{MEMORY_FILE}",
            self._memory.records_for_save(bot_id),
        )

    async def flush(self) -> None:
        """保存全部脏 bot(关闭/周期落盘用)。"""
        dirty = self._dirty
        self._dirty = set()
        for bot_id in dirty:
            try:
                await self.save(bot_id)
            except Exception as e:
                logger.warning(f"情感数据保存失败({bot_id}): {e}")
                self._dirty.add(bot_id)

    def stats(self) -> dict[str, Any]:
        return {
            "bots_loaded": len(self._loaded),
            "dirty_bots": len(self._dirty),
            "users_total": sum(len(s) for s in self._states.values()),
        }


def now_ts() -> float:
    return time.time()
