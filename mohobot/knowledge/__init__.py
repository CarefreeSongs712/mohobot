"""虚拟歌手知识库(RAG) — embedding 语义检索 + 可选 rerank。

与 music_knowledge(歌曲事实库, 关键词/歌词确定性匹配)互补:
本包把 data/knowledge_db 下的 markdown 条目与 index.json 档案切块、
embedding 入库(SQLite), 通过 knowledge_search / knowledge_lookup
两个 LLM 工具按需检索。未启用/初始化失败时整体降级, 正常聊天不受影响。
"""

from mohobot.knowledge.chunker import Chunk, chunk_source_dir, scan_source_dir
from mohobot.knowledge.service import KnowledgeService

__all__ = [
    "Chunk", "chunk_source_dir", "scan_source_dir", "KnowledgeService",
]
