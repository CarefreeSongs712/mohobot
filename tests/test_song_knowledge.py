"""歌曲知识测试(重写: 全局库 + 识别 + LLM 前注入)。

覆盖:
1. 新 schema 建库/增删查(get_song_detail 完整字段 / 歌词片段搜索)
2. SongInfoMatcher 歌名/书名号/裸文本+语境词/歌词子串/防误伤
3. 注解格式化(【歌曲信息】段含介绍/演唱/UP主/词曲/歌词)
4. Legacy 路径注入: LLMService._build_messages 用户消息下方追加注解
5. Agent 路径注入: payload["song_annotation"] → UnreadMessage → prompt
6. 删除唱歌: 无 [sing] 解析 / 无 sing_plan
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 隔离全局 SQLite engine 状态, 每个测试用独立临时目录(避免类级 engine 复用冲突)
_TMP_DIRS = []


def make_config(tmp: str) -> dict:
    """构造指向临时 data 目录的 music_knowledge 配置(新 schema 空库)。"""
    data_dir = os.path.join(tmp, "data", "song_knowledge")
    return {
        "enabled": True,
        "song_database": {"db_folder": data_dir, "db_file": "knowledge_db.db"},
    }


def seed_songs(tmp: str) -> None:
    """向临时库写入测试歌曲(走 SongInfoService 初始化新 schema)。"""
    from mohobot.music_knowledge import SongInfoService
    from mohobot.music_knowledge.song_database import get_song_session, Song

    cfg = make_config(tmp)
    SongInfoService(cfg)  # 建库
    db = get_song_session()
    db.add(Song(
        name="千年食谱颂", safe_name="千年食谱颂", uploader="H.K.君", singers="洛天依",
        lyricist="青柠", composer="Wing翼", arranger="Wing翼", mixer="某人", tuner="某某",
        year=2017, introduction="一首关于美食与温馨的歌。", lyrics="小笼包啊小笼包\n好吃得不得了\n一口一个香",
    ))
    db.add(Song(
        name="九九八十一", safe_name="九九八十一", uploader="康师傅の海鲜面", singers="洛天依",
        introduction="洛天依名曲。", lyrics="刚擒住了几个妖\n又降住了几个魔",
    ))
    db.add(Song(
        name="赤伶", safe_name="赤伶", uploader="HITA", singers="洛天依",
        introduction="戏腔名曲。", lyrics="戏一折 水袖起落\n唱悲欢唱离合",
    ))
    db.commit()
    db.close()


async def test_schema_and_query() -> None:
    """新 schema: 建库 + 完整字段查询 + 歌词片段搜索。"""
    from mohobot.music_knowledge import SongInfoService, get_song_detail, search_songs_by_lyrics
    from mohobot.music_knowledge.song_database import get_song_session

    tmp = tempfile.mkdtemp(prefix="songlib_")
    _TMP_DIRS.append(tmp)
    cfg = make_config(tmp)
    SongInfoService(cfg)
    from mohobot.music_knowledge.song_database import Song
    db = get_song_session()
    s = Song(name="测试歌", safe_name="测试歌", uploader="UP", singers="歌手",
             lyricist="词", composer="曲", arranger="编", mixer="混", tuner="调",
             introduction="简介", lyrics="歌词第一行\n歌词第二行", year=2024)
    db.add(s)
    db.commit()
    db.close()

    db = get_song_session()
    detail = await asyncio.to_thread(get_song_detail, db, "测试歌")
    assert detail["name"] == "测试歌"
    assert detail["lyricist"] == "词" and detail["composer"] == "曲"
    assert detail["arranger"] == "编" and detail["mixer"] == "混" and detail["tuner"] == "调"
    assert detail["year"] == "2024"
    assert "\n" in detail["lyrics"], "歌词应保留换行"

    hits = await asyncio.to_thread(search_songs_by_lyrics, db, "歌词第二行")
    assert hits == ["测试歌"], hits
    db.close()
    print("[1] 新 schema 建库/查询 OK")


async def test_matcher() -> None:
    """SongInfoMatcher: 书名号/裸文本+语境/歌词子串/防误伤。"""
    from mohobot.music_knowledge import SongInfoMatcher

    tmp = tempfile.mkdtemp(prefix="songmatch_")
    _TMP_DIRS.append(tmp)
    seed_songs(tmp)
    cfg = make_config(tmp)
    m = SongInfoMatcher(db_folder=cfg["song_database"]["db_folder"],
                        db_file=cfg["song_database"]["db_file"])

    # 书名号 → 高置信
    r = m.match("你会唱《千年食谱颂》吗")
    assert r and r.name == "千年食谱颂"
    assert "歌曲信息" in r.build_annotation()
    assert "小笼包啊小笼包" in r.build_annotation()

    # 裸文本 + 语境词
    r2 = m.match("唱一首九九八十一")
    assert r2 and r2.name == "九九八十一"

    # 2 字歌名精确匹配(点歌前缀剥离后)允许; 1 字歌名仍拦截
    r2b = m.match("唱一首赤伶")
    assert r2b and r2b.name == "赤伶", r2b
    assert m.match("唱一首歌") is None  # 库里无 1 字歌命中, 且防"唱一首歌"误报
    assert m.match("晚上唱首歌吧") is None

    # lyrics 子串(换行在歌词库中, 消息空格)
    r3 = m.match("刚擒住了几个妖 又降住了几个魔")
    assert r3 and r3.name == "九九八十一"

    # 无语境裸文本 → 不误伤
    assert m.match("今天天气不错 九九八十一") is None

    # 普通消息 → None
    assert m.match("晚上吃什么呀") is None

    # 空输入
    assert m.match("") is None
    assert m.match(None) is None
    print("[2] 匹配器 OK")


async def test_annotation_format() -> None:
    """注解格式化: 介绍 + 演唱/UP主 + 词/曲/混/调 + 完整歌词。"""
    from mohobot.music_knowledge import SongInfoMatcher

    tmp = tempfile.mkdtemp(prefix="songann_")
    _TMP_DIRS.append(tmp)
    seed_songs(tmp)
    cfg = make_config(tmp)
    m = SongInfoMatcher(db_folder=cfg["song_database"]["db_folder"],
                        db_file=cfg["song_database"]["db_file"])
    r = m.match("你可以唱《千年食谱颂》吗")
    assert r is not None
    text = r.build_annotation()
    lines = text.split("\n")
    assert lines[0] == "【歌曲信息】消息中提到歌曲《千年食谱颂》"
    joined = "\n".join(lines)
    assert "歌曲介绍：" in joined and "一首关于美食" in joined
    assert "演唱：洛天依" in joined and "UP主：H.K.君" in joined
    assert "作词：青柠" in joined and "作曲：Wing翼" in joined
    assert "编曲：Wing翼" in joined and "混音：某人" in joined and "调教：某某" in joined
    assert "完整歌词：" in joined
    assert "小笼包啊小笼包" in joined
    print("[3] 注解格式化 OK")


async def test_legacy_injection() -> None:
    """Legacy 路径: LLMService._build_messages 在用户消息下方追加注解。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.config import GlobalConfig
    from mohobot.models.onebot import PrivateMessageEvent, GroupMessageEvent, Sender
    from mohobot.music_knowledge import SongInfoMatcher

    tmp = tempfile.mkdtemp(prefix="legacyinj_")
    _TMP_DIRS.append(tmp)
    seed_songs(tmp)
    cfg = make_config(tmp)
    m = SongInfoMatcher(db_folder=cfg["song_database"]["db_folder"],
                        db_file=cfg["song_database"]["db_file"])

    async def annotator(event):
        from mohobot.utils.cq_code import extract_plain_text
        text = (extract_plain_text(event.message) or "").strip()
        match = m.match(text) if text else None
        return match.build_annotation() if match else ""

    svc = LLMService(GlobalConfig(), song_annotator=annotator)

    ev = PrivateMessageEvent(
        time=0, self_id=0, post_type="message",
        message=[{"type": "text", "data": {"text": "你会唱《千年食谱颂》吗"}}],
        user_id=123456, message_id=1, sender=Sender(user_id=123456, nickname="测试"),
    )
    msgs = await svc._build_messages("bot_001", ev, context=[])
    user_msg = msgs[-1]["content"]
    assert "你会唱《千年食谱颂》吗" in user_msg
    # 注解在用户消息同一内容里(下方)
    assert "【歌曲信息】消息中提到歌曲《千年食谱颂》" in user_msg
    assert "完整歌词：" in user_msg

    # 无命中 → 无注解
    ev2 = PrivateMessageEvent(
        time=0, self_id=0, post_type="message",
        message=[{"type": "text", "data": {"text": "晚上吃什么呀"}}],
        user_id=123456, message_id=2, sender=Sender(user_id=123456, nickname="测试"),
    )
    msgs2 = await svc._build_messages("bot_001", ev2, context=[])
    assert "【歌曲信息】" not in msgs2[-1]["content"]

    # 群聊事件同样注入
    ev3 = GroupMessageEvent(
        time=0, self_id=0, post_type="message",
        message=[{"type": "text", "data": {"text": "唱一首九九八十一"}}],
        user_id=123456, message_id=3, group_id=555,
        sender=Sender(user_id=123456, nickname="测试"),
    )
    msgs3 = await svc._build_messages("bot_001", ev3, context=[])
    assert "《九九八十一》" in msgs3[-1]["content"]
    print("[4] Legacy 路径注入 OK")


