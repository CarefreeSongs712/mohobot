"""歌曲知识门面 — 全局歌曲识别/事实库门面(重写, 兼容旧接口)。

由 main.py 装配为全局 SongInfoService(注入 MessageHandler 与 LLMService),
legacy 与 agent 两条路径共用; 不再创建 per-bot 实例。
保留旧调用面(不含默认库复制):
- async search_song_facts_for_topic(constraints): 歌曲约束 → 介绍/歌词文本
- async get_song_lyrics_text(song_name): 取歌词(供事实检索/兼容)
- (删除 get_random_song_with_lyrics / _extract_song_name 点歌规划相关)
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, List

from loguru import logger

from mohobot.music_knowledge.knowledge_service import (
    get_song_introduction,
    get_song_lyrics,
)
from mohobot.music_knowledge.song_database import (
    get_song_session,
    init_song_db,
)


class SongInfoService:
    """全局歌曲知识门面(事实检索; 识别/注入走 SongInfoMatcher)。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._init_song_database()

    def _init_song_database(self) -> None:
        song_db_config = self.config.get("song_database") or {}
        if not song_db_config:
            song_db_config = {
                "db_folder": "./data/song_knowledge",
                "db_file": "knowledge_db.db",
            }
        try:
            init_song_db(song_db_config)
        except Exception as e:
            logger.warning(f"歌曲知识库初始化失败: {e}")

    # ── 检索 ───────────────────────────────────────────────────

    async def search_song_facts_for_topic(self, constraints: List[str]) -> List[str]:
        """歌曲约束 → 介绍/歌词去重文本(事实检索用)。"""
        if not constraints:
            return []

        db = get_song_session()
        try:
            dedup: List[str] = []
            seen = set()
            for raw in constraints:
                song_name = self._extract_song_name(raw)
                if not song_name:
                    continue

                intro = await asyncio.to_thread(get_song_introduction, db, song_name)
                lyrics = await asyncio.to_thread(get_song_lyrics, db, song_name)

                if intro:
                    text = f"《{song_name}》的介绍:\n{intro}"
                    if text not in seen:
                        seen.add(text)
                        dedup.append(text)

                if lyrics:
                    text = f"《{song_name}》的歌词:\n{lyrics}"
                    if text not in seen:
                        seen.add(text)
                        dedup.append(text)

            return dedup
        finally:
            db.close()

    @staticmethod
    def _extract_song_name(text: str) -> str:
        content = (text or "").strip()
        if not content:
            return ""

        match = re.search(r"《([^》]+)》", content)
        if match:
            return match.group(1).strip()

        if "是一首歌" in content:
            return content.split("是一首歌", 1)[0].strip().strip("《》")

        return content.strip("\"'“”‘’《》")