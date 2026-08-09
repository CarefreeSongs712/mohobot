"""歌曲知识门面 — 移植自 Agent-LuoTianyi (memory/song_knowledge.py)。

SongKnowledgeMemory.search_song_facts_for_topic:
普通歌名约束 → 查 SQLite, 返回《歌名》的介绍/歌词去重文本。
同时负责把 git 内置默认知识库(res/song_knowledge/)复制到 data 目录
(运行时若 data 下不存在, 自动复制默认; 已存在则使用现有数据)。
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any, List

from loguru import logger

from mohobot.agent.music_knowledge.knowledge_service import (
    get_song_introduction,
    get_song_lyrics,
)
from mohobot.agent.music_knowledge.song_database import (
    Song,
    get_song_session,
    init_song_db,
)

# git 内置默认知识库(上传到仓库, 不放 data/)
_DEFAULT_RES_DIR = Path(__file__).resolve().parents[3] / "res" / "song_knowledge"


class SongKnowledgeMemory:
    """Memory-facing facade for song facts and lyrics knowledge."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._ensure_default_files()
        self._init_song_database()

    # ── 默认知识库复制 ──────────────────────────────────────────

    def _ensure_default_files(self) -> None:
        """data 目录下不存在知识库时, 复制 git 内置默认(res/song_knowledge/)。

        只复制缺失的文件, 不覆盖用户已有数据。
        """
        song_db_cfg = self.config.get("song_database") or {}
        db_folder = Path(song_db_cfg.get("db_folder", "./data/song_knowledge"))
        db_file = song_db_cfg.get("db_file", "knowledge_db.db")
        data_dir = db_folder
        data_dir.mkdir(parents=True, exist_ok=True)

        cfg_songname = (self.config.get("songname_file") or "").strip()
        cfg_lyric = (self.config.get("lyric_file") or "").strip()
        songname_file = Path(cfg_songname) if cfg_songname else data_dir / "song_name_keywords.txt"
        lyric_file = Path(cfg_lyric) if cfg_lyric else data_dir / "song_lyric_keywords.txt"

        targets = [
            (data_dir / db_file, _DEFAULT_RES_DIR / "knowledge_db.db"),
            (songname_file, _DEFAULT_RES_DIR / "song_name_keywords.txt"),
            (lyric_file, _DEFAULT_RES_DIR / "song_lyric_keywords.txt"),
        ]
        for target, default in targets:
            if target.exists():
                continue
            if not default.exists():
                logger.warning(f"默认知识库文件缺失: {default}")
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(default, target)
                logger.info(f"知识库: 已复制默认文件 {default.name} → {target}")
            except Exception as e:
                logger.error(f"知识库复制失败 {default.name}: {e}")

    def _init_song_database(self) -> None:
        song_db_config = self.config.get("song_database") or {}
        if not song_db_config:
            song_db_config = {
                "db_folder": "./data/song_knowledge",
                "db_file": "knowledge_db.db",
            }
        init_song_db(song_db_config)

    # ── 检索 ───────────────────────────────────────────────────

    async def search_song_facts_for_topic(self, constraints: List[str]) -> List[str]:
        """歌名约束 → 介绍/歌词去重文本。"""
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

    async def get_song_lyrics_text(self, song_name: str) -> str:
        """点歌场景: 直接取歌词(无则空串)。"""
        song_name = self._extract_song_name(song_name or "")
        if not song_name:
            return ""
        db = get_song_session()
        try:
            return (await asyncio.to_thread(get_song_lyrics, db, song_name)) or ""
        finally:
            db.close()

    async def get_random_song_with_lyrics(self) -> tuple[str, str]:
        """随机抽一首有歌词的歌, 返回 (歌名, 歌词)。无则 ("", "")。"""
        db = get_song_session()
        try:
            from sqlalchemy import func

            song = (
                db.query(Song)
                .filter(Song.lyrics != "", Song.lyrics.isnot(None))
                .order_by(func.random())
                .first()
            )
            if song is None:
                return ("", "")
            return song.name, song.lyrics or ""
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
