"""Real API file writers must share approval ownership and request generations."""

import asyncio
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meme_stealer_core.api import image_mutations, pending, taxonomy
from meme_stealer_core.core.config.config import PluginConfig
from meme_stealer_core.core.db.database_service import DatabaseService
from meme_stealer_core.core.search.meme_selector import MemeSelector
from meme_stealer_core.host import StealerEvent
from meme_stealer_core.plugin_api import PluginAPI
import meme_stealer_core.plugin_api as api_module
from meme_stealer_core.core.util import safe_io


@pytest.fixture
def env(tmp_path):
    cfg = PluginConfig({}, tmp_path / "data")
    db = DatabaseService(tmp_path / "emoji.db")
    plugin = SimpleNamespace(
        plugin_config=cfg, db_service=db, meme_selector=None,
        base_dir=tmp_path / "data", cache_dir=cfg.cache_dir,
        enable_embedding_search=False, models=None,
    )
    plugin.update_config = lambda changes: [setattr(cfg, key, value) for key, value in changes.items()]
    return SimpleNamespace(cfg=cfg, db=db, plugin=plugin, api=PluginAPI(plugin))


def set_request(monkeypatch, payload, started=None):
    async def get_json():
        if started:
            started.set()
        return payload

    for module in (image_mutations, taxonomy):
        monkeypatch.setattr(module, "request", SimpleNamespace(get_json=get_json))
        monkeypatch.setattr(module, "jsonify", lambda value: value)


async def add_pending(env):
    src = env.cfg.pending_dir / "same.png"
    src.write_bytes(b"A pending bytes")
    image_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    pid = await env.db.insert_pending({
        "path": str(src), "hash": image_hash, "category": "happy", "scope_mode": "public",
    })
    return pid, src


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["handle_update_image", "handle_batch_move"])
async def test_real_category_move_waits_until_approval_compensation_finishes(env, monkeypatch, route):
    pid, src = await add_pending(env)
    old = env.cfg.ensure_category_dir("angry") / src.name
    raw = b"B gallery bytes"
    old.write_bytes(raw)
    image_hash = hashlib.sha256(raw).hexdigest()
    await env.db.insert_batch([{
        "path": str(old), "hash": image_hash, "category": "angry", "scope_mode": "public",
    }])
    target = env.cfg.ensure_category_dir("happy") / src.name
    started, release = threading.Event(), threading.Event()
    original_move = pending.shutil.move

    def blocked_move(source, destination):
        if str(source) == str(src):
            started.set()
            assert release.wait(5)
        return original_move(source, destination)

    monkeypatch.setattr(pending.shutil, "move", blocked_move)
    read = asyncio.Event()
    set_request(monkeypatch, {"hash": image_hash, "hashes": [image_hash], "category": "happy"}, read)
    approval = asyncio.create_task(env.api._approve_one(env.db, pid))
    mover = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        mover = asyncio.create_task(getattr(env.api, route)())
        await asyncio.wait_for(read.wait(), 5)
        await asyncio.sleep(0.03)
        approval.cancel()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(approval, 5)
        if mover:
            result = await asyncio.wait_for(mover, 5)
    assert result["success"]
    assert src.read_bytes() == b"A pending bytes"
    assert target.read_bytes() == raw
    assert not old.exists()
    assert env.db.get_emoji(str(target))["hash"] == image_hash
    assert env.db.get_emoji(str(old)) is None
    assert env.db.get_pending(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["handle_delete_image", "handle_batch_delete", "handle_delete_category"])
async def test_real_delete_waits_until_inserted_approval_has_compensated(env, monkeypatch, route):
    pid, src = await add_pending(env)
    target = env.cfg.ensure_category_dir("happy") / src.name
    started, release = threading.Event(), threading.Event()
    original_insert = env.db._insert_batch_sync

    def blocked_insert(entries, *args, **kwargs):
        result = original_insert(entries, *args, **kwargs)
        started.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(env.db, "_insert_batch_sync", blocked_insert)
    read = asyncio.Event()
    image_hash = env.db.get_pending(pid)["hash"]
    set_request(monkeypatch, {"hash": image_hash, "hashes": [image_hash], "key": "happy"}, read)
    approval = asyncio.create_task(env.api._approve_one(env.db, pid))
    deleter = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        deleter = asyncio.create_task(getattr(env.api, route)())
        await asyncio.wait_for(read.wait(), 5)
        await asyncio.sleep(0.03)
        approval.cancel()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(approval, 5)
        if deleter:
            await asyncio.wait_for(deleter, 5)
    assert src.read_bytes() == b"A pending bytes"
    assert not target.exists()
    assert env.db.get_pending(pid)
    assert env.db.get_emoji(str(target)) is None
    assert not env.api._read_approve_marker(pid)


