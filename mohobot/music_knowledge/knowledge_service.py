"""歌曲知识查询 — 全局歌曲识别/事实库查询(重写)。

查询全部走这里:
- get_song_detail: 精确匹配优先(name/safe_name 相等), 兜底 ilike 模糊, 返回完整字段
- search_songs_by_lyrics: 歌词片段包含匹配(识别"唱了句歌词但不知道歌名"场景)
- 歌词检索为线性扫描(库为本地静态数据, 数千首量级, 无索引亦可接受)。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from mohobot.music_knowledge.song_database import Song

# 需要随详情返回的人员/创作字段(注解格式化用)
CREDIT_KEYS = (
    "lyricist", "composer", "arranger", "mixer", "tuner",
    "mastering", "pv", "illustrator",
)


def _escape_like(val: str) -> str:
    """转义 SQL LIKE 通配符 % 和 _"""
    return val.replace("%", "\\%").replace("_", "\\_")


def safe_name_of(name: str) -> str:
    """规范化歌名(与爬虫入库一致): 保留字母数字与空格/-/_。"""
    return "".join(c for c in (name or "") if c.isalnum() or c in (" ", "-", "_")).strip()


def _query_by_name(db: Session, song_name: str) -> Optional[Song]:
    """精确匹配优先(含 safe_name), 兜底包含匹配。

    顺序: 1) 全等(name/safe_name) 2) 包含(like)。SQLite 的 OR 顺序不保证走
    左侧精确分支, 因此把"全等"与"包含"分成两次查询, 确保精确优先。
    """
    safe = safe_name_of(song_name)
    exact = (
        db.query(Song)
        .filter((Song.name == song_name) | (Song.safe_name == safe) | (Song.name == safe))
        .first()
    )
    if exact is not None:
        return exact
    return (
        db.query(Song)
        .filter(Song.name.like(f"%{_escape_like(song_name)}%"))
        .order_by(Song.name == song_name, Song.name)
        .first()
    )


def get_song_detail(db: Session, song_name: str) -> Dict[str, str]:
    """按歌名取完整歌曲信息(含 credits/介绍/歌词); 未命中返回空 dict。"""
    if not song_name:
        return {}
    song = _query_by_name(db, song_name)
    if song is None:
        return {}
    return {
        "name": song.name,
        "uploader": song.uploader or "",
        "singers": song.singers or "",
        "lyricist": song.lyricist or "",
        "composer": song.composer or "",
        "arranger": song.arranger or "",
        "mixer": song.mixer or "",
        "tuner": song.tuner or "",
        "mastering": song.mastering or "",
        "pv": song.pv or "",
        "illustrator": song.illustrator or "",
        "year": str(song.year) if song.year else "",
        "introduction": song.introduction or "",
        "lyrics": song.lyrics or "",
    }


def get_song_introduction(db: Session, song_name: str) -> Optional[str]:
    """歌曲介绍(旧接口兼容; 事实检索用)。"""
    return get_song_detail(db, song_name).get("introduction") or None


def get_song_lyrics(db: Session, song_name: str) -> Optional[str]:
    """歌曲歌词(旧接口兼容)。"""
    return get_song_detail(db, song_name).get("lyrics") or None


def search_songs_by_lyrics(db: Session, lyrics_snippet: str, limit: int = 5) -> List[str]:
    """根据歌词片段搜索歌曲(包含匹配, 线性扫描; 空片段返回空)。"""
    snippet = (lyrics_snippet or "").strip()
    if len(snippet) < 4:
        return []
    songs = (
        db.query(Song.name)
        .filter(Song.lyrics.ilike(f"%{_escape_like(snippet)}%", escape="\\"))
        .limit(limit)
        .all()
    )
    return [name for (name,) in songs]


def list_all_songs(db: Session) -> List[Dict[str, str]]:
    """全量歌曲列表(name + year), 供匹配器加载到内存 / 迁移 / 统计。"""
    rows = db.query(Song.name, Song.year).all()
    return [{"name": name, "year": year} for name, year in rows]


def count_songs(db: Session) -> int:
    return db.query(Song).count()