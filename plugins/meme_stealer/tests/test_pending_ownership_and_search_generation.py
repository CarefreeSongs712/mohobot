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
            while not getattr(env.db, "_pending_operation_locks", {}).get(pid):
                await asyncio.sleep(0)
        await asyncio.sleep(0.02)
        assert src.is_file() and not cat.exists()
        owned.write_bytes(src.read_bytes())
        # A competing real SQLite writer registers the path/hash before any move.
        env.db._insert_batch_sync([{**entry, "path": str(owned), "scope_mode": "local",
                                   "origin_target": "group:100", "desc": "owner-only"}])
        before = gallery_snapshot(env.db)
    finally:
        env.db._write_lock.release()
    ok, _error = await asyncio.wait_for(task, 5)
    assert not ok
    assert gallery_snapshot(env.db) == before
    assert env.db.get_pending(pid)
    assert src.read_bytes() == b"owned image bytes"
    assert owned.read_bytes() == b"owned image bytes"
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
    # Normalize old requests to current metadata, never downgrade shared state.
    await engine._build_bm25_index(old)
    assert not engine.bm25_index_stale(env.db.get_index_cache_readonly())
    current_shared = engine._bm25_index
    await engine._build_bm25_index(old)
    assert engine._bm25_index is current_shared


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
    assert not engine.bm25_index_stale(env.db.get_index_cache_readonly())


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


@pytest.mark.asyncio
async def test_cancelled_approval_and_different_hash_writer_keep_both_files(env, monkeypatch):
    import threading

    pid, src, entry = await seed_pending(env)
    cat = env.cfg.ensure_category_dir("happy") / src.name
    move_started, release_move = threading.Event(), threading.Event()
    original_move = pending_module.shutil.move

    def blocked_move(source, target):
        if str(source) == str(src):
            move_started.set()
            assert release_move.wait(5)
        return original_move(source, target)

    monkeypatch.setattr(pending_module.shutil, "move", blocked_move)
    approval = asyncio.create_task(env.api._approve_one(env.db, pid))
    assert await asyncio.to_thread(move_started.wait, 5)
    attempted = asyncio.Event()
    other_bytes = b"different gallery owner"
    other_hash = hashlib.sha256(other_bytes).hexdigest()

    async def competing_writer():
        attempted.set()
        async with env.db._write_lock:
            cat.write_bytes(other_bytes)
            env.db._insert_batch_sync([{**entry, "path": str(cat), "hash": other_hash,
                                       "desc": "other owner"}])
            env.db._bump_index_generation()

    writer = asyncio.create_task(competing_writer())
    try:
        await attempted.wait()
        await asyncio.sleep(0.02)
        approval.cancel()
    finally:
        release_move.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(approval, 5)
    await asyncio.wait_for(writer, 5)
    assert src.read_bytes() == b"owned image bytes"
    assert cat.read_bytes() == other_bytes
    assert env.db.get_emoji(str(cat))["hash"] == other_hash
    assert env.db.get_pending(pid)
    assert not env.api._read_approve_marker(pid)


@pytest.mark.asyncio
async def test_rollback_rechecks_owner_after_waiting_for_writer_gate(env):
    pid, src, entry = await seed_pending(env)
    cat = env.cfg.ensure_category_dir("happy") / src.name
    src.replace(cat)
    await env.db.insert_batch([{**entry, "path": str(cat)}])
    assert env.api._write_approve_marker(pid, str(src), str(cat))
    await env.db._write_lock.acquire()
    rollback = asyncio.create_task(env.api._rollback_approval(
        env.db, pid, str(src), str(cat), inserted=True, expected_hash=entry["hash"],
    ))
    other_bytes = b"replacement gallery file"
    other_hash = hashlib.sha256(other_bytes).hexdigest()
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        cat.write_bytes(other_bytes)
        env.db._insert_batch_sync([{**entry, "path": str(cat), "hash": other_hash}])
    finally:
        env.db._write_lock.release()
    assert not await asyncio.wait_for(rollback, 5)
    assert env.db.get_emoji(str(cat))["hash"] == other_hash
    assert cat.read_bytes() == other_bytes and not src.exists()
    assert env.api._read_approve_marker(pid)