async def add_gallery(env, category="angry"):
    path = env.cfg.ensure_category_dir(category) / "existing.png"
    raw = b"existing gallery bytes"
    path.write_bytes(raw)
    image_hash = hashlib.sha256(raw).hexdigest()
    await env.db.insert_batch([{
        "path": str(path), "hash": image_hash, "category": category, "scope_mode": "public",
    }])
    return path, image_hash, raw


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [
    "handle_update_image", "handle_batch_move", "handle_delete_image",
    "handle_batch_delete", "handle_delete_category",
])
async def test_cancelled_real_gallery_writer_settles_files_and_sqlite(env, monkeypatch, route):
    moving = route in {"handle_update_image", "handle_batch_move"}
    old, image_hash, raw = await add_gallery(env)
    target = env.cfg.ensure_category_dir("happy") / old.name
    started, release = threading.Event(), threading.Event()
    if moving:
        original = api_module.shutil.move

        def blocked_move(source, destination):
            if str(source) == str(old):
                started.set()
                assert release.wait(5)
            return original(source, destination)

        monkeypatch.setattr(api_module.shutil, "move", blocked_move)
    else:
        original = safe_io.os.remove

        def blocked_remove(path):
            if str(path) == str(old):
                started.set()
                assert release.wait(5)
            return original(path)

        monkeypatch.setattr(safe_io.os, "remove", blocked_remove)
    set_request(monkeypatch, {
        "hash": image_hash, "hashes": [image_hash], "category": "happy", "key": "angry",
    })
    task = asyncio.create_task(getattr(env.api, route)())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert env.db._write_lock.locked()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert not env.db._write_lock.locked()
    assert not old.exists() and env.db.get_emoji(str(old)) is None
    if moving:
        assert target.read_bytes() == raw
        assert env.db.get_emoji(str(target))["hash"] == image_hash
    else:
        assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["handle_update_image", "handle_batch_move"])
async def test_real_move_restores_source_before_releasing_gate_on_sqlite_failure(env, monkeypatch, route):
    old, image_hash, raw = await add_gallery(env)
    target = env.cfg.ensure_category_dir("happy") / old.name

    def fail_move(*_args):
        assert env.db._write_lock.locked()
        assert target.read_bytes() == raw and not old.exists()
        return False

    monkeypatch.setattr(env.db, "_move_path_sync", fail_move)
    set_request(monkeypatch, {"hash": image_hash, "hashes": [image_hash], "category": "happy"})
    result = await getattr(env.api, route)()
    assert not result.get("count", 0)
    assert old.read_bytes() == raw and not target.exists()
    assert env.db.get_emoji(str(old))["hash"] == image_hash
    assert env.db.get_emoji(str(target)) is None
    assert not env.db._write_lock.locked()


@pytest.mark.asyncio
async def test_category_delete_preserves_unknown_or_inflight_files(env, monkeypatch):
    owned, _image_hash, _raw = await add_gallery(env)
    unknown = owned.with_name("import-still-registering.png")
    unknown.write_bytes(b"unowned import bytes")
    warnings = []
    monkeypatch.setattr(api_module.logger, "warning", warnings.append)
    set_request(monkeypatch, {"key": "angry"})
    result = await env.api.handle_delete_category()
    assert result["success"] and result["deleted_files"] == 1
    assert not owned.exists() and env.db.get_emoji(str(owned)) is None
    assert unknown.read_bytes() == b"unowned import bytes"
    assert any("分类目录保留" in message for message in warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["handle_batch_delete", "handle_delete_category"])
@pytest.mark.parametrize("partial", [False, True])
async def test_delete_reports_sqlite_failure_and_refreshes_partial_success(env, monkeypatch, route, partial):
    first, image_hash, raw = await add_gallery(env)
    hashes = [image_hash]
    failing = first
    if partial:
        failing = first.with_name("second.png")
        failing.write_bytes(raw + b" second")
        second_hash = hashlib.sha256(failing.read_bytes()).hexdigest()
        hashes.append(second_hash)
        await env.db.insert_batch([{
            "path": str(failing), "hash": second_hash, "category": "angry", "scope_mode": "public",
        }])
    original = env.db._delete_paths_sync

    def fail_delete(paths, *args, **kwargs):
        if str(failing) in paths:
            raise RuntimeError("injected SQLite delete failure")
        return original(paths, *args, **kwargs)

    refresh = AsyncMock()
    monkeypatch.setattr(env.db, "_delete_paths_sync", fail_delete)
    monkeypatch.setattr(api_module, "refresh_search_entry", refresh)
    set_request(monkeypatch, {"hashes": hashes, "key": "angry"})
    result = await getattr(env.api, route)()
    assert not result["success"]
    assert "injected SQLite delete failure" in result["error"]
    assert "angry" in env.cfg.categories
    assert env.db.get_emoji(str(failing)) is not None
    assert not env.db._write_lock.locked()
    if partial:
        assert not first.exists() and env.db.get_emoji(str(first)) is None
        refresh.assert_awaited_once_with(env.plugin, str(first))
    else:
        refresh.assert_not_awaited()


