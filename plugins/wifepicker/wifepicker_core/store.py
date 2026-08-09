"""合并 JSON 存储 — 原插件 5 个文件合并为 data/plugins_data/wifepicker/data.json。

结构:
  records:          {"date": "2026-08-09", "groups": {gid: {"records": [...]}}}   今日抽取记录(懒重置)
  active_users:     {gid: {uid: ts}}                                              活跃池(近 N 天发言)
  forced_marriage:  {gid: {uid: ts}}                                              强娶冷却(最后一次强娶时间戳)
  marriage_actions: {gid: {uid: {action, start_at, expire_at, ...}}}              求婚冷却(当天)
  rbq_stats:        {gid: {uid: [ts, ...]}}                                       被强娶统计(近 30 天)

并发安全: 所有修改持实例级 asyncio.Lock, 写盘走 file_store.json_update 原子读改写
(6 bot 并发指令不丢更新)。活跃记录高频 → 仅内存 + dirty 标记, 由后台维护任务
定期落盘; 命令级修改(抽取/强娶/求婚)立即落盘。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from mohobot.file_store import json_update

DEFAULT_DATA: dict[str, Any] = {
    "records": {"date": "", "groups": {}},
    "active_users": {},
    "forced_marriage": {},
    "marriage_actions": {},
    "rbq_stats": {},
}

# 活跃池落盘周期(秒)
SAVE_INTERVAL_SECONDS = 120
# 活跃池全量清理周期(秒)
TRIM_INTERVAL_SECONDS = 3600


class WifeStore:
    """抽老婆插件数据存储(内存为主 + 原子落盘)。"""

    def __init__(self, data_dir: str | Path):
        self._path = Path(data_dir) / "plugins_data" / "wifepicker" / "data.json"
        self._lock = asyncio.Lock()
        self.data: dict[str, Any] = self._load()
        self._dirty = False
        self._last_save_at = time.time()

    # ── 加载 ─────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return self._merge_defaults(raw)
        except Exception as e:
            logger.error(f"wifepicker 数据加载失败, 使用默认: {e}")
        return json.loads(json.dumps(DEFAULT_DATA))

    @staticmethod
    def _merge_defaults(raw: dict) -> dict:
        merged = json.loads(json.dumps(DEFAULT_DATA))
        for key in merged:
            if key in raw and isinstance(raw[key], type(merged[key])):
                merged[key] = raw[key]
        return merged

    # ── 写盘 ─────────────────────────────────────────────────

    def mark_dirty(self) -> None:
        self._dirty = True

    async def flush(self, *, force: bool = False) -> None:
        """原子写盘(锁 + json_update)。dirty 或 force 时执行。"""
        if not force and not self._dirty:
            return
        async with self._lock:
            try:
                await json_update(
                    self._path, lambda cur: self.data, default=self.data
                )
                self._dirty = False
                self._last_save_at = time.time()
            except Exception as e:
                logger.error(f"wifepicker 数据落盘失败: {e}")

    async def mutate(self, fn: Callable[[dict], None]) -> None:
        """锁内修改内存数据 + 立即原子写盘(命令级修改用)。"""
        async with self._lock:
            fn(self.data)
            try:
                await json_update(
                    self._path, lambda cur: self.data, default=self.data
                )
                self._dirty = False
                self._last_save_at = time.time()
            except Exception as e:
                logger.error(f"wifepicker 数据落盘失败: {e}")

    # ── 便捷访问 ─────────────────────────────────────────────

    @property
    def records(self) -> dict:
        return self.data["records"]

    @property
    def active_users(self) -> dict:
        return self.data["active_users"]

    @property
    def forced_marriage(self) -> dict:
        return self.data["forced_marriage"]

    @property
    def marriage_actions(self) -> dict:
        return self.data["marriage_actions"]

    @property
    def rbq_stats(self) -> dict:
        return self.data["rbq_stats"]
