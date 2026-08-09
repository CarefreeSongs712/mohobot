"""歌曲知识包 — 移植自 Agent-LuoTianyi (server/src/subconscious/music_knowledge/)。

包含: SQLite 事实库(songs 表)、FlashText 关键词链接器、知识检索门面。
数据文件由用户提供(knowledge_db.db)或通过 VCPedia 手动同步生成。
"""

from mohobot.agent.music_knowledge.jargon import SongEntityLinker
from mohobot.agent.music_knowledge.song_knowledge import SongKnowledgeMemory

__all__ = ["SongEntityLinker", "SongKnowledgeMemory"]
