"""歌曲知识测试(移植自 Agent-LuoTianyi 的 music_knowledge):
1. 默认知识库自动复制(res/song_knowledge/ → data/)
2. knowledge_service 查询(精确/safe_name/ilike 兜底/UP主/歌手随机/歌词片段)
3. SongEntityLinker 触发动词门控 + 歌名/歌词术语
4. search_song_facts_for_topic 去重文本
5. 点歌规划: 指定歌名取歌词 / random_song / 查不到
6. fact_constraints 分流: 歌曲约束 vs 联网查询
7. parser [sing] → SongSegmentChat(带歌词)
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_config(tmp: str) -> dict:
    """构造指向临时 data 目录的 music_knowledge 配置(默认库会自动复制)。"""
    data_dir = os.path.join(tmp, "data", "song_knowledge")
    return {
        "song_database": {"db_folder": data_dir, "db_file": "knowledge_db.db"},
        "songname_file": os.path.join(data_dir, "song_name_keywords.txt"),
        "lyric_file": os.path.join(data_dir, "song_lyric_keywords.txt"),
    }


async def test_default_copy_and_query() -> None:
    """默认知识库复制 + 各查询函数。"""
    from mohobot.agent.music_knowledge.knowledge_service import (
        get_random_songs_by_singer,
        get_song_introduction,
        get_song_lyrics,
        get_songs_by_uploader,
        search_songs_by_lyrics,
    )
    from mohobot.agent.music_knowledge.song_database import get_song_session
    from mohobot.agent.music_knowledge.song_knowledge import SongKnowledgeMemory

    tmp = tempfile.mkdtemp(prefix="songlib_")
    cfg = make_config(tmp)
    sk = SongKnowledgeMemory(cfg)

    # 默认文件已复制
    data_dir = os.path.join(tmp, "data", "song_knowledge")
    for f in ("knowledge_db.db", "song_name_keywords.txt", "song_lyric_keywords.txt"):
        assert os.path.exists(os.path.join(data_dir, f)), f"{f} 未复制"

    db = get_song_session()
    try:
        # 精确查询
        intro = await asyncio.to_thread(get_song_introduction, db, "千年食谱颂")
        assert intro and "洛天依" in intro, intro[:60]
        lyrics = await asyncio.to_thread(get_song_lyrics, db, "千年食谱颂")
        assert lyrics, "应有歌词"

        # safe_name 匹配(原名含特殊字符, safe_name 过滤后匹配)
        intro2 = await asyncio.to_thread(get_song_introduction, db, "（0，0）")
        assert intro2, "原名查询应命中"

        # 不存在的歌 → None
        assert await asyncio.to_thread(get_song_introduction, db, "不存在的歌XYZ") is None

        # UP主查询(ilike 模糊, uploader 可能是多人)
        uploader_songs = await asyncio.to_thread(get_songs_by_uploader, db, "H.K.君")
        assert "千年食谱颂" in uploader_songs, uploader_songs[:5]

        # 歌手随机
        singer_songs = await asyncio.to_thread(get_random_songs_by_singer, db, "洛天依", 3)
        assert len(singer_songs) == 3, singer_songs

        # 歌词片段搜歌
        snippet_songs = await asyncio.to_thread(
            search_songs_by_lyrics, db, "小笼包"
        )
        assert snippet_songs, "歌词片段应能搜到歌"
    finally:
        db.close()
    print("[1] 默认复制 + knowledge_service 查询 OK")


async def test_linker_gate() -> None:
    """FlashText 链接器: 触发动词门控。"""
    from mohobot.agent.music_knowledge.jargon import SongEntityLinker
    from mohobot.agent.music_knowledge.song_knowledge import SongKnowledgeMemory

    tmp = tempfile.mkdtemp(prefix="songlink_")
    cfg = make_config(tmp)
    SongKnowledgeMemory(cfg)  # 先复制默认知识库(链接器才能加载关键词文件)
    linker = SongEntityLinker(cfg)

    # 命中触发动词 → 歌名识别
    terms = linker.extract_and_verify("唱一首千年食谱颂")
    assert "《千年食谱颂》是一首歌" in terms, terms

    # 无触发动词 → 不激活(防日常误触发)
    assert linker.extract_and_verify("千年食谱颂今天天气不错") == []

    # 歌词片段术语(歌词关键词文件格式: 片段=>片段是《歌名》的歌词)
    lyric_terms = linker.extract_and_verify("我想听小笼包快点拿过来") or []
    assert lyric_terms, "应命中歌词术语"

    # 空输入
    assert linker.extract_and_verify("") == []
    print("[2] 链接器触发动词门控 OK")


async def test_song_facts_for_topic() -> None:
    """search_song_facts_for_topic 去重文本。"""
    from mohobot.agent.music_knowledge.song_knowledge import SongKnowledgeMemory

    tmp = tempfile.mkdtemp(prefix="songfact_")
    sk = SongKnowledgeMemory(make_config(tmp))

    hits = await sk.search_song_facts_for_topic(["《千年食谱颂》"])
    assert len(hits) == 2, hits  # 介绍 + 歌词
    assert "《千年食谱颂》的介绍:" in hits[0]
    assert "《千年食谱颂》的歌词:" in hits[1]

    # 无约束 → 空
    assert await sk.search_song_facts_for_topic([]) == []

    # 未知歌 → 空
    assert await sk.search_song_facts_for_topic(["《不存在的歌XYZ》"]) == []
    print("[3] search_song_facts_for_topic OK")


async def test_sing_plan() -> None:
    """点歌规划: 指定歌名取歌词 / random_song / 查不到 → (歌名, None)。"""
    from mohobot.agent.character_mind import CharacterSubconscious
    from mohobot.agent.music_knowledge.song_knowledge import SongKnowledgeMemory

    tmp = tempfile.mkdtemp(prefix="singplan_")
    sk = SongKnowledgeMemory(make_config(tmp))

    class FakeMind:
        def __init__(self):
            self.song_knowledge = sk

    mind = FakeMind()
    subconscious = CharacterSubconscious.__new__(CharacterSubconscious)
    subconscious.song_knowledge = sk
    subconscious.logger = __import__("loguru").logger

    # 指定歌名
    song, lyrics = await subconscious._plan_sing_attempts_for_topic(["千年食谱颂"])
    assert song == "千年食谱颂" and lyrics, (song, (lyrics or "")[:30])

    # 带《》+ 动词前缀
    song, lyrics = await subconscious._plan_sing_attempts_for_topic(["唱一首《九九八十一》"])
    assert song == "九九八十一" and lyrics

    # random_song
    song, lyrics = await subconscious._plan_sing_attempts_for_topic(["random_song"])
    assert song and lyrics

    # 查不到 → (歌名, None)
    song, lyrics = await subconscious._plan_sing_attempts_for_topic(["不存在的歌XYZ"])
    assert song == "不存在的歌XYZ" and lyrics is None

    # 空
    assert await subconscious._plan_sing_attempts_for_topic([]) == (None, None)
    print("[4] 点歌规划 OK")


async def test_fact_constraint_routing() -> None:
    """fact_constraints 分流: 歌曲约束 → 知识库; 其余 → 联网(未配置时降级)。"""
    from mohobot.agent.character_mind import CharacterSubconscious

    tmp = tempfile.mkdtemp(prefix="route_")
    from mohobot.agent.music_knowledge.song_knowledge import SongKnowledgeMemory

    sk = SongKnowledgeMemory(make_config(tmp))

    subconscious = CharacterSubconscious.__new__(CharacterSubconscious)
    subconscious.song_knowledge = sk
    subconscious.anysearch = None  # 未配置 → 联网降级为空
    subconscious.logger = __import__("loguru").logger

    hits = await subconscious.search_fact_constraints_for_topic(["《千年食谱颂》"])
    assert len(hits) == 2, hits  # 走知识库

    # 混合: 歌曲 + 联网查询(联网未配置 → 只返回歌曲命中)
    mixed = await subconscious.search_fact_constraints_for_topic(
        ["《千年食谱颂》", "洛天依最新动态"]
    )
    assert len(mixed) == 2, mixed

    # 空
    assert await subconscious.search_fact_constraints_for_topic([]) == []
    print("[5] fact_constraints 分流 OK")


async def test_parser_sing_line() -> None:
    """StructuredResponseParser: [sing] 行 → SongSegmentChat(带歌词)。"""
    from mohobot.agent.main_chat import StructuredResponseParser
    from mohobot.agent.domain import ContextType, SongSegmentChat

    parser = StructuredResponseParser()
    lines = "[中性]好呀，这就唱给你听~\n[sing]千年食谱颂"
    items = parser.parse(lines, ("千年食谱颂", "歌词文本XYZ"))
    assert len(items) == 2, items
    sing_item = items[1]
    assert isinstance(sing_item, SongSegmentChat)
    assert sing_item.song == "千年食谱颂"
    assert sing_item.lyrics == "歌词文本XYZ"
    assert "歌词文本XYZ" in sing_item.get_content()
    assert sing_item.type == ContextType.SING

    # 无 sing_plan → 空歌词但保留歌名
    items2 = parser.parse("[sing]别的歌", None)
    assert len(items2) == 1 and isinstance(items2[0], SongSegmentChat)
    assert items2[0].lyrics == ""

    # 纯文本行仍解析
    items3 = parser.parse("[欣喜]今天开心", None)
    assert items3[0].type == ContextType.TEXT
    print("[6] [sing] 解析 → SongSegmentChat OK")


async def main() -> None:
    await test_default_copy_and_query()
    await test_linker_gate()
    await test_song_facts_for_topic()
    await test_sing_plan()
    await test_fact_constraint_routing()
    await test_parser_sing_line()
    print("\nALL SONG KNOWLEDGE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