async def test_agent_annotation_flow() -> None:
    """Agent 路径: 消息 → payload["song_annotation"] → UnreadMessage → 回复 prompt。"""
    from mohobot.agent.domain import ChatInputEvent, UnreadMessage
    from mohobot.music_knowledge import SongInfoMatcher

    tmp = tempfile.mkdtemp(prefix="agentinj_")
    _TMP_DIRS.append(tmp)
    seed_songs(tmp)
    cfg = make_config(tmp)
    m = SongInfoMatcher(db_folder=cfg["song_database"]["db_folder"],
                        db_file=cfg["song_database"]["db_file"])

    content = "你会唱《千年食谱颂》吗"
    match = m.match(content)
    assert match is not None
    song_annotation = match.build_annotation()
    terms = [f"《{match.name}》是一首歌"]

    # 模拟 message_handler._handle_agent_path
    ev = ChatInputEvent(
        event_type="user_message",
        user_id="123456",
        character_id="bot_001",
        content=content,
        terms=terms,
        payload={"speaker": "123456-测试", "song_annotation": song_annotation},
    )
    assert ev.payload["song_annotation"]

    # UnreadMessage 传递
    um = UnreadMessage(
        message_id="m1", content=content, terms=terms, speaker="123456-测试",
        song_annotation=song_annotation,
    )
    assert um.song_annotation == song_annotation
    print("[5] Agent 注解流转 OK")


