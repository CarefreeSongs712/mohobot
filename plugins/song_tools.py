"""VCPedia 歌曲知识工具: 暴露给 Legacy 与 Agent LLM 的只读函数。"""
from __future__ import annotations

import json
from typing import Any

from mohobot.music_knowledge import get_song_detail, get_song_lyrics, init_song_db
from mohobot.music_knowledge.knowledge_service import search_songs_by_lyrics
from mohobot.music_knowledge.pool import ensure_init, get_session
from mohobot.services.llm_tools import LLMTool, registry, tool_schema


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def song_search(query: str, limit: int = 5) -> str:
    """按歌曲名或歌词片段搜索。"""
    query = str(query or "").strip()
    limit = max(1, min(int(limit or 5), 10))
    if len(query) < 2:
        return _json({"error": "query 至少需要 2 个字符"})
    ensure_init("./data/song_knowledge", "knowledge_db.db")
    db = get_session()
    try:
        # 歌名查询优先，再补歌词命中，去重后返回简短结果。
        from mohobot.music_knowledge.song_database import Song
        names = [r[0] for r in db.query(Song.name).filter(Song.name.like(f"%{query}%")).limit(limit).all()]
        lyric_names = search_songs_by_lyrics(db, query, limit=limit)
        merged = list(dict.fromkeys(names + lyric_names))[:limit]
        return _json({"query": query, "songs": merged})
    finally:
        db.close()


def song_get_detail(song_name: str) -> str:
    """获取歌曲介绍、演唱、UP主及词曲编混调等信息。"""
    name = str(song_name or "").strip()
    if not name:
        return _json({"error": "song_name 不能为空"})
    ensure_init("./data/song_knowledge", "knowledge_db.db")
    db = get_session()
    try:
        detail = get_song_detail(db, name)
        if not detail:
            return _json({"error": "未找到歌曲", "song_name": name})
        detail.pop("lyrics", None)
        return _json(detail)
    finally:
        db.close()


def song_get_lyrics(song_name: str) -> str:
    """获取歌曲完整歌词(保留换行)。"""
    name = str(song_name or "").strip()
    if not name:
        return _json({"error": "song_name 不能为空"})
    ensure_init("./data/song_knowledge", "knowledge_db.db")
    db = get_session()
    try:
        lyrics = get_song_lyrics(db, name)
        if not lyrics:
            return _json({"error": "未找到歌词", "song_name": name})
        return _json({"song_name": name, "lyrics": lyrics})
    finally:
        db.close()


TOOLS = [
    LLMTool(tool_schema("song_search", "从 VCPedia 歌曲库按歌名或歌词片段搜索歌曲。", {
        "query": {"type": "string", "description": "歌名或至少 2 个字符的歌词片段"},
        "limit": {"type": "integer", "description": "最多返回数量，1-10", "minimum": 1, "maximum": 10},
    }, ["query"]), song_search),
    LLMTool(tool_schema("song_get_detail", "获取歌曲介绍、演唱、UP主和词曲编混调等信息。", {
        "song_name": {"type": "string", "description": "歌曲名称"},
    }, ["song_name"]), song_get_detail),
    LLMTool(tool_schema("song_get_lyrics", "获取歌曲完整歌词，歌词中的换行会被保留。", {
        "song_name": {"type": "string", "description": "歌曲名称"},
    }, ["song_name"]), song_get_lyrics),
]


def _register() -> None:
    for tool in TOOLS:
        if tool.name not in {schema["function"]["name"] for schema in registry.schemas()}:
            registry.register(tool)


_register()


class Plugin:
    """歌曲知识工具插件。工具为只读操作，不直接发送消息或修改数据。"""
    info = {"commands": []}
