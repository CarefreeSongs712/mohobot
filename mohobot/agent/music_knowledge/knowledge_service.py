"""歌曲知识查询 — 移植自 Agent-LuoTianyi (knowledge_service.py)。

查询全部走这里:
- get_song_introduction / get_song_lyrics: 精确匹配优先(name/safe_name 相等), 最后兜底 ilike 模糊
- get_songs_by_uploader: 查某个 UP 主的作品
- get_random_songs_by_singer: 随机返回歌手演唱的歌(singers 逗号分隔, ilike 模糊)
- search_songs_by_lyrics: 歌词片段 LIKE 搜索("唱了句歌词但不知道歌名"场景)
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from mohobot.agent.music_knowledge.song_database import Song


def _escape_like(val: str) -> str:
    """转义 SQL LIKE 通配符 % 和 _"""
    return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query_by_name(db: Session, song_name: str):
    """精确匹配优先, 兜底 ilike 模糊(名字可能带特殊字符/全角差异)。"""
    return db.query(Song).filter(
        (Song.name == song_name) |
        (Song.safe_name == song_name) |
        (Song.name.ilike(f"%{_escape_like(song_name)}%", escape="\\"))
    ).first()


def get_song_introduction(db: Session, song_name: str) -> Optional[str]:
    """
    根据歌名查询歌曲介绍 (Summary)
    必须完全匹配歌名或安全歌名，模糊匹配可能会返回错误的介绍
    """
    song = _query_by_name(db, song_name)
    return song.introduction if song else None


def get_song_lyrics(db: Session, song_name: str) -> Optional[str]:
    """
    根据歌名查询歌词
    必须完全匹配歌名，模糊匹配可能会返回错误的歌词
    """
    song = _query_by_name(db, song_name)
    return song.lyrics if song else None


def get_songs_by_uploader(db: Session, uploader_name: str) -> List[str]:
    """
    给定人名查询创作者（UP主）创作的歌曲(ilike 模糊匹配)
    """
    songs = db.query(Song).filter(
        Song.uploader.ilike(f"%{_escape_like(uploader_name)}%", escape="\\")
    ).all()
    return [song.name for song in songs]


def get_random_songs_by_singer(db: Session, singer_name: str, n: int = 1) -> List[str]:
    """
    给定歌手名，随机返回n个这个歌手唱的歌
    """
    # 查找包含该歌手的歌曲; singers字段可能包含多个歌手(逗号/换行分隔), ilike 模糊匹配
    songs = db.query(Song).filter(
        Song.singers.ilike(f"%{_escape_like(singer_name)}%", escape="\\")
    ).all()
    if not songs:
        return []
    selected = songs if len(songs) <= n else random.sample(songs, n)
    return [song.name for song in selected]


def get_song_info(db: Session, song_name: str) -> Dict[str, str]:
    """
    获取歌曲完整信息辅助函数
    """
    song = _query_by_name(db, song_name)
    if song:
        return {
            "name": song.name,
            "uploader": song.uploader,
            "singers": song.singers,
            "introduction": song.introduction,
            "lyrics": song.lyrics,
        }
    return {}


def search_songs_by_lyrics(db: Session, lyrics_snippet: str) -> List[str]:
    """
    根据歌词片段搜索歌曲
    """
    songs = db.query(Song).filter(
        Song.lyrics.ilike(f"%{_escape_like(lyrics_snippet)}%", escape="\\")
    ).all()
    return [song.name for song in songs]
