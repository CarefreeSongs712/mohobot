"""Mohobot 占卜插件 — 每日一次, 结果持久化到 data/divination.json。

触发词: /占卜、占卜、今日占卜
昵称通过框架 WSServer.get_nickname 获取(群名片 → QQ 昵称 → 数字)。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

from loguru import logger

TRIGGERS = {"/占卜", "占卜", "今日占卜"}

_FIELDS = ["财运", "事业", "姻缘", "健康", "出行", "学业", "运气"]


class Plugin:
    """Responds to divination requests — once per day per user."""

    # 全局指令: 群内多 bot 时只由 bot_id 最小者回复(框架去重)
    global_triggers = TRIGGERS
    # 无前缀触发: 群聊不 @ 直接发"占卜/今日占卜"也可触发(框架观察钩子精确匹配)
    no_prefix_triggers = {"占卜", "今日占卜"}

    info = {
        "commands": [
            {"name": "占卜", "desc": "每日一次人品占卜(财运/事业/姻缘等)"},
        ],
    }

    _ws_server = None
    _data_dir = "./data"

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    def _records_path(self) -> Path:
        return Path(self._data_dir) / "divination.json"

    def _load_records(self) -> dict[str, dict]:
        try:
            path = self._records_path()
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取占卜记录失败: {e}")
        return {}

    def _save_records(self, records: dict[str, dict]) -> None:
        try:
            path = self._records_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"保存占卜记录失败: {e}")

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        # 提取纯文本
        text = ""
        if isinstance(event.message, str):
            text = event.message.strip()
        elif isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
            text = text.strip()

        if text not in TRIGGERS:
            return (False, None)

        user_id = str(event.user_id)
        nickname = await self._get_nickname(bot_id, event, user_id)
        from mohobot.utils.time_utils import format_utc8
        today = format_utc8("%Y-%m-%d")

        records = self._load_records()
        record = records.get(user_id)
        if record and record.get("date") == today:
            return (True, f"{nickname}, 今天已经占卜过了哦~\n结果如下:\n{record.get('result', '')}")

        result = self._generate_result(nickname)
        records[user_id] = {"date": today, "result": result}
        self._save_records(records)
        return (True, result)

    async def _get_nickname(self, bot_id: str, event: Any, user_id: str) -> str:
        """通过框架 get_nickname 获取昵称(群名片 → QQ 昵称 → 数字)。"""
        ws = self._ws_server
        if ws is None:
            return user_id
        group_id = None
        if hasattr(event, "group_id") and event.group_id:
            group_id = event.group_id
        try:
            return await ws.get_nickname(bot_id, user_id, group_id)
        except Exception as e:
            logger.warning(f"获取昵称失败, 使用数字: {e}")
            return user_id

    def _generate_result(self, nickname: str) -> str:
        lines = [f"@{nickname}, 这是你的占卜结果, 祝你生活快乐哦"]
        for field in _FIELDS:
            lines.append(f"{field}: {random.randint(1, 100)}")
        return "\n".join(lines)
