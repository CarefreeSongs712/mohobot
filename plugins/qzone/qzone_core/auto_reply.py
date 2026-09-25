"""说说自动回复（被@ / 自己说说被评论）— 存储与文本工具。

去重状态按 bot 隔离, 持久化于 data/plugins_data/qzone/auto_reply_{bot_id}.json:
  {"seen": [key...], "baselined": bool}
- seen: 已处理过的条目 key(评论/被@), 上限裁剪防膨胀
- baselined: 首轮基线标记 — 首次启用时把当前已存在的评论全部标记为已读,
  避免把历史评论全部回复一遍(与 welcome 插件首启基线同思路)
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from loguru import logger

from mohobot.file_store import json_read, json_write

_SEEN_MAX = 1000


class AutoReplyStore:
    """per-bot 自动回复去重状态(惰性加载, 显式保存)。"""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict = {}
        self._loaded = False

    async def _ensure(self) -> None:
        if self._loaded:
            return
        data = await json_read(self._path)
        self._data = data if isinstance(data, dict) else {}
        seen = self._data.get("seen")
        self._data["seen"] = [str(k) for k in seen] if isinstance(seen, list) else []
        self._loaded = True

    async def is_seen(self, key: str) -> bool:
        await self._ensure()
        return key in self._data.get("seen", [])

    async def mark_seen(self, key: str) -> None:
        await self._ensure()
        seen: list[str] = self._data.setdefault("seen", [])
        if key not in seen:
            seen.append(key)
        if len(seen) > _SEEN_MAX:
            del seen[: len(seen) - _SEEN_MAX]

    async def is_baselined(self) -> bool:
        await self._ensure()
        return bool(self._data.get("baselined", False))

    async def set_baselined(self) -> None:
        await self._ensure()
        self._data["baselined"] = True

    async def save(self) -> None:
        await self._ensure()
        try:
            await json_write(self._path, self._data)
        except Exception as e:
            logger.warning(f"[qzone] 自动回复状态保存失败: {e}")


def content_key(*parts) -> str:
    """由若干字段生成稳定去重 key(字段缺失时兜底哈希)。"""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def render_reply_prompt(template: str, *, nick: str, content: str, post: str) -> str:
    """渲染回复提示词模板({nick}/{content}/{post} 占位符)。"""
    text = template
    for key, val in (("nick", nick), ("content", content), ("post", post)):
        text = text.replace("{" + key + "}", str(val or ""))
    return text


def clean_reply_text(text: str, max_len: int) -> str:
    """清洗 LLM 生成回复: 去引号/换行折叠/去尾句号/截断(参考 ultra _clean_short_reply)。"""
    cleaned = re.sub(r"[\s\u3000]+", " ", str(text or "")).strip()
    cleaned = cleaned.strip("\"'“”‘’`")
    cleaned = cleaned.rstrip("。.")
    return cleaned[: max(1, int(max_len))]
