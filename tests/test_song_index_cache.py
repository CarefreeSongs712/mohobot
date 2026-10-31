"""歌曲索引缓存/后台构建测试(对应设计文档第 8 节 1-7)。

全部使用临时歌曲库与临时缓存目录, 不读写仓库/业务 data/。
"""

import atexit
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIRS: list[str] = []


def _cleanup_all() -> None:
    """进程结束时关闭歌曲库连接池并清理临时目录(pytest/_run_all 通用)。"""
    try:
        from mohobot.music_knowledge import pool

        pool.close_all()
    except Exception:
        pass
    while _TMP_DIRS:
        shutil.rmtree(_TMP_DIRS.pop(), ignore_errors=True)


atexit.register(_cleanup_all)


# ── 临时库 ────────────────────────────────────────────────────


_SONGS = [
    (
        "uuid-1",
        "千年食谱颂",
        "小笼包啊小笼包\n好吃得不得了\n一口一个香\n"
        "热气腾腾的蒸笼里\n藏着人间烟火的味道",
    ),
    (
        "uuid-2",
        "九九八十一",
        "刚擒住了几个妖\n又降住了几个魔\n"
        "魑魅魍魉怎么它就这么多\n杀你个魂也丢来魄也落",
    ),
    (
        "uuid-3",
        "赤伶",
        "戏一折 水袖起落\n唱悲欢唱离合\n"
        "台下人走过 不见旧颜色\n台上人唱着 心碎离别歌",
    ),
    (
        "uuid-4",
        "达拉崩吧",
        "很久很久以前 巨龙突然出现\n"
        "带来灾难 带走了公主又消失不见\n"
        "王国十分危险 世间谁最勇敢\n一位勇者赶来 大声喊",
    ),
    (
        "uuid-5",
        "标点，测试。",
        "第一句：有标点，也有空格。\n第二句！问号？\n第三句……省略号。",
    ),
]


def _fresh_db(prefix: str = "songcache_") -> tuple[str, str]:
    from mohobot.music_knowledge import pool
    from mohobot.music_knowledge.song_database import Song, SongStats

    folder = tempfile.mkdtemp(prefix=prefix)
    _TMP_DIRS.append(folder)
    pool.ensure_init(folder, "knowledge_db.db")
    db = pool.get_session()
    try:
        db.query(Song).delete()
        db.query(SongStats).delete()
        db.commit()
        for uuid, name, lyrics in _SONGS:
            db.add(
                Song(
                    uuid=uuid,
                    name=name,
                    safe_name=name,
                    uploader="UP主",
                    singers="洛天依",
                    introduction=f"{name}的介绍。",
                    lyrics=lyrics,
                )
            )
        db.commit()
    finally:
        db.close()
    return folder, "knowledge_db.db"


def _cache_cfg(folder: str, *, keep: int = 2) -> dict:
    return {
        "enabled": True,
        "dir": os.path.join(folder, "cache", "song_index"),
        "keep": keep,
    }


def _build_cfg(**kwargs) -> dict:
    cfg = {"mode": "thread", "yield_every": 50}
    cfg.update(kwargs)
    return cfg