async def seed_semantic(env):
    path = env.cfg.ensure_category_dir("happy") / "semantic.png"
    path.write_bytes(b"semantic bytes")
    await env.db.insert_batch([{
        "path": str(path), "hash": "semantic-hash", "category": "happy",
        "desc": "old cactus", "scope_mode": "public", "origin_target": "group:200",
    }])
    selector = MemeSelector(env.plugin)
    event = StealerEvent("bot_a", SimpleNamespace(group_id="100", user_id="123"))
    return str(path), selector, event


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [{"scope_mode": "local"}, {"allowed_bots": "bot_b"}])
async def test_semantic_candidates_refresh_policy_after_direct_embedding_wait(env, updates):
    path, selector, event = await seed_semantic(env)
    started, release = asyncio.Event(), asyncio.Event()

    class Embedding:
        def is_available(self):
            return True

        async def search(self, *_args, **_kwargs):
            started.set()
            await release.wait()
            return [(path, 0.9)]

    selector._smart_select_service._embedding_service = Embedding()
    task = asyncio.create_task(selector.semantic_candidates("cactus", limit=1, event=event))
    await asyncio.wait_for(started.wait(), 5)
    try:
        await env.db.update_path(path, updates)
    finally:
        release.set()
    assert await asyncio.wait_for(task, 5) == []


@pytest.mark.asyncio
async def test_semantic_candidates_refresh_metadata_after_fallback_wait(env, monkeypatch):
    path, selector, event = await seed_semantic(env)
    started, release = asyncio.Event(), asyncio.Event()

    async def fallback(*_args, **_kwargs):
        started.set()
        await release.wait()
        return [(path, "new dolphin", "happy", "")]

    monkeypatch.setattr(selector, "search_images", fallback)
    task = asyncio.create_task(selector.semantic_candidates("dolphin", limit=1, event=event))
    await asyncio.wait_for(started.wait(), 5)
    try:
        await env.db.update_path(path, {"desc": "new dolphin"})
    finally:
        release.set()
    results = await asyncio.wait_for(task, 5)
    assert results[0][1]["desc"] == "new dolphin"


@pytest.mark.asyncio
async def test_search_finishes_current_bm25_build_when_generation_changes_during_cache_read(env, monkeypatch):
    path, selector, event = await seed_semantic(env)
    engine = selector._search_engine
    await engine._build_bm25_index()
    engine._invalidate_bm25_index()
    started, release = asyncio.Event(), asyncio.Event()
    original = asyncio.to_thread

    async def blocked_read(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "read_text" and getattr(func, "__self__", None) == engine._cache_path:
            started.set()
            await release.wait()
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", blocked_read)
    task = asyncio.create_task(selector.search_images("dolphin", event=event))
    await asyncio.wait_for(started.wait(), 5)
    try:
        await env.db.update_path(path, {"desc": "new dolphin"})
    finally:
        release.set()
    results = await asyncio.wait_for(task, 5)
    assert results and results[0][1] == "new dolphin"
    assert not engine.bm25_index_stale(engine.get_index())


@pytest.mark.asyncio
async def test_cache_write_generation_change_rebuilds_once_without_more_cache_io(env, monkeypatch):
    path, selector, event = await seed_semantic(env)
    engine = selector._search_engine
    started, release = threading.Event(), threading.Event()
    calls = []
    original = engine._save_bm25_cache

    def blocked_save(data):
        calls.append(data["signature"])
        started.set()
        assert release.wait(5)
        original(data)

    monkeypatch.setattr(engine, "_save_bm25_cache", blocked_save)
    task = asyncio.create_task(selector.search_images("dolphin", event=event))
    assert await asyncio.to_thread(started.wait, 5)
    try:
        await env.db.update_path(path, {"desc": "new dolphin", "scope_mode": "local"})
    finally:
        release.set()
    assert await asyncio.wait_for(task, 5) == []
    assert len(calls) == 1
    assert not engine.bm25_index_stale(engine.get_index())
