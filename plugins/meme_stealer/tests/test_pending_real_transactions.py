import asyncio
import hashlib
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from meme_stealer_core.core.config.config import PluginConfig
from meme_stealer_core.core.db.database_service import DatabaseService
from meme_stealer_core.core.search.meme_search_engine import MemeSearchEngine
from meme_stealer_core.plugin_api import PluginAPI
from meme_stealer_core.web.http import RequestAdapter, bind_request, reset_request


@pytest.fixture
def env(tmp_path):
    cfg = PluginConfig({}, tmp_path / "data")
    db = DatabaseService(tmp_path / "emoji.db")
    plugin = SimpleNamespace(plugin_config=cfg, db_service=db, meme_selector=None,
                             base_dir=str(tmp_path / "data"), cache_dir=cfg.cache_dir)
    return SimpleNamespace(cfg=cfg, db=db, api=PluginAPI(plugin), plugin=plugin,
                           db_path=tmp_path / "emoji.db")


async def pending(env, name="pending.png"):
    src = env.cfg.pending_dir / name
    src.write_bytes(name.encode())
    entry = {"path": str(src), "hash": hashlib.sha256(src.read_bytes()).hexdigest(),
             "category": "happy", "desc": "开心", "scope_mode": "public"}
    pid = await env.db.insert_pending(entry)
    assert pid
    cat = env.cfg.ensure_category_dir("happy") / name
    return pid, src, cat, entry


async def post(api, action, payload):
    async def endpoint(request):
        token = bind_request(RequestAdapter(request))
        try:
            return await getattr(api, f"handle_pending_{action}")()
        finally:
            reset_request(token)
    app = Starlette(routes=[Route("/pending", endpoint, methods=["POST"])])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        return (await client.post("/pending", json=payload)).json()


async def moved(cat):
    async with asyncio.timeout(5):
        while not cat.is_file():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["db_hash", "file_hash", "both_locations", "marker_identity"])
async def test_recovery_preserves_conflicting_claim(env, conflict):
    pid, src, cat, entry = await pending(env)
    assert env.api._write_approve_marker(pid, str(src), str(cat))
    shutil.move(src, cat)
    if conflict == "db_hash":
        entry["hash"] = hashlib.sha256(b"other").hexdigest()
    if conflict == "file_hash":
        cat.write_bytes(b"other")
    if conflict == "both_locations":
        src.write_bytes(b"source still present")
    if conflict == "marker_identity":
        with sqlite3.connect(env.db_path) as conn:
            conn.execute("UPDATE emoji_pending SET path=? WHERE id=?", (str(src)+"other", pid))
    await env.db.insert_batch([{**entry, "path": str(cat)}])
    ok, error = await env.api._approve_one(env.db, pid)
    assert not ok and "冲突" in error
    assert env.db.get_pending(pid)
    assert env.db.get_emoji(str(cat))
    assert cat.is_file()
    assert Path(env.api._approve_marker_path(pid)).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject", "update"])
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_same_id_operation_waits_for_active_approval(env, action, cancel_first):
    pid, src, cat, _ = await pending(env)
    # Hold admission before any file move; the same-id operation guard stays held.
    await env.db._write_lock.acquire()
    first = asyncio.create_task(env.api._approve_one(env.db, pid))
    async with asyncio.timeout(5):
        while not getattr(env.db, "_pending_operation_locks", {}).get(pid):
            await asyncio.sleep(0)
    assert src.is_file() and not cat.exists()
    entered = asyncio.Event()
    async def followup():
        entered.set()
        return await post(PluginAPI(env.plugin), action, {"id": pid, "desc": "later"})
    second = asyncio.create_task(followup())
    await entered.wait()
    await asyncio.sleep(0.02)
    assert not second.done()
    assert env.db.get_pending(pid)
    if cancel_first:
        first.cancel()
    env.db._write_lock.release()
    if cancel_first:
        with pytest.raises(asyncio.CancelledError):
            await first
    else:
        assert (await first)[0]
    result = await asyncio.wait_for(second, 5)
    if cancel_first and action == "approve":
        assert result["approved"] == 1
    if cancel_first and action == "update":
        assert result["success"] and env.db.get_pending(pid)["desc"] == "later"
        assert src.is_file() and not cat.exists()
    elif cancel_first and action == "reject":
        assert result["success"]
        assert not src.exists() and not cat.exists() and not env.db.get_pending(pid)
    else:
        assert cat.is_file() and env.db.get_emoji(str(cat))
        assert not src.exists() and not env.db.get_pending(pid)
    assert not Path(env.api._approve_marker_path(pid)).exists()