def _wait_build(matcher, timeout: float = 30.0) -> None:
    """等待后台构建线程结束并断言快照已安装。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        thread = getattr(matcher, "_build_thread", None)
        if thread is None:
            break
        thread.join(0.05)
    assert matcher.is_ready(), "后台索引构建未完成"


def _sig(match) -> tuple:
    if match is None:
        return (None, None, None, None)
    return (match.name, round(match.confidence, 4), match.reason, match.ambiguous)


_CASES = [
    "你会唱《千年食谱颂》吗",
    "唱一首九九八十一",
    "唱一首赤伶",
    "刚擒住了几个妖 又降住了几个魔",
    "刚擒住了几个妖 又降住了几个摸",          # 错字 → 歌词模糊
    "小笼包啊小笼包 好吃得不得了",            # 歌词片段
    "第一句有标点也有空格第二句问号",          # 标点/空格归一
    "唱一首达拉崩巴",                          # 歌名错字
    "今天天气不错 九九八十一",                 # 无语境词 → 不误伤
    "晚上吃什么呀",
    "",
]


def _expected_direct(folder: str, filename: str) -> tuple:
    """不传 build/index_cache → 旧的同步构建路径。"""
    from mohobot.music_knowledge import SongInfoMatcher

    matcher = SongInfoMatcher(db_folder=folder, db_file=filename)
    return matcher, [_sig(matcher.match(text)) for text in _CASES]


# ── 1. round-trip 与直接构建逐条一致 ─────────────────────────


async def test_roundtrip_matches_direct_build() -> None:
    from mohobot.music_knowledge import SongInfoMatcher
    from mohobot.music_knowledge.index_cache import load_cache

    folder, filename = _fresh_db("songrt_")
    direct, expected = _expected_direct(folder, filename)
    cache_cfg = _cache_cfg(folder)

    # 旧同步路径不应写缓存(build=None 等价旧行为)。
    assert not os.path.isdir(cache_cfg["dir"]) or not os.listdir(
        cache_cfg["dir"]
    )

    # 缓存 miss → 后台构建; 完成后结果与直接构建逐条一致。
    built = SongInfoMatcher(
        db_folder=folder,
        db_file=filename,
        index_cache=cache_cfg,
        build=_build_cfg(),
    )
    _wait_build(built)
    assert [_sig(built.match(text)) for text in _CASES] == expected
    assert built.index_source == "built"

    cached_file = load_cache(
        cache_cfg["dir"], built._fingerprint, expected_gram_size=6
    )
    assert cached_file is not None
    assert cached_file.docs == len(_SONGS)
    assert cached_file.postings > 0

    # 新实例直接命中缓存, 不再构建。
    t0 = time.perf_counter()
    hit = SongInfoMatcher(
        db_folder=folder,
        db_file=filename,
        index_cache=cache_cfg,
        build=_build_cfg(),
    )
    elapsed = time.perf_counter() - t0
    assert hit.is_ready()
    assert hit.index_source == "cache"
    assert elapsed < 10.0
    assert [_sig(hit.match(text)) for text in _CASES] == expected

    # 底层索引也必须逐项一致(gram 顺序/偏移/posting/doc 归一化歌词)。
    left = direct._snapshot.lyric_index.to_compact()
    right = hit._snapshot.lyric_index.to_compact()
    assert left.gram_size == right.gram_size == 6
    assert left.doc_uuid == right.doc_uuid
    assert left.doc_name == right.doc_name
    assert left.doc_lyrics == right.doc_lyrics
    assert left.doc_len == right.doc_len
    assert left.grams_blob == right.grams_blob
    assert left.gram_off == right.gram_off
    assert left.post_ptr == right.post_ptr
    assert left.post_doc == right.post_doc
    # 原始歌词也保留(精确子串路径不受归一化影响)。
    assert hit._snapshot.index == direct._snapshot.index
    # 两种倒排布局结果一致。
    assert hit._snapshot.lyric_index.layout in ("dict", "bisect")
    assert hit._snapshot.lyric_index.match(
        "刚擒住了几个妖 又降住了几个魔"
    )[0].name == "九九八十一"


# ── 2. 指纹失效: 主库 / WAL / 都不变 ───────────────────────


async def test_fingerprint_invalidation() -> None:
    from mohobot.music_knowledge.index_cache import compute_fingerprint

    folder = tempfile.mkdtemp(prefix="songfp_")
    _TMP_DIRS.append(folder)
    db_path = os.path.join(folder, "k.db")
    with open(db_path, "wb") as fh:
        fh.write(b"x" * 128)

    fp = compute_fingerprint(db_path, gram_size=6, fuzzy=True)
    assert fp == compute_fingerprint(db_path, gram_size=6, fuzzy=True)
    assert len(fp) == 16

    st = os.stat(db_path)
    os.utime(db_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    fp_mtime = compute_fingerprint(db_path, gram_size=6, fuzzy=True)
    assert fp_mtime != fp
    os.utime(db_path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert compute_fingerprint(db_path, gram_size=6, fuzzy=True) == fp

    wal_path = db_path + "-wal"
    with open(wal_path, "wb") as fh:
        fh.write(b"w" * 32)
    fp_wal = compute_fingerprint(db_path, gram_size=6, fuzzy=True)
    assert fp_wal != fp, "只改 -wal 也必须让指纹失效"
    wst = os.stat(wal_path)
    os.utime(wal_path, ns=(wst.st_atime_ns, wst.st_mtime_ns + 1_000_000))
    assert compute_fingerprint(db_path, gram_size=6, fuzzy=True) != fp_wal
    os.remove(wal_path)
    assert compute_fingerprint(db_path, gram_size=6, fuzzy=True) == fp

    # 规则版本也参与指纹。
    assert (
        compute_fingerprint(db_path, gram_size=5, fuzzy=True) != fp
    )
    assert (
        compute_fingerprint(db_path, gram_size=6, fuzzy=False) != fp
    )


# ── 3. 损坏缓存: 截断/改字节 → miss, 不抛异常 ───────────────


async def test_corrupted_cache_is_miss() -> None:
    from mohobot.music_knowledge import SongInfoMatcher
    from mohobot.music_knowledge.index_cache import cache_path, load_cache
    from mohobot.music_knowledge.matcher import build_song_index_cache

    folder, filename = _fresh_db("songbad_")
    cache_cfg = _cache_cfg(folder)
    path = build_song_index_cache(folder, filename, cache_cfg["dir"])
    assert path and os.path.exists(path)

    from mohobot.music_knowledge.index_cache import compute_fingerprint

    fp = compute_fingerprint(
        os.path.join(folder, filename), gram_size=6, fuzzy=True
    )
    assert load_cache(cache_cfg["dir"], fp) is not None

    # 截断
    with open(path, "rb") as fh:
        payload = fh.read()
    with open(path, "wb") as fh:
        fh.write(payload[: len(payload) // 2])
    assert load_cache(cache_cfg["dir"], fp) is None
    assert not os.path.exists(path), "损坏缓存应被删除"

    # 改一个 body 字节(校验失败)
    path = build_song_index_cache(folder, filename, cache_cfg["dir"])
    assert path
    with open(path, "rb") as fh:
        data = bytearray(fh.read())
    data[-1] ^= 0xFF
    with open(path, "wb") as fh:
        fh.write(data)
    assert load_cache(cache_cfg["dir"], fp) is None
    assert not os.path.exists(path)

    # 损坏后 Matcher 自动重建而不是抛异常。
    matcher = SongInfoMatcher(
        db_folder=folder,
        db_file=filename,
        index_cache=cache_cfg,
        build=_build_cfg(),
    )
    _wait_build(matcher)
    assert matcher.is_ready()
    assert load_cache(cache_cfg["dir"], matcher._fingerprint) is not None
    assert matcher.match("唱一首赤伶") is not None
    assert cache_path(cache_cfg["dir"], matcher._fingerprint) == path


# ── 4. 未就绪语义: 不触发同步构建 ────────────────────────────


async def test_not_ready_does_not_trigger_sync_build() -> None:
    from mohobot.music_knowledge import SongInfoMatcher
    import mohobot.music_knowledge.matcher as matcher_module

    folder, filename = _fresh_db("songnotready_")
    gate = threading.Event()
    original = matcher_module.SongInfoMatcher._build_snapshot_from_rows

    def blocked(self, rows, **kwargs):
        gate.wait(10)
        return original(self, rows, **kwargs)

    matcher_module.SongInfoMatcher._build_snapshot_from_rows = blocked
    try:
        matcher = SongInfoMatcher(
            db_folder=folder,
            db_file=filename,
            index_cache=_cache_cfg(folder),
            build=_build_cfg(),
        )
        sync_calls: list[int] = []
        matcher.reload_index = lambda: sync_calls.append(1)  # type: ignore[method-assign]

        assert not matcher.is_ready()
        assert matcher.index_source == "pending"
        assert matcher.match("刚擒住了几个妖 又降住了几个魔") is None
        assert matcher._find_by_lyrics("刚擒住了几个妖又降住了几个魔") is None
        assert matcher._find_by_lyrics_fuzzy(
            "刚擒住了几个妖又降住了几个魔"
        ) is None
        assert sync_calls == [], "未就绪时不得触发同步 reload_index()"

        gate.set()
        _wait_build(matcher)
        assert matcher.is_ready()
        assert matcher.match("唱一首九九八十一") is not None
        assert sync_calls == []
    finally:
        gate.set()
        matcher_module.SongInfoMatcher._build_snapshot_from_rows = original


# ── 5. 快照原子替换: 旧引用仍可读, 新引用生效 ────────────────


async def test_atomic_snapshot_replacement() -> None:
    from mohobot.music_knowledge import SongInfoMatcher

    folder, filename = _fresh_db("songatomic_")
    matcher = SongInfoMatcher(
        db_folder=folder,
        db_file=filename,
        index_cache=_cache_cfg(folder),
        build=_build_cfg(),
    )
    _wait_build(matcher)
    old_snapshot = matcher._snapshot
    assert old_snapshot is not None

    errors: list[Exception] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                result = matcher.match("刚擒住了几个妖 又降住了几个魔")
                assert result is not None and result.name == "九九八十一"
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for thread in readers:
        thread.start()
    try:
        matcher.request_rebuild(reason="test")
        _wait_build(matcher)
    finally:
        stop.set()
        for thread in readers:
            thread.join(2)

    new_snapshot = matcher._snapshot
    assert new_snapshot is not None
    assert new_snapshot is not old_snapshot, "重建后应替换为新的快照对象"
    assert not errors
    # 旧快照仍然完整可用(在途查询持有引用不会被构建线程破坏)。
    candidates = old_snapshot.lyric_index.match(
        "刚擒住了几个妖 又降住了几个魔"
    )
    assert candidates and candidates[0].name == "九九八十一"
    assert matcher.match("唱一首赤伶") is not None


# ── 6. 格式版本不符 → miss ──────────────────────────────────


async def test_format_version_mismatch_is_miss() -> None:
    from mohobot.music_knowledge.index_cache import (
        compute_fingerprint,
        load_cache,
    )
    from mohobot.music_knowledge.matcher import build_song_index_cache

    folder, filename = _fresh_db("songver_")
    cache_cfg = _cache_cfg(folder)
    path = build_song_index_cache(folder, filename, cache_cfg["dir"])
    assert path
    fp = compute_fingerprint(
        os.path.join(folder, filename), gram_size=6, fuzzy=True
    )
    assert load_cache(cache_cfg["dir"], fp) is not None

    with open(path, "rb") as fh:
        data = bytearray(fh.read())
    # magic(7B) 之后是 version(uint32 LE)。
    assert struct.unpack_from("<7sI", bytes(data), 0)[0] == b"MBIDX1\0"
    struct.pack_into("<I", data, 7, 0)
    with open(path, "wb") as fh:
        fh.write(data)
    assert load_cache(cache_cfg["dir"], fp) is None

    # 自愈: 下一次构建重新写入当前版本缓存。
    rebuilt = build_song_index_cache(folder, filename, cache_cfg["dir"])
    assert rebuilt
    assert load_cache(cache_cfg["dir"], fp) is not None


# ── 7. 兼容关闭: enabled=false / mode=sync 回退旧行为 ────────


async def test_compat_disabled_and_sync() -> None:
    from mohobot.music_knowledge import SongInfoMatcher

    folder, filename = _fresh_db("songcompat_")
    disabled_dir = os.path.join(folder, "cache", "disabled")
    matcher = SongInfoMatcher(
        db_folder=folder,
        db_file=filename,
        index_cache={"enabled": False, "dir": disabled_dir, "keep": 2},
        build=_build_cfg(),
    )
    # 构造即完成同步构建(旧行为), 不写缓存。
    assert matcher.is_ready()
    assert matcher.index_source == "built"
    assert matcher.match("唱一首九九八十一") is not None
    assert not os.path.isdir(disabled_dir) or not os.listdir(disabled_dir)

    sync_dir = os.path.join(folder, "cache", "sync")
    matcher2 = SongInfoMatcher(
        db_folder=folder,
        db_file=filename,
        index_cache={"enabled": True, "dir": sync_dir, "keep": 2},
        build={"mode": "sync", "yield_every": 50},
    )
    assert matcher2.is_ready()
    assert matcher2.index_source == "built"
    assert matcher2.match("唱一首赤伶") is not None
    assert not os.path.isdir(sync_dir) or not os.listdir(sync_dir)

    # sync 模式下 request_rebuild 走同步 reload(等价旧行为)。
    matcher2.request_rebuild(reason="test")
    assert matcher2.is_ready()


# ── 附加: 两种倒排布局结果一致 ──────────────────────────────


async def test_bisect_layout_matches_dict_layout() -> None:
    """大库加载会选 bisect(方案 B), 必须与 dict(方案 A) 结果完全一致。"""
    from mohobot.music_knowledge import SongInfoMatcher
    from mohobot.music_knowledge.fuzzy import LyricGramIndex

    folder, filename = _fresh_db("songbisect_")
    direct = SongInfoMatcher(db_folder=folder, db_file=filename)
    compact = direct._snapshot.lyric_index.to_compact()

    as_dict = LyricGramIndex.from_compact(compact, layout="dict", benchmark=False)
    as_bisect = LyricGramIndex.from_compact(
        compact, layout="bisect", benchmark=False
    )
    assert as_dict.layout == "dict"
    assert as_bisect.layout == "bisect"
    assert as_dict.stats()["grams"] == as_bisect.stats()["grams"] > 0
    assert as_dict.stats()["postings"] == as_bisect.stats()["postings"]

    def signature(index):
        return [
            [
                (c.uuid, c.name, c.hits, round(c.coverage, 6), c.ambiguous)
                for c in index.match(text)
            ]
            for text in _CASES
        ]

    assert signature(as_dict) == signature(as_bisect)

    # 缓存加载后按所选布局工作, 未就绪语义不受影响。
    matcher = SongInfoMatcher(
        db_folder=folder,
        db_file=filename,
        index_cache=_cache_cfg(folder),
        build=_build_cfg(),
    )
    _wait_build(matcher)
    assert [_sig(matcher.match(text)) for text in _CASES] == [
        _sig(direct.match(text)) for text in _CASES
    ]


# ── 附加: keep 清理 ─────────────────────────────────────────


async def test_cache_keep_cleanup() -> None:
    from mohobot.music_knowledge.index_cache import save_cache
    from mohobot.music_knowledge.matcher import build_song_index_cache

    folder, filename = _fresh_db("songkeep_")
    cache_cfg = _cache_cfg(folder, keep=2)
    first = build_song_index_cache(folder, filename, cache_cfg["dir"])
    assert first
    from mohobot.music_knowledge.index_cache import load_cache

    data = load_cache(cache_cfg["dir"], os.path.basename(first)[:-4])
    assert data is not None

    # 用同一份数据写三个不同指纹的副本, keep=2 只保留最近两份。
    for seq, fingerprint in enumerate(("aaaa1111aaaa1111", "bbbb2222bbbb2222")):
        data.fingerprint = fingerprint
        assert save_cache(cache_cfg["dir"], data, keep=2)
        time.sleep(0.01)

    bins = sorted(
        name for name in os.listdir(cache_cfg["dir"]) if name.endswith(".bin")
    )
    assert len(bins) == 2, bins
    assert "bbbb2222bbbb2222.bin" in bins


def test_file_lock_does_not_retry_body_on_oserror() -> None:
    """锁内代码抛 OSError 必须只执行一次并向上抛; Windows 分支不得重复 yield。"""
    from mohobot.music_knowledge.index_cache import _file_lock

    with tempfile.TemporaryDirectory() as tmp:
        calls: list[int] = []
        try:
            with _file_lock(tmp):
                calls.append(1)
                raise OSError("boom")
        except OSError:
            pass
        assert calls == [1], calls


# ── 附加: 复审 P2/P3 回归 ───────────────────────────────────


def test_load_rows_requires_stable_fingerprint() -> None:
    """读行前后指纹不一致时必须返回空指纹, 避免旧行配新指纹写缓存。"""
    from mohobot.music_knowledge import SongInfoMatcher

    folder, filename = _fresh_db("songfpri_")
    matcher = SongInfoMatcher(db_folder=folder, db_file=filename)
    seq = iter(["a", "b", "c", "d"])
    matcher._safe_fingerprint = lambda: next(seq)

    rows, fingerprint = matcher._load_rows_with_stable_fingerprint(attempts=2)

    assert rows and fingerprint == ""


def test_build_once_skips_cache_when_fingerprint_unstable() -> None:
    from mohobot.music_knowledge import SongInfoMatcher
    from mohobot.music_knowledge.matcher import _load_song_rows

    folder, filename = _fresh_db("songnowrite_")
    matcher = SongInfoMatcher(db_folder=folder, db_file=filename, build={"mode": "sync"})
    rows = _load_song_rows()
    matcher._load_rows_with_stable_fingerprint = lambda **kw: (rows, "")
    written: list[int] = []
    matcher._write_cache = lambda *a, **k: written.append(1)

    matcher._build_once("fingerprint-unstable")

    assert written == []
    assert matcher.is_ready()


def test_build_worker_retries_once_on_transient_failure() -> None:
    from mohobot.music_knowledge import SongInfoMatcher
    import mohobot.music_knowledge.matcher as matcher_module

    folder, filename = _fresh_db("songretry_")
    matcher = SongInfoMatcher(db_folder=folder, db_file=filename, build={"mode": "sync"})
    calls: list[str] = []

    def flaky(reason: str) -> None:
        calls.append(reason)
        if len(calls) == 1:
            raise RuntimeError("database is locked")

    matcher._build_once = flaky
    original_sleep = matcher_module.time.sleep
    matcher_module.time.sleep = lambda _seconds: None
    try:
        matcher._build_worker("retry")
    finally:
        matcher_module.time.sleep = original_sleep

    assert calls == ["retry", "retry"]


def test_close_prevents_new_build() -> None:
    from mohobot.music_knowledge import SongInfoMatcher

    folder, filename = _fresh_db("songclose_")
    matcher = SongInfoMatcher(db_folder=folder, db_file=filename, build={"mode": "sync"})
    matcher._build_mode = "thread"

    matcher.close(timeout=0)
    matcher.request_rebuild(reason="after-close")

    assert matcher._closing is True
    assert matcher._build_thread is None


def test_put_cache_ignores_stale_snapshot() -> None:
    from mohobot.music_knowledge import SongInfoMatcher
    from mohobot.music_knowledge.matcher import _IndexSnapshot

    folder, filename = _fresh_db("songstale_")
    matcher = SongInfoMatcher(db_folder=folder, db_file=filename)
    old_snapshot = matcher._snapshot
    result = matcher.match("唱一首赤伶")
    assert result is not None

    matcher._install(_IndexSnapshot((), None, None, "empty"), source="empty")
    matcher._put_cache("stale", result, snap=old_snapshot)

    assert matcher._cache == {}
