"""多来源歌曲知识库测试: 归一化、合并、同步断点、模糊匹配、注入预算。

全部使用临时库与假适配器, 不访问网络、不读写业务 data/。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIRS: list[str] = []


def _fresh_db():
    """建临时库并返回 (folder, file)。"""
    from mohobot.music_knowledge import pool
    from mohobot.music_knowledge.song_database import Song, SongStats

    folder = tempfile.mkdtemp(prefix="songsync_")
    _TMP_DIRS.append(folder)
    pool.ensure_init(folder, "test.db")
    db = pool.get_session()
    try:
        db.query(Song).delete()
        db.query(SongStats).delete()
        db.commit()
    finally:
        db.close()
    return folder, "test.db"


def _record(**kwargs):
    from mohobot.music_knowledge.sources.base import SourceRecord

    kwargs.setdefault("source", "moegirl")
    kwargs.setdefault("source_title", kwargs.get("name", "x"))
    return SourceRecord(**kwargs)


class _FakeAdapter:
    """最小来源适配器: 从内存字典发记录。"""

    license = "test"
    default_interval = 0.0
    max_retries = 0

    def __init__(self, name, records):
        self.name = name
        self.records = records
        self.fetched = []

    def list_titles(self, *, limit=None):
        titles = list(self.records)
        return titles[:limit] if limit else titles

    def fetch(self, title):
        self.fetched.append(title)
        value = self.records[title]
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        return None


async def test_normalize_name() -> None:
    """名称归一化: 标点/大小写/feat/括号核名与版本分类。"""
    from mohobot.music_knowledge.normalize import (
        bracket_core,
        classify_suffix,
        detect_language,
        extract_bvid,
        normalize_name,
        split_singers,
    )

    assert normalize_name("《千本樱》") == "千本樱"
    assert normalize_name("Butterflies") == normalize_name("butterflies")
    assert normalize_name("Song feat. 洛天依") == "song"
    assert bracket_core("九九八十一(乐正绫)") == "九九八十一"
    assert bracket_core("无括号") == "无括号"
    assert classify_suffix("X(中文版)") == "version"
    assert classify_suffix("X(洛天依)") == "disambiguation"
    assert detect_language("这是中文歌词内容") == "zh"
    assert detect_language("これは日本語の歌詞です") == "ja"
    assert split_singers("洛天依、言和 / 乐正绫") == ["洛天依", "言和", "乐正绫"]
    assert extract_bvid("https://www.bilibili.com/video/BV1Vs411H7JH") == "BV1Vs411H7JH"
    print("[1] 归一化 OK")


async def test_merge_create_and_fill() -> None:
    """合并: 新建 → 其它来源只补空字段, 不覆盖高优先级来源。"""
    from mohobot.music_knowledge import pool, sync
    from mohobot.music_knowledge.song_database import Song

    folder, filename = _fresh_db()
    sync.set_db_location(folder, filename)

    moegirl = _FakeAdapter("moegirl", {
        "九九八十一(乐正绫)": _record(
            source="moegirl", source_title="九九八十一(乐正绫)", name="九九八十一",
            singers="乐正绫", uploader="乌龟Sui", year=2016,
            lyricist="邪叫教主", composer="乌龟Sui",
            introduction="介绍", lyrics="第一行\n第二行\n第三行",
            aliases=["九九八十一(乐正绫)"],
        ),
    })
    run1 = sync.sync_source(moegirl, mode="fill_missing")
    assert run1.merge.created == 1, run1.as_dict()

    # 同来源同内容: 第二次应全部跳过(断点已记 done, 不再发起抓取)
    run_again = sync.sync_source(moegirl, mode="fill_missing")
    assert run_again.merge.created == 0
    assert run_again.fetched == 0

    # 另一个来源补空字段(mixer/arranger 原为空)
    voca = _FakeAdapter("vocawiki", {
        "九九八十一": _record(
            source="vocawiki", source_title="九九八十一", name="九九八十一",
            singers="乐正绫", mixer="混音者", arranger="编曲者",
            lyrics="更长的歌词版本" * 5,
        ),
    })
    run2 = sync.sync_source(voca, mode="fill_missing")
    assert run2.merge.filled_fields >= 2, run2.as_dict()
    assert run2.merge.created == 0

    db = pool.get_session()
    try:
        song = db.query(Song).filter(Song.name == "九九八十一").one()
        assert song.mixer == "混音者" and song.arranger == "编曲者"
        # 歌词优先级: vcpedia > vocawiki > moegirl, 这里 vocawiki 应替换 moegirl
        assert "更长的歌词版本" in song.lyrics
        assert song.year == 2016
        assert "九九八十一(乐正绫)" in (song.aliases or "")
    finally:
        db.close()
    print("[2] 合并新建/补字段 OK")


async def test_merge_does_not_collapse_different_songs() -> None:
    """同名不同演唱者: 不得被合并成一首。"""
    from mohobot.music_knowledge import pool, sync
    from mohobot.music_knowledge.song_database import Song

    folder, filename = _fresh_db()
    sync.set_db_location(folder, filename)

    adapter = _FakeAdapter("moegirl", {
        "彼岸花(洛天依)": _record(
            source="moegirl", source_title="彼岸花(洛天依)", name="彼岸花",
            singers="洛天依", year=2015, lyrics="洛天依版本歌词",
        ),
        "彼岸花(乐正绫)": _record(
            source="moegirl", source_title="彼岸花(乐正绫)", name="彼岸花",
            singers="乐正绫", year=2018, lyrics="乐正绫版本歌词",
        ),
    })
    stats = sync.sync_source(adapter, mode="fill_missing")
    db = pool.get_session()
    try:
        rows = db.query(Song).filter(Song.name == "彼岸花").all()
        assert len(rows) == 2, [(r.name, r.singers) for r in rows]
        assert {r.singers for r in rows} == {"洛天依", "乐正绫"}
    finally:
        db.close()
    # 第二个可能被判为歧义(取决于打分), 但绝不能只剩一首
    assert stats.merge.created + stats.merge.unmatched + stats.merge.unchanged == 2
    print("[3] 同名不同歌手不误合并 OK")


async def test_sync_resume_and_failure() -> None:
    """同步断点: 已完成标题不重复抓取; 单条失败不中断整轮。"""
    from mohobot.music_knowledge import pool, sync
    from mohobot.music_knowledge.song_database import Song
    from mohobot.music_knowledge.sources.base import SourceError

    folder, filename = _fresh_db()
    sync.set_db_location(folder, filename)

    records = {
        "A": _record(source="vocawiki", source_title="A", name="A歌", singers="洛天依",
                     lyrics="aaa", year=2020),
        "B": SourceError("boom"),
        "C": _record(source="vocawiki", source_title="C", name="C歌", singers="言和",
                     lyrics="ccc", year=2021),
    }
    adapter = _FakeAdapter("vocawiki", records)
    stats = sync.sync_source(adapter, mode="fill_missing", max_fail=5)
    assert stats.failed == 1
    assert stats.merge.created == 2

    # 第二轮: A/C 已记录, 不应再抓; B 仍失败
    adapter2 = _FakeAdapter("vocawiki", records)
    stats2 = sync.sync_source(adapter2, mode="fill_missing", max_fail=5)
    assert "A" not in adapter2.fetched and "C" not in adapter2.fetched
    assert "B" in adapter2.fetched
    assert stats2.failed == 1

    db = pool.get_session()
    try:
        assert db.query(Song).count() == 2
    finally:
        db.close()
    print("[4] 断点续跑/单条失败 OK")


async def test_mode_new_only() -> None:
    """new_only 模式: 库内已有的歌不再抓取。"""
    from mohobot.music_knowledge import pool, sync
    from mohobot.music_knowledge.song_database import Song

    folder, filename = _fresh_db()
    sync.set_db_location(folder, filename)
    db = pool.get_session()
    try:
        db.add(Song(name="老歌", safe_name="老歌", normalized_name="老歌",
                    singers="洛天依", introduction="", lyrics="旧歌词"))
        db.commit()
    finally:
        db.close()

    adapter = _FakeAdapter("moegirl", {
        "老歌": _record(source="moegirl", source_title="老歌", name="老歌",
                        singers="洛天依", lyrics="新歌词覆盖", lyricist="补到的作词"),
        "新歌": _record(source="moegirl", source_title="新歌", name="新歌",
                        singers="言和", lyrics="新歌歌词"),
    })
    stats = sync.sync_source(adapter, mode="new_only")
    assert adapter.fetched == ["新歌"], adapter.fetched
    assert stats.merge.created == 1

    db = pool.get_session()
    try:
        old = db.query(Song).filter(Song.name == "老歌").one()
        # new_only 不应改动老歌字段
        assert old.lyrics == "旧歌词" and not old.lyricist
    finally:
        db.close()
    print("[5] new_only 模式 OK")


async def test_fuzzy_name_and_lyrics() -> None:
    """模糊索引: 错字歌名与错字歌词仍能命中; 日常句子不误报。"""
    from mohobot.music_knowledge.fuzzy import LyricGramIndex, SongNameIndex

    names = [("达拉崩吧", "u1"), ("九九八十一", "u2"), ("千本樱", "u3")]
    index = SongNameIndex(names)
    assert index.match("达拉崩吧")[0].reason == "exact"
    typo = index.match("达拉崩巴")
    assert typo and typo[0].name == "达拉崩吧" and typo[0].reason == "typo", typo
    assert index.match("今天天气不错") == []

    lyrics = [
        ("u1", "达拉崩吧",
         "很久很久以前 巨龙突然出现\n带来灾难 带走了公主又消失不见\n"
         "王国十分危险 世间谁最勇敢\n一位勇者赶来 大声喊"),
        ("u2", "九九八十一",
         "刚擒住了几个妖 又降住了几个魔\n魑魅魍魉怎么它就这么多\n"
         "杀你个魂也丢来魄也落"),
    ]
    lyric_index = LyricGramIndex()
    lyric_index.build(lyrics)
    hit = lyric_index.match("大王叫我来巡山 很久很久以前巨龙突然出现带走了公主")
    assert hit and hit[0].name == "达拉崩吧", hit
    # 错一字仍命中
    typo_hit = lyric_index.match("刚擒住了几个妖 又降住了几个摸")
    assert typo_hit and typo_hit[0].name == "九九八十一", typo_hit
    assert lyric_index.match("今天天气不错我们去吃饭吧") == []
    print("[6] 模糊歌名/歌词 OK")


async def test_matcher_fuzzy_and_budget() -> None:
    """匹配器: 模糊命中带 confidence; 预算模式只给命中片段。"""
    from mohobot.music_knowledge import SongInfoMatcher
    from mohobot.music_knowledge.song_database import Song

    folder, filename = _fresh_db()
    from mohobot.music_knowledge import pool

    db = pool.get_session()
    try:
        db.add(Song(
            name="达拉崩吧", safe_name="达拉崩吧", normalized_name="达拉崩吧",
            singers="洛天依、言和", uploader="ilem", year=2017, lyricist="ilem",
            introduction="一首很长的介绍。" * 40,
            lyrics="\n".join(f"第{i}行歌词内容在这里" for i in range(1, 21)),
        ))
        db.commit()
    finally:
        db.close()

    matcher = SongInfoMatcher(db_folder=folder, db_file=filename, fuzzy=True)
    match = matcher.match("唱一首达拉崩巴")
    assert match is not None and match.name == "达拉崩吧"
    assert match.confidence < 1.0

    full = match.build_annotation()
    assert "完整歌词：" in full and "第20行" in full

    budgeted = match.build_annotation(max_chars=400, lyric_context_lines=2)
    assert len(budgeted) <= 400 + 30, len(budgeted)
    assert "完整歌词：" not in budgeted
    assert "song_get_lyrics" in budgeted
    assert "第20行" not in budgeted  # 只给命中处附近, 不是全文

    # 日常聊天不误报
    assert matcher.match("今天天气不错我们去吃饭吧") is None
    print("[7] 匹配器模糊与预算 OK")


async def test_annotation_lyric_span() -> None:
    """注入的歌词片段应围绕命中行, 而不是从头截。"""
    from mohobot.music_knowledge.matcher import SongMatch

    lyrics = "\n".join(f"第{i}句歌词" for i in range(1, 31))
    detail = {"name": "测试", "lyrics": lyrics, "introduction": "简介"}
    match = SongMatch("测试", detail, confidence=0.8, reason="lyric",
                      lyric_span="第20句歌词")
    text = match.build_annotation(max_chars=600, lyric_context_lines=1)
    assert "第20句歌词" in text
    assert "第1句歌词" not in text
    assert "第30句歌词" not in text
    print("[8] 命中片段定位 OK")


async def test_source_parsers_offline() -> None:
    """来源解析器的纯函数(离线夹具, 不走网络)。"""
    from mohobot.music_knowledge.sources.moegirl import (
        parse_category_titles,
        parse_category_total,
    )
    from mohobot.music_knowledge.sources.vocawiki import (
        parse_lyrics_kai,
        parse_songbox,
    )

    html = (
        '<div class="mw-category"><ul>'
        '<li><a href="/A" title="A">A</a></li>'
        '<li><a href="/B" title="B">B</a></li>'
        '</ul></div><p>以下 200 个页面属于本分类，共 3,524 个页面。</p>'
    )
    assert parse_category_titles(html) == ["A", "B"]
    assert parse_category_total(html) == 3524

    source = (
        "{{VOCALOID_Songbox\n"
        "|演唱 = [[洛天依]]、[[言和]]\n"
        "|歌曲名称 = 测试曲\n"
        "|UP主 = [[某P]]\n"
        "|bb_id = BV1Vs411H7JH\n"
        "|bb_date = 2024-01-02\n"
        "}}\n"
        "== 歌词 ==\n{{LyricsKai\n|original=中文原文\n第二行\n|translated=翻译\n}}"
    )
    box = parse_songbox(source)
    assert "洛天依" in box.get("演唱", "")
    assert box.get("bb_id") == "BV1Vs411H7JH"
    lyrics = parse_lyrics_kai(source)
    assert "中文原文" in lyrics.get("original", "")
    print("[9] 来源解析器夹具 OK")


async def test_schema_migration_idempotent() -> None:
    """migrate_song_schema 可重复执行, 并为旧行补 normalized_name/lang。"""
    from mohobot.music_knowledge import pool
    from mohobot.music_knowledge.song_database import (
        Song, backfill_normalized_columns, migrate_song_schema,
    )

    folder, filename = _fresh_db()
    migrate_song_schema(pool._engine)
    migrate_song_schema(pool._engine)  # 幂等
    db = pool.get_session()
    try:
        db.add(Song(name="测试歌", safe_name="测试歌", introduction="", lyrics="中文歌词"))
        db.commit()
        updated = backfill_normalized_columns(db)
        assert updated >= 1
        row = db.query(Song).filter(Song.name == "测试歌").one()
        assert row.normalized_name == "测试歌"
        assert row.lang == "zh"
    finally:
        db.close()
    print("[10] schema 迁移幂等 OK")


async def test_concurrent_sources_no_duplicates() -> None:
    """三来源并行: 同一首新歌被同时抓到, 只能建一条记录。

    回归: merge() 必须在合并锁内提交。旧实现先放锁再由调用方提交,
    另一来源在锁内查不到未提交的新歌, 会各建一条重复(CI 实测复现)。
    用 Barrier 把三个线程对齐到同一时刻, 尽量复现竞争窗口。
    """
    import threading

    from mohobot.music_knowledge import pool, sync
    from mohobot.music_knowledge.song_database import Song, SongSource

    title = "达拉崩吧"
    for round_no in range(3):
        folder, filename = _fresh_db()
        sync.set_db_location(folder, filename)

        records = {
            "moegirl": _record(
                source="moegirl", source_title=title, name=title,
                singers="洛天依、言和", uploader="ilem", year=2017,
                lyricist="ilem", lyrics="萌百歌词" * 20),
            "vocawiki": _record(
                source="vocawiki", source_title=title, name=title,
                singers="洛天依、言和", year=2017, composer="ilem",
                bvid="BV1eF411K7pf", lyrics="voca歌词" * 20),
            "vcpedia": _record(
                source="vcpedia", source_title=title, name=title,
                singers="洛天依、言和", year=2017, arranger="ilem",
                lyrics="vcpedia歌词" * 20),
        }
        adapters = [_FakeAdapter(name, {title: rec}) for name, rec in records.items()]

        barrier = threading.Barrier(len(adapters), timeout=30)
        errors: list[Exception] = []

        class _Sync(_FakeAdapter):
            actual = None

        def run(adapter):
            barrier.wait()
            try:
                sync.sync_source(adapter, mode="fill_missing")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(a,)) for a in adapters]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert not errors, errors

        db = pool.get_session()
        try:
            rows = db.query(Song).filter(Song.name == title).all()
            assert len(rows) == 1, (
                f"round {round_no}: "
                + str([(r.name, r.singers, r.primary_source) for r in rows])
            )
            song = rows[0]
            source_rows = (
                db.query(SongSource)
                .filter(SongSource.source_title == title)
                .all()
            )
            assert len(source_rows) == 3, [(r.source, r.status) for r in source_rows]
            assert all(r.song_uuid == song.uuid for r in source_rows)
        finally:
            db.close()

    print("[11] 三来源并行去重(3 轮 Barrier 对齐) OK")


async def test_moegirl_pagination_cursor() -> None:
    """萌百分类翻页必须用站点给出的 pagefrom, 而不是本页最后一个标题。

    回归: `?from=` 是按排序键包含式游标, 中文标题按拼音排序, 用标题作游标
    会原地打转, 导致第 2 页之后取不到数据、分类被误判提前结束
    (洛天依分类标称 3524, 旧实现只枚举到 2484)。
    """
    from mohobot.music_knowledge.sources.moegirl import parse_next_pagefrom

    html = (
        '<div class="mw-pages">'
        '<a href="/index.php?title=Category:X&amp;pagefrom='
        '%E5%8D%9A%E6%AD%8C%E6%A2%A6%E6%BE%9C#mw-pages">下一页</a></div>'
    )
    assert parse_next_pagefrom(html) == "博歌梦澜"
    assert parse_next_pagefrom("<p>没有下一页</p>") == ""
    assert parse_next_pagefrom("") == ""
    # 上一页/无关链接不应被误认为下一页
    only_prev = '<a href="/index.php?pageuntil=X">上一页</a>'
    assert parse_next_pagefrom(only_prev) == ""
    print("[13] 萌百翻页游标 OK")


async def test_build_adapters_accepts_both_config_shapes() -> None:
    """build_adapters 必须同时接受完整配置和 music_knowledge 段。

    回归: 插件/脚本传入 music_knowledge 段时, 旧实现取 config["music_knowledge"]
    得到空 dict, sync.sources(含分类/interval)被静默忽略, 适配器回落到默认
    大分类(初音未来等), 表现为"范围配置不生效"。
    """
    from mohobot.music_knowledge.sync import build_adapters

    inner = {
        "song_database": {"db_folder": "./data", "db_file": "x.db"},
        "sync": {
            "order": ["moegirl"],
            "sources": {"moegirl": {"categories": ["洛天依歌曲"], "interval": 3.5}},
        },
    }
    for shape in (inner, {"music_knowledge": inner}):
        adapter = build_adapters(shape)[0]
        assert adapter.categories == ["洛天依歌曲"], adapter.categories
        assert adapter.interval == 3.5, adapter.interval
    print("[12] build_adapters 两种入参 OK")


async def main() -> None:
    await test_normalize_name()
    await test_merge_create_and_fill()
    await test_merge_does_not_collapse_different_songs()
    await test_sync_resume_and_failure()
    await test_mode_new_only()
    await test_fuzzy_name_and_lyrics()
    await test_matcher_fuzzy_and_budget()
    await test_annotation_lyric_span()
    await test_source_parsers_offline()
    await test_schema_migration_idempotent()
    await test_concurrent_sources_no_duplicates()
    await test_build_adapters_accepts_both_config_shapes()
    await test_moegirl_pagination_cursor()
    print("ALL SONG SYNC TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