@pytest.mark.asyncio
async def test_old_search_resumes_after_new_request_rebuilds_without_metadata_mix(env):
    path, engine = await seed_search(env)
    old_snapshot = engine.get_index()
    started, release = asyncio.Event(), asyncio.Event()

    class Embedding:
        def is_available(self):
            return True

        async def search(self, *_args, **_kwargs):
            started.set()
            await release.wait()
            return []

    class PublicOnlyScope:
        def _is_entry_allowed_for_event(self, data, event):
            return data.get("scope_mode") != "local"

    scope = PublicOnlyScope()
    engine._scope_service = scope
    old_search = MemeSmartSelectService(env.plugin, engine, Selection(), scope)
    old_search._embedding_service = Embedding()
    task = asyncio.create_task(old_search.search_images("dolphin", idx=old_snapshot))
    await asyncio.wait_for(started.wait(), 5)
    try:
        await env.db.insert_batch([{**env.db.get_emoji(path), "desc": "new dolphin",
                                    "scope_mode": "local", "origin_target": "group:200"}])
        assert env.db.get_emoji(path)["scope_mode"] == "local"
        new_search = MemeSmartSelectService(env.plugin, engine, Selection(), Scope())
        new_search._embedding_service = None
        fresh = await new_search.search_images("dolphin")
        assert fresh and fresh[0][1] == "new dolphin"
        shared_new_index = engine._bm25_index
        assert engine.bm25_index_stale(old_snapshot)
    finally:
        release.set()
    assert await asyncio.wait_for(task, 5) == []
    assert engine._bm25_index is shared_new_index
    assert engine._bm25_generation == env.db.get_index_generation()


@pytest.mark.asyncio
async def test_rollback_delete_is_conditioned_on_hash_inside_sql_transaction(env, monkeypatch):
    pid, src, entry = await seed_pending(env)
    cat = env.cfg.ensure_category_dir("happy") / src.name
    src.replace(cat)
    await env.db.insert_batch([{**entry, "path": str(cat)}])
    assert env.api._write_approve_marker(pid, str(src), str(cat))
    original_delete = env.db._delete_paths_sync
    other_bytes = b"writer after ownership read"
    other_hash = hashlib.sha256(other_bytes).hexdigest()

    def external_replace_then_delete(paths, *args, **kwargs):
        # A separate SQLite connection changes ownership after the API's read,
        # before DELETE begins. The predicate must be checked by SQLite itself.
        with env.db._get_connection() as conn:
            conn.execute("UPDATE emoji SET hash = ? WHERE path = ?", (other_hash, str(cat)))
        cat.write_bytes(other_bytes)
        return original_delete(paths, *args, **kwargs)

    monkeypatch.setattr(env.db, "_delete_paths_sync", external_replace_then_delete)
    assert not await env.api._rollback_approval(
        env.db, pid, str(src), str(cat), inserted=True, expected_hash=entry["hash"],
    )
    assert env.db.get_emoji(str(cat))["hash"] == other_hash
    assert cat.read_bytes() == other_bytes and not src.exists()
    assert env.api._read_approve_marker(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("scope_mode", "local"),
                                         ("origin_target", "group:200"),
                                         ("allowed_bots", "bot_002")])
async def test_policy_only_sync_retires_request_snapshot_without_changing_bm25(env, field, value):
    path, engine = await seed_search(env)
    old = engine.get_index()
    before = env.db.get_index_generation()
    assert await env.db.sync_index({path: {field: value}}) == [path]
    assert env.db.get_index_generation() > before
    latest = engine.current_index(old)
    assert latest[path][field] == value and latest is not old
    assert engine.bm25_index_stale(old)
    assert not engine.bm25_index_stale(latest)  # Token corpus did not change.
