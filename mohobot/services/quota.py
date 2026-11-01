"""每日 LLM token 配额 — 群聊 / 私聊按日限额，超限不调用 LLM。

口径（维护者 2026-11-01 确认）：
- 计 ``total_tokens``（prompt + completion），含该会话所有 LLM 用途
  （chat / summary / vision / emotion 等，只要落在 usage 记录里）；
- **每日 0 点（UTC+8）重置**；
- 群聊按**群号**累计（跨 bot 共享一个额度）；
- 私聊按 **(bot_id, 用户 QQ)** 累计（换 bot 另算）；
- 管理员（``GlobalConfig.admins``）不受限；
- 触顶后由调用方直接回复提示、跳过 LLM（本模块只负责判定）。

数据源：``data/stats/llm_usage.jsonl``（UsageRecorder 落盘的调用记录）。
读取为**增量**方式：记住文件字节偏移与末尾半行，每次只解析新增行；
按当天 0 点过滤旧记录，所以跨天无需重扫历史。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.utils.time_utils import TZ_UTC8


class DailyTokenQuota:
    """按日统计各会话 token 用量并判定是否超限。"""

    def __init__(self, global_config: Any, usage_path: str | Path) -> None:
        self._cfg = global_config          # 共享 GlobalConfig(面板保存后原位更新)
        self._path = Path(usage_path)
        self._lock = asyncio.Lock()
        self._day: date | None = None
        self._totals: dict[tuple, int] = {}
        self._offset = 0
        self._carry = b""

    # ── 对外接口 ───────────────────────────────────────────

    async def check(self, bot_id: str, event: Any) -> str | None:
        """超限返回提示文本（调用方回复后跳过 LLM）；未超限返回 ``None``。"""
        cfg = getattr(self._cfg, "quota", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            return None

        admins = {str(a) for a in (getattr(self._cfg, "admins", None) or [])}
        if str(getattr(event, "user_id", "")) in admins:
            return None                    # 管理员豁免

        key = self.session_key(bot_id, event)
        limit = int(
            cfg.group_daily_tokens if key[0] == "group" else cfg.private_daily_tokens
        )
        if limit <= 0:
            return None                    # 0/负数视为不限

        async with self._lock:
            used = await asyncio.to_thread(self._snapshot, key)
        if used < limit:
            return None
        where = "本群" if key[0] == "group" else "本对话"
        logger.info(
            f"[quota] 触顶: {key} used={used} limit={limit} "
            f"(bot={bot_id}, user={getattr(event, 'user_id', '?')})"
        )
        return (
            f"📊 {where}今日额度已用完：已用 {used:,} / 上限 {limit:,} tokens，"
            f"明天 0 点（UTC+8）重置；管理员不受此限制。"
        )

    @staticmethod
    def session_key(bot_id: str, event: Any) -> tuple:
        """会话键: 群聊 ``("group", 群号)``；私聊 ``("private", bot_id, QQ)``。"""
        from mohobot.models.onebot import GroupMessageEvent

        if isinstance(event, GroupMessageEvent):
            return ("group", str(getattr(event, "group_id", "")))
        return ("private", str(bot_id), str(getattr(event, "user_id", "")))

    async def usage(self, bot_id: str, event: Any) -> tuple[int, int]:
        """返回 (今天已用 token, 当天上限)；未启用/不限返回 (used, 0)。"""
        cfg = getattr(self._cfg, "quota", None)
        if cfg is None:
            return 0, 0
        key = self.session_key(bot_id, event)
        limit = int(
            cfg.group_daily_tokens if key[0] == "group" else cfg.private_daily_tokens
        )
        async with self._lock:
            used = await asyncio.to_thread(self._snapshot, key)
        return used, limit

    def used_tokens(self, key: tuple) -> int:
        """同步查询某会话当天已用 token（测试/诊断用）。"""
        self._refresh()
        return self._totals.get(key, 0)

    def reset_cache(self) -> None:
        """清空内存统计与读取偏移（测试用）。"""
        self._day = None
        self._totals = {}
        self._offset = 0
        self._carry = b""

    # ── 内部实现 ───────────────────────────────────────────

    def _snapshot(self, key: tuple) -> int:
        self._refresh()
        return self._totals.get(key, 0)

    def _refresh(self) -> None:
        now = datetime.now(TZ_UTC8)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._day != day_start.date():
            # 跨天: 只需清空累计；偏移保留，跨天前的记录会被 cutoff 过滤掉
            self._day = day_start.date()
            self._totals = {}

        if not self._path.exists():
            return
        try:
            with self._path.open("rb") as fh:
                fh.seek(self._offset)
                data = fh.read()
        except OSError as e:  # 文件被删除/占用: 本轮按已知累计判定
            logger.debug(f"[quota] 读取 usage 失败: {e}")
            return
        if not data:
            return

        buf = self._carry + data
        lines = buf.split(b"\n")
        carry = lines.pop()                # 末尾半行留到下次
        self._carry = carry
        self._offset += len(data) - len(carry)

        cutoff = day_start.timestamp()
        for raw in lines:
            rec = self._parse_line(raw)
            if rec is None:
                continue
            if rec["ts"] < cutoff:
                continue
            key = rec["key"]
            if key is None or rec["tokens"] <= 0:
                continue
            self._totals[key] = self._totals.get(key, 0) + rec["tokens"]

    @staticmethod
    def _parse_line(raw: bytes) -> dict | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None
        if not isinstance(rec, dict):
            return None
        try:
            ts = float(rec.get("time") or 0)
        except (TypeError, ValueError):
            return None

        chat_type = str(rec.get("chat_type") or "")
        if chat_type == "group":
            key: tuple | None = ("group", str(rec.get("chat_id") or ""))
        elif chat_type == "private":
            key = (
                "private",
                str(rec.get("bot_id") or ""),
                str(rec.get("chat_id") or rec.get("user_id") or ""),
            )
        else:
            key = None

        try:
            tokens = int(rec.get("total_tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0
        if tokens <= 0:
            try:
                tokens = int(rec.get("prompt_tokens") or 0) + int(
                    rec.get("completion_tokens") or 0
                )
            except (TypeError, ValueError):
                tokens = 0
        return {"ts": ts, "key": key, "tokens": tokens}