async def test_no_sing_chain() -> None:
    """删除唱歌: 解析器不再解析 [sing], 无 sing_plan 字段。"""
    from mohobot.agent.main_chat import StructuredResponseParser
    from mohobot.agent.domain import TopicAttentionPlan

    parser = StructuredResponseParser()
    items = parser.parse("[中性]好呀\n[sing]千年食谱颂")
    # [sing] 行不产生回复对象(唱歌已移除)
    contents = [it.get_content() for it in items]
    assert contents == ["好呀"], contents

    # TopicAttentionPlan 无 sing_plan 字段
    import dataclasses
    fields = {f.name for f in dataclasses.fields(TopicAttentionPlan)}
    assert "sing_plan" not in fields
    assert "sing_attempts" not in {f.name for f in dataclasses.fields(
        __import__("mohobot.agent.domain", fromlist=["ExtractedTopic"]).ExtractedTopic)}
    print("[6] 唱歌链路已删除 OK")


async def test_legacy_schema_migration() -> None:
    """旧 songs 表缺 credits 列时, pool 初始化应原地补列并保留数据。"""
    import sqlite3
    from mohobot.music_knowledge.pool import close_all, ensure_init, get_session
    from mohobot.music_knowledge.song_database import Song

    tmp = tempfile.mkdtemp(prefix="songlegacy_")
    path = os.path.join(tmp, "old.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE songs (uuid TEXT PRIMARY KEY, name TEXT NOT NULL, safe_name TEXT NOT NULL, "
        "uploader TEXT, singers TEXT, introduction TEXT NOT NULL DEFAULT '', lyrics TEXT NOT NULL DEFAULT '')"
    )
    con.execute("INSERT INTO songs VALUES ('1','旧歌','旧歌','UP','歌手','简介','歌词')")
    con.commit()
    con.close()
    close_all()
    ensure_init(tmp, "old.db")
    with get_session() as db:
        song = db.query(Song).first()
        assert song.name == "旧歌" and song.lyricist is None and song.year is None
    close_all()
    print("[7] 旧歌曲库 schema 自动迁移 OK")


async def test_real_db_dialogue_cases() -> None:
    """真实大库存在时验证自然歌名和歌词问句; CI 无库时跳过。"""
    db_path = Path("data/song_knowledge/knowledge_db.db")
    if not db_path.exists():
        print("[8] 真实库不存在, 跳过自然对话用例")
        return
    from mohobot.music_knowledge import SongInfoMatcher
    from mohobot.music_knowledge.pool import close_all
    close_all()
    matcher = SongInfoMatcher(db_folder=str(db_path.parent), db_file=db_path.name)
    cases = {
        "你知道白鸟过河滩吗": "白鸟过河滩",
        "你是信的开头诗的内容这是哪首的？": "勾指起誓",
    }
    for text, expected in cases.items():
        match = matcher.match(text)
        assert match and match.name == expected, (text, match.name if match else None)
        assert "\n" in match.detail["lyrics"]
    close_all()
    print("[8] 真实库自然对话识别 OK")


async def main() -> None:
    await test_schema_and_query()
    await test_matcher()
    await test_annotation_format()
    await test_legacy_injection()
    await test_agent_annotation_flow()
    await test_no_sing_chain()
    await test_legacy_schema_migration()
    await test_real_db_dialogue_cases()
    print("\nALL SONG KNOWLEDGE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())