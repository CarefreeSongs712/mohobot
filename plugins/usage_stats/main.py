"""Mohobot token 用量统计插件 — 管理员查看 LLM 调用消耗。

用法(管理员):
  /用量            今日用量(各 bot 消耗 + 调用次数 + 平均每条 token + 按调用类型分类)
  /用量 7d         近 7 天用量

数据源: data/stats/llm_usage.jsonl(LLMModule 与 LLMService 统一写入)。
调用类型(module): main_chat(主回复) / topic_extractor(话题提取) /
memory_writer(记忆写入) / user_profile_updater(用户画像) / chat(旧路径对话) /
vision(视觉识别) / summarize(上下文总结) / 其他(旧记录无 module)。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

TRIGGERS = {"/用量"}

# module 展示名
_MODULE_NAMES = {
    "main_chat": "主回复",
    "topic_extractor": "话题提取",
    "memory_writer": "记忆写入",
    "user_profile_updater": "用户画像",
    "chat": "旧路径对话",
    "vision": "视觉识别",
    "summarize": "上下文总结",
}


class Plugin:
    """用量统计: /用量 [7d] — 按 bot × 调用类型汇总。"""

    global_triggers = TRIGGERS

    info = {
        "commands": [
            {"name": "用量", "desc": "用量 [7d] — 查看今日/近7天 token 消耗统计(管理员)"},
        ],
    }

    _ws_server = None
    _data_dir = "./data"
    _admin_ids: list[str] = []

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    @classmethod
    def inject_admin_ids(cls, admin_ids: list[int] | None) -> None:
        cls._admin_ids = [str(a) for a in (admin_ids or [])]

    @staticmethod
    def _extract_text(event) -> str:
        if isinstance(event.message, str):
            return event.message.strip()
        text = ""
        if isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
        return text.strip()

    def _usage_path(self) -> Path:
        return Path(self._data_dir) / "stats" / "llm_usage.jsonl"

    def _load_records(self, since_ts: float) -> list[dict]:
        path = self._usage_path()
        if not path.exists():
            return []
        records = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("time", 0) >= since_ts:
                        records.append(rec)
        except OSError as e:
            logger.warning(f"读取用量记录失败: {e}")
        return records

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        text = self._extract_text(event)
        if text != "/用量" and not text.startswith("/用量 "):
            return (False, None)
        if str(event.user_id) not in set(self._admin_ids):
            return (True, "❌ 你没有权限查看用量统计。")

        args = text[len("/用量"):].strip().split()
        days = 7 if args and args[0] in ("7d", "7天", "近7天") else 1

        from mohobot.utils.time_utils import TZ_UTC8
        from datetime import datetime as _dt
        now = _dt.now(TZ_UTC8)
        if days == 1:
            since = _dt(now.year, now.month, now.day, tzinfo=TZ_UTC8).timestamp()
            title = "今日"
        else:
            since = _dt(now.year, now.month, now.day - 6, tzinfo=TZ_UTC8).timestamp()
            title = "近 7 天"

        records = self._load_records(since)
        if not records:
            return (True, f"📊 {title}没有用量记录。")

        # 汇总: 按 bot → 总 token/调用次数/按 module
        bots: dict[str, dict] = {}
        totals = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
        for rec in records:
            bid = str(rec.get("bot_id", "?"))
            module = str(rec.get("module", "") or "其他")
            prompt = int(rec.get("prompt_tokens", 0) or 0)
            completion = int(rec.get("completion_tokens", 0) or 0)
            total = int(rec.get("total_tokens", 0) or 0)
            if total == 0:
                total = prompt + completion
            b = bots.setdefault(bid, {
                "prompt": 0, "completion": 0, "total": 0, "calls": 0,
                "modules": {},
            })
            b["prompt"] += prompt
            b["completion"] += completion
            b["total"] += total
            b["calls"] += 1
            m = b["modules"].setdefault(module, {"calls": 0, "total": 0})
            m["calls"] += 1
            m["total"] += total
            totals["prompt"] += prompt
            totals["completion"] += completion
            totals["total"] += total
            totals["calls"] += 1

        lines = [f"📊 {title} LLM 用量统计"]
        lines.append(
            f"总计: {totals['total']:,} token "
            f"(输入 {totals['prompt']:,} / 输出 {totals['completion']:,}), "
            f"{totals['calls']} 次调用, "
            f"平均每次 {totals['total'] // max(1, totals['calls']):,} token"
        )
        lines.append("")
        for bid in sorted(bots):
            b = bots[bid]
            avg = b["total"] // max(1, b["calls"])
            lines.append(
                f"🤖 {bid}: {b['total']:,} token, {b['calls']} 次, "
                f"平均每条 {avg:,} token"
            )
            for mod in sorted(b["modules"], key=lambda m: -b["modules"][m]["total"]):
                m = b["modules"][mod]
                name = _MODULE_NAMES.get(mod, mod)
                lines.append(
                    f"  · {name}: {m['total']:,} token, {m['calls']} 次"
                )
        return (True, "\n".join(lines))