@pytest.mark.asyncio
async def test_different_ids_use_independent_guards(env):
    pid, _, _, _ = await pending(env)
    other, _, cat, _ = await pending(env, "other.png")
    async with env.api._pending_operation(env.db, pid):
        assert (await asyncio.wait_for(env.api._approve_one(env.db, other), 5))[0]
    assert cat.is_file()


@pytest.mark.asyncio
async def test_delete_failure_keeps_file_and_marker_for_retry(env):
    pid, src, cat, entry = await pending(env)
    assert env.api._write_approve_marker(pid, str(src), str(cat))
    shutil.move(src, cat)
    await env.db.insert_batch([{**entry, "path": str(cat)}])
    # A real SQLite trigger aborts the compensation DELETE.
    with sqlite3.connect(env.db_path) as conn:
        conn.execute("CREATE TRIGGER deny_delete BEFORE DELETE ON emoji "
                     "BEGIN SELECT RAISE(ABORT, 'temporary delete failure'); END")
    assert not await env.api._rollback_approval(env.db, pid, str(src), str(cat),
                                                inserted=True, expected_hash=entry["hash"])
    assert env.db.get_emoji(str(cat)) and cat.is_file() and not src.exists()
    assert Path(env.api._approve_marker_path(pid)).is_file()
    result = await post(env.api, "reject", {"id": pid})
    assert not result["success"] and env.db.get_pending(pid)
    assert cat.is_file() and Path(env.api._approve_marker_path(pid)).is_file()
    with sqlite3.connect(env.db_path) as conn:
        conn.execute("DROP TRIGGER deny_delete")
    assert (await env.api._approve_one(env.db, pid))[0]
    assert cat.is_file() and env.db.get_emoji(str(cat)) and not env.db.get_pending(pid)


@pytest.mark.asyncio
async def test_reverse_move_failure_remains_recoverable(env):
    pid, src, cat, entry = await pending(env)
    assert env.api._write_approve_marker(pid, str(src), str(cat))
    shutil.move(src, cat)
    await env.db.insert_batch([{**entry, "path": str(cat)}])
    src.parent.rmdir()
    src.parent.write_bytes(b"filesystem obstacle")
    assert not await env.api._rollback_approval(env.db, pid, str(src), str(cat),
                                                inserted=True, expected_hash=entry["hash"])
    assert not env.db.get_emoji(str(cat)) and cat.is_file()
    assert env.db.get_pending(pid) and Path(env.api._approve_marker_path(pid)).is_file()
    src.parent.unlink()
    assert (await env.api._approve_one(env.db, pid))[0]
    assert cat.is_file() and env.db.get_emoji(str(cat)) and not env.db.get_pending(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["insert", "delete", "update", "move", "sync", "clear"])
async def test_cancelled_thread_write_settles_before_unlock_and_generation(env, operation):
    path = str(env.cfg.pending_dir / "index.png")
    entry = {"path": path, "hash": "hash", "category": "happy", "desc": "开心"}
    await env.db.insert_batch([entry])
    engine = MemeSearchEngine(env.plugin, None, None)
    await engine._build_bm25_index(env.db.get_index_cache_readonly())
    before = env.db.get_index_generation()
    blocker = sqlite3.connect(env.db_path)
    blocker.execute("BEGIN IMMEDIATE")
    calls = {
        "insert": lambda: env.db.insert_batch([{**entry, "path": path+"new", "hash": "new"}]),
        "delete": lambda: env.db.delete_paths([path]),
        "update": lambda: env.db.update_path(path, {"desc": "伤心"}),
        "move": lambda: env.db.move_path(path, path+"moved", "happy"),
        "sync": lambda: env.db.sync_index({"happy": {path: {**entry, "desc": "伤心"}}}),
        "clear": env.db.clear_all,
    }
    task = asyncio.create_task(calls[operation]())
    try:
        await asyncio.sleep(0.05)
        assert not task.done() and env.db._write_lock.locked()
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and env.db._write_lock.locked()
    finally:
        blocker.rollback()
        blocker.close()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not env.db._write_lock.locked()
    assert env.db.get_index_generation() == before + 1
    assert engine.bm25_index_stale(env.db.get_index_cache_readonly())
