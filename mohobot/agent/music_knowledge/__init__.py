"""旧模块兼容垫片 — 把旧 Agent 引用(per-bot)重定向到新全局模块。

替换 mohobot/agent/music_knowledge/(旧 flashtext/jargon + res 内置库) 之前,
临时保留此包: 现有引用(mohobot/agent/runtime.py 等)依然 import 这里,
但类已换成新数据层实现。新代码请直接使用 mohobot.music_knowledge。
"""

from __future__ import annotations

from mohobot.music_knowledge.song_knowledge import SongInfoService

__all__ = ["SongKnowledgeMemory"]

# 旧名: 运行时仍持有 song_knowledge = SongKnowledgeMemory(music_cfg)
# 新实现 = SongInfoService(keyword/linker 相关已移除)。
SongKnowledgeMemory = SongInfoService


class SongEntityLinker:
    """兼容占位(旧 FlashText 链接器已废弃)。

    消息预处理已改为全局 SongInfoMatcher(DB 直查), 不再用关键词文件;
    保留类名仅防止旧代码 import 崩溃。调用返回空列表。
    """

    def __init__(self, *args, **kwargs):
        pass

    def extract_and_verify(self, user_input: str | None) -> list[str]:
        return []