"""歌曲知识包 — 全局(非 beta 专属)歌曲识别与事实库。

包含: 新 schema SQLite 事实库(songs 表)、SongInfoMatcher(歌名/歌词识别 +
注解格式化)、SongInfoService(事实检索门面)与重写后的 VCPedia 爬虫。
App 级(全局)组件, legacy 与 agent 两条 LLM 路径共用。
不再依赖 flashtext / res/song_knowledge/ 默认库文件。
"""

from mohobot.music_knowledge.knowledge_service import (
    get_song_detail,
    get_song_introduction,
    get_song_lyrics,
    search_songs_by_lyrics,
)
from mohobot.music_knowledge.matcher import SongInfoMatcher, SongMatch
from mohobot.music_knowledge.pool import close_all, ensure_init, get_session
from mohobot.music_knowledge.song_database import (
    Song,
    SongStats,
    get_song_db,
    get_song_session,
    init_song_db,
    update_song_stats,
)
from mohobot.music_knowledge.song_knowledge import SongInfoService

__all__ = [
    "Song",
    "SongStats",
    "init_song_db",
    "get_song_db",
    "get_song_session",
    "update_song_stats",
    "SongInfoService",
    "SongInfoMatcher",
    "SongMatch",
    "get_song_detail",
    "get_song_introduction",
    "get_song_lyrics",
    "search_songs_by_lyrics",
    "pool",
]