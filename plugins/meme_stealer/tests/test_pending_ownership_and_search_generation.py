import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from meme_stealer_core.api import pending as pending_module
from meme_stealer_core.core.config.config import PluginConfig
from meme_stealer_core.core.db.database_service import DatabaseService
from meme_stealer_core.core.search.meme_search_engine import MemeSearchEngine
from meme_stealer_core.core.search.meme_smart_select_service import MemeSmartSelectService
from meme_stealer_core.plugin_api import PluginAPI


@pytest.fixture
def env(tmp_path):
    cfg = PluginConfig({}, tmp_path / "data")
    db = DatabaseService(tmp_path / "emoji.db")
    plugin = SimpleNamespace(plugin_config=cfg, db_service=db, meme_selector=None,
                             base_dir=str(tmp_path / "data"), cache_dir=cfg.cache_dir)
    return SimpleNamespace(cfg=cfg, db=db, plugin=plugin, api=PluginAPI(plugin))


def gallery_snapshot(db):
    # DatabaseService closes each connection; do not leave Windows file handles open.
    with db._get_connection() as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("emoji", "emoji_tag", "emoji_scene")}


async def seed_pending(env, name="claimed.png"):
    src = env.cfg.pending_dir / name
    src.write_bytes(b"owned image bytes")
    entry = {"path": str(src), "hash": hashlib.sha256(src.read_bytes()).hexdigest(),
             "category": "happy", "desc": "pending replacement", "scope_mode": "public"}
    pid = await env.db.insert_pending(entry)
    return pid, src, entry


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["same_path", "same_hash_other_path", "path_other_hash"])
async def test_orphan_recovery_never_takes_registered_gallery_content(env, claim):
    pid, src, entry = await seed_pending(env)
    orphan = env.cfg.ensure_category_dir("happy") / src.name
    orphan.write_bytes(src.read_bytes())
    owned = orphan if claim != "same_hash_other_path" else orphan.with_name("owner.png")
    owned.write_bytes(src.read_bytes())
    await env.db.insert_batch([{**entry, "path": str(owned),
                               "hash": "other-hash" if claim == "path_other_hash" else entry["hash"],
                               "scope_mode": "local", "origin_target": "group:100",
                               "desc": "owner-only", "use_count": 17,
                               "tags": ["owner tag"], "scenes": ["owner scene"]}])
    src.unlink()
    before = gallery_snapshot(env.db)

    ok, _error = await env.api._approve_one(env.db, pid)

    assert not ok
    assert gallery_snapshot(env.db) == before
    assert orphan.read_bytes() == b"owned image bytes"
    assert owned.read_bytes() == b"owned image bytes"
    assert not src.exists()
    assert not env.api._read_approve_marker(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_path", ["same", "other"])
async def test_approval_rechecks_gallery_ownership_after_waiting_for_write_lock(env, claim_path):
    pid, src, entry = await seed_pending(env)
    cat = env.cfg.ensure_category_dir("happy") / src.name
    owned = cat if claim_path == "same" else cat.with_name("owner.png")
    await env.db._write_lock.acquire()
    task = asyncio.create_task(env.api._approve_one(env.db, pid))
    try:
        async with asyncio.timeout(5):
            while not cat.exists():
                await asyncio.sleep(0.01)
        # A competing real SQLite writer registers the path/hash while approval waits.
        env.db._insert_batch_sync([{**entry, "path": str(owned), "scope_mode": "local",
                                   "origin_target": "group:100", "desc": "owner-only"}])
        before = gallery_snapshot(env.db)
    finally:
        env.db._write_lock.release()
    ok, _error = await asyncio.wait_for(task, 5)
    assert not ok
    assert gallery_snapshot(env.db) == before
    assert env.db.get_pending(pid)
    assert (cat if claim_path == "same" else src).read_bytes() == b"owned image bytes"
    # A rejected insert has no authority to recover/delete the competing gallery row.
    assert not env.api._read_approve_marker(pid)
    assert not (await env.api._approve_one(env.db, pid))[0]
    assert gallery_snapshot(env.db) == before


class Selection:
    _recent_usage = {}


class Scope:
    def _is_entry_allowed_for_event(self, data, event):
        return True


async def seed_search(env):
    path = str(env.cfg.ensure_category_dir("happy") / "search.png")
    await env.db.insert_batch([{"path": path, "hash": "search-hash", "category": "happy",
                                "desc": "old cactus", "scope_mode": "public"}])
    engine = MemeSearchEngine(env.plugin, Selection(), Scope())
    await engine._build_bm25_index(env.db.get_index_cache_readonly())
    return path, engine


@pytest.mark.asyncio
async def test_old_snapshot_cannot_certify_a_new_gallery_generation(env):
    path, engine = await seed_search(env)
    old = env.db.get_index_cache_readonly()
    old_generation = engine._bm25_generation
    await env.db.update_path(path, {"desc": "new dolphin"})
    assert engine.bm25_index_stale(old)
    assert engine._bm25_generation == old_generation
    # Explicitly rebuilding an old snapshot must not label it as current either.
    await engine._build_bm25_index(old)
    assert engine.bm25_index_stale(env.db.get_index_cache_readonly())


@pytest.mark.asyncio
async def test_embedding_wait_cannot_permanently_hide_new_bm25_content(env):
    path, engine = await seed_search(env)
    started, release = asyncio.Event(), asyncio.Event()

    class Embedding:
        def is_available(self):
            return True

        async def search(self, query, limit):
            started.set()
            await release.wait()
            return []

    smart = MemeSmartSelectService(env.plugin, engine, Selection(), Scope())
    smart._embedding_service = Embedding()
    task = asyncio.create_task(smart.search_images("dolphin"))
    await asyncio.wait_for(started.wait(), 5)
    await env.db.update_path(path, {"desc": "new dolphin"})
    release.set()
    await asyncio.wait_for(task, 5)
    results = await smart.search_images("dolphin")
    assert results and results[0][0] == path and results[0][1] == "new dolphin"
    assert engine._bm25_signature == engine._search_signature_from_index(engine.get_index())


@pytest.mark.asyncio
async def test_cache_read_cannot_label_old_documents_with_new_generation(env, monkeypatch):
    path, engine = await seed_search(env)
    engine._invalidate_bm25_index()
    started, release = asyncio.Event(), asyncio.Event()
    original = asyncio.to_thread

    async def blocked_read(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "read_text" and getattr(func, "__self__", None) == engine._cache_path:
            started.set()
            await release.wait()
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", blocked_read)
    task = asyncio.create_task(engine._build_bm25_index())
    await asyncio.wait_for(started.wait(), 5)
    await env.db.update_path(path, {"desc": "new dolphin"})
    release.set()
    await asyncio.wait_for(task, 5)
    assert engine.bm25_index_stale(env.db.get_index_cache_readonly())


@pytest.mark.asyncio
async def test_orphan_move_waits_for_gallery_claims_to_settle(env, monkeypatch):
    pid, src, entry = await seed_pending(env)
    cat = env.cfg.ensure_category_dir("happy") / src.name
    cat.write_bytes(src.read_bytes())
    src.unlink()
    found = asyncio.Event()
    moves = []
    original_find = env.api._find_orphan_by_hash
    original_move = pending_module.shutil.move

    def find(*args):
        result = original_find(*args)
        found.set()
        return result

    def move(*args):
        moves.append(args)
        return original_move(*args)

    monkeypatch.setattr(env.api, "_find_orphan_by_hash", find)
    monkeypatch.setattr(pending_module.shutil, "move", move)
    await env.db._write_lock.acquire()
    task = asyncio.create_task(env.api._approve_one(env.db, pid))
    try:
        await asyncio.wait_for(found.wait(), 5)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        env.db._insert_batch_sync([{**entry, "path": str(cat), "scope_mode": "local",
                                   "origin_target": "group:100", "desc": "owner-only"}])
        before = gallery_snapshot(env.db)
    finally:
        env.db._write_lock.release()
    assert not (await asyncio.wait_for(task, 5))[0]
    assert moves == []
    assert gallery_snapshot(env.db) == before
    assert cat.read_bytes() == b"owned image bytes" and not src.exists()
