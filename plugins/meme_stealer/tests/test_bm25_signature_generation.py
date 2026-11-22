"""BM25 语料签名代次校验测试(issue #129)。

搜图热路径曾每次调用都重新计算 O(n) 语料签名；现在先用数据库语料代次
做 O(1) 校验，代次未变直接复用已有索引。这里覆盖：
- 构建后同代次不重算签名；
- 写入后代次变化会触发重建(即便调用方漏了 invalidate)；
- 无代次信息时退化为签名比对；
- 搜图路径连续调用只构建一次。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meme_stealer_core.core.db.database_service import DatabaseService
from meme_stealer_core.core.search.meme_search_engine import MemeSearchEngine
from meme_stealer_core.core.search.meme_smart_select_service import MemeSmartSelectService


def _entry(path: str, category: str = "happy", desc: str = "开心", overlay: str = "") -> dict:
    return {
        "path": path,
        "hash": f"hash-{Path(path).stem}",
        "category": category,
        "desc": desc,
        "overlay_text": overlay,
        "tags": [category],
        "scenes": [category],
    }


class _SelectionStrategy:
    def __init__(self) -> None:
        self._recent_usage: dict[str, list[str]] = {}

    def _calculate_recent_penalty(self, category: str, path: str) -> float:
        return 0.0

    def _update_recent_usage(self, category: str, path: str) -> None:
        pass


class _ScopeService:
    def _is_entry_allowed_for_event(self, data, event) -> bool:
        return True


@pytest.fixture()
def env(tmp_path: Path):
    db = DatabaseService(tmp_path / "emoji.db")
    plugin = SimpleNamespace(
        db_service=db,
        cache_dir=tmp_path / "cache",
        plugin_config={},
        enable_embedding_search=False,
        meme_selector=None,
        models=None,
    )
    engine = MemeSearchEngine(plugin, _SelectionStrategy(), _ScopeService())
    return db, plugin, engine


def test_generation_bumped_only_by_real_emoji_writes(env):
    db, _plugin, _engine = env

    async def run():
        start = db.get_index_generation()
        assert await db.insert_batch([_entry("/a.gif")]) == 1
        after_insert = db.get_index_generation()
        assert after_insert == start + 1
        assert await db.update_path("/a.gif", {"desc": "新描述"}) is True
        after_update = db.get_index_generation()
        assert after_update > after_insert
        assert await db.update_path("/missing.gif", {"desc": "x"}) is False
        assert db.get_index_generation() == after_update
        assert await db.delete_paths(["/a.gif"]) == 1
        after_delete = db.get_index_generation()
        assert after_delete > after_update
        assert await db.delete_paths(["/missing.gif"]) == 0
        assert db.get_index_generation() == after_delete

    import asyncio

    asyncio.run(run())


def test_build_records_generation_and_same_generation_skips_signature(env, monkeypatch):
    db, _plugin, engine = env

    async def run():
        await db.insert_batch([_entry("/a.gif")])
        idx = db.get_index_cache_readonly()
        await engine._build_bm25_index(idx)
        assert engine._bm25_index is not None
        assert engine._bm25_generation == db.get_index_generation()

        calls = {"n": 0}
        original = engine._compute_bm25_signature

        def counting(idx_arg, **kwargs):
            calls["n"] += 1
            return original(idx_arg, **kwargs)

        monkeypatch.setattr(engine, "_compute_bm25_signature", counting)
        assert engine.bm25_index_stale(idx) is False
        assert engine.bm25_index_stale(idx) is False
        assert calls["n"] == 0, "同代次不应重算语料签名"

    import asyncio

    asyncio.run(run())


def test_write_without_invalidation_detected_by_generation(env, monkeypatch):
    db, _plugin, engine = env

    async def run():
        await db.insert_batch([_entry("/a.gif")])
        idx = db.get_index_cache_readonly()
        await engine._build_bm25_index(idx)

        # 模拟写入路径漏了 _invalidate_bm25_index: 脏标记仍为 False
        await db.insert_batch([_entry("/b.gif", desc="崩溃")])
        assert engine._bm25_dirty is False
        new_idx = db.get_index_cache_readonly()

        calls = {"n": 0}
        original = engine._compute_bm25_signature

        def counting(idx_arg, **kwargs):
            calls["n"] += 1
            return original(idx_arg, **kwargs)

        monkeypatch.setattr(engine, "_compute_bm25_signature", counting)
        assert engine.bm25_index_stale(new_idx) is True
        assert calls["n"] == 1

        # 重建后代次对齐，恢复 O(1) 判定
        await engine._build_bm25_index(new_idx)
        assert engine._bm25_generation == db.get_index_generation()
        assert engine.bm25_index_stale(new_idx) is False
        # 1 次来自过期检测, 1 次来自重建本身; 之后同代次不再计算
        assert calls["n"] == 2

    import asyncio

    asyncio.run(run())


def test_invalidate_drops_generation(env):
    db, _plugin, engine = env

    async def run():
        await db.insert_batch([_entry("/a.gif")])
        idx = db.get_index_cache_readonly()
        await engine._build_bm25_index(idx)
        engine._invalidate_bm25_index()
        assert engine._bm25_generation == -1
        assert engine.bm25_index_stale(idx) is True

    import asyncio

    asyncio.run(run())


def test_no_generation_support_falls_back_to_signature(tmp_path, monkeypatch):
    db = DatabaseService(tmp_path / "emoji.db")
    # 没有 get_index_generation 的替身 → 走签名比对
    bare_db = SimpleNamespace(
        count_total=db.count_total,
        get_corpus_signature=db.get_corpus_signature,
        get_index_cache_readonly=db.get_index_cache_readonly,
    )
    plugin = SimpleNamespace(db_service=bare_db, cache_dir=tmp_path / "cache")
    engine = MemeSearchEngine(plugin, _SelectionStrategy(), _ScopeService())

    async def run():
        await db.insert_batch([_entry("/a.gif")])
        idx = db.get_index_cache_readonly()
        await engine._build_bm25_index(idx)
        assert engine._bm25_generation == -1
        assert engine.bm25_index_stale(idx) is False
        # 语料真变化时仍能检测到
        await db.insert_batch([_entry("/b.gif", desc="崩溃")])
        assert engine.bm25_index_stale(db.get_index_cache_readonly()) is True

    import asyncio

    asyncio.run(run())


def test_search_images_builds_bm25_once_across_calls(env, monkeypatch):
    db, plugin, engine = env

    async def run():
        await db.insert_batch([_entry("/a.gif", desc="开心大笑")])
        plugin.plugin_config = SimpleNamespace(get_keyword_map=lambda: {})
        smart = MemeSmartSelectService(plugin, engine, _SelectionStrategy(), _ScopeService())
        monkeypatch.setattr(smart, "_is_embedding_ready", lambda: False)

        calls = {"n": 0}
        original = engine._compute_bm25_signature

        def counting(idx_arg, **kwargs):
            calls["n"] += 1
            return original(idx_arg, **kwargs)

        monkeypatch.setattr(engine, "_compute_bm25_signature", counting)

        idx = db.get_index_cache_readonly()
        await smart.search_images("开心", idx=idx)
        assert engine._bm25_index is not None
        assert calls["n"] == 1
        await smart.search_images("开心", idx=idx)
        assert calls["n"] == 1, "同代次第二次搜索不应重算签名"

    import asyncio

    asyncio.run(run())


def test_move_path_bumps_generation_and_stale_detects(env):
    """直接调用 DatabaseService.move_path 也必须推进代次(#129 review)。"""
    db, _plugin, engine = env

    async def run():
        await db.insert_batch([_entry("/old.gif")])
        idx = db.get_index_cache_readonly()
        await engine._build_bm25_index(idx)
        assert engine.bm25_index_stale(idx) is False

        before = db.get_index_generation()
        assert await db.move_path("/old.gif", "/new.gif", "happy") is True
        assert db.get_index_generation() > before, "move_path 成功必须推进语料代次"

        # 调用方漏了 invalidate: 仅靠代次也要发现旧路径
        assert engine._bm25_dirty is False
        assert engine.bm25_index_stale(db.get_index_cache_readonly()) is True

        # 失败的 move 不推进代次
        gen = db.get_index_generation()
        assert await db.move_path("/missing.gif", "/x.gif", "happy") is False
        assert db.get_index_generation() == gen

    import asyncio

    asyncio.run(run())


def test_sync_index_bumps_generation_only_when_changed(env):
    """sync_index 有实际变更才推进代次, 无变化不得反复失效缓存(#129 review)。"""
    db, _plugin, _engine = env

    async def run():
        idx = {"/s.gif": {"path": "/s.gif", "hash": "h1", "category": "happy", "desc": "开心"}}
        before = db.get_index_generation()
        assert await db.sync_index(idx), "首次同步应新增路径"
        assert db.get_index_generation() > before

        gen = db.get_index_generation()
        assert await db.sync_index(idx) == []
        assert db.get_index_generation() == gen, "无变化时不应推进代次"

        changed = {
            "/s.gif": {
                "path": "/s.gif",
                "hash": "h1",
                "category": "happy",
                "desc": "新描述",
            }
        }
        assert await db.sync_index(changed)
        assert db.get_index_generation() > gen

    import asyncio

    asyncio.run(run())


def test_generation_advance_without_signature_change_aligns_cache(env, monkeypatch):
    """代次变了但签名相同时对齐代次, 避免每次搜索重算 O(n) 签名(#129 review)。"""
    db, _plugin, engine = env

    async def run():
        await db.insert_batch([_entry("/a.gif")])
        idx = db.get_index_cache_readonly()
        await engine._build_bm25_index(idx)

        db._bump_index_generation()  # 代次推进, 语料签名不变
        calls = {"n": 0}
        original = engine._compute_bm25_signature

        def counting(idx_arg, **kwargs):
            calls["n"] += 1
            return original(idx_arg, **kwargs)

        monkeypatch.setattr(engine, "_compute_bm25_signature", counting)
        assert engine.bm25_index_stale(idx) is False
        assert calls["n"] == 1, "代次不一致时应做一次签名比对"
        assert engine._bm25_generation == db.get_index_generation(), "签名相同需对齐代次"

        assert engine.bm25_index_stale(idx) is False
        assert calls["n"] == 1, "对齐后应恢复 O(1) 判定"

    import asyncio

    asyncio.run(run())
