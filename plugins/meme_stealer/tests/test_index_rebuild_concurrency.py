"""Rebuild must not overwrite gallery owners or policies changed during a scan."""

import asyncio
import hashlib
import threading
from types import SimpleNamespace

import pytest

from meme_stealer_core.api import image_mutations
from meme_stealer_core.core.commands import index_rebuild_command
from meme_stealer_core.core.commands.index_rebuild_command import IndexRebuildCommand
from meme_stealer_core.core.config.config import PluginConfig
from meme_stealer_core.core.db.database_service import DatabaseService
from meme_stealer_core.core.db.index_manager import IndexManager
from meme_stealer_core.plugin_api import PluginAPI


@pytest.fixture
async def env(tmp_path):
    cfg = PluginConfig({"max_reg_num": 100}, tmp_path / "data")
    db = DatabaseService(tmp_path / "emoji.db")
    target = cfg.ensure_category_dir("happy") / "same.png"
    target.write_bytes(b"original bytes")
    old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    await db.insert_batch([{
        "path": str(target), "hash": old_hash, "category": "happy",
        "desc": "original description", "tags": ["old"], "scope_mode": "public",
    }])
    plugin = SimpleNamespace(
        plugin_config=cfg, db_service=db, base_dir=cfg.data_dir,
        cache_dir=cfg.cache_dir, categories_dir=cfg.categories_dir, meme_selector=None,
    )
    plugin.index_manager = IndexManager(plugin)
    return SimpleNamespace(
        plugin=plugin, cfg=cfg, db=db, target=target, old_hash=old_hash,
        command=IndexRebuildCommand(plugin), api=PluginAPI(plugin),
    )


async def run_rebuild(env):
    event = SimpleNamespace(plain_result=lambda text: text)
    return [text async for text in env.command.rebuild_index(event)]


def pause_scan(env, monkeypatch):
    scanned, release = asyncio.Event(), asyncio.Event()
    original = env.plugin.index_manager.rebuild_index_from_files

    async def blocked_scan():
        result = await original()
        scanned.set()
        await release.wait()
        return result

    monkeypatch.setattr(env.plugin.index_manager, "rebuild_index_from_files", blocked_scan)
    return scanned, release


async def replace_with_pending(env, monkeypatch):
    src = env.cfg.pending_dir / env.target.name
    src.write_bytes(b"new pending bytes")
    new_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    pid = await env.db.insert_pending({
        "path": str(src), "hash": new_hash, "category": "happy",
        "desc": "new description", "tags": ["new"], "scope_mode": "local",
        "origin_target": "group:200",
    })

    async def get_json():
        return {"hash": env.old_hash}

    monkeypatch.setattr(image_mutations, "request", SimpleNamespace(get_json=get_json))
    monkeypatch.setattr(image_mutations, "jsonify", lambda result: result)
    await env.api.handle_delete_image()
    assert await env.api._approve_one(env.db, pid) == (True, "")
    await env.db.update_path(str(env.target), {"allowed_bots": "bot_b"})
    return env.db.get_emoji(str(env.target))


@pytest.mark.asyncio
async def test_rebuild_preserves_pending_path_owner_approved_after_scan(env, monkeypatch):
    scanned, release = pause_scan(env, monkeypatch)
    task = asyncio.create_task(run_rebuild(env))
    try:
        await asyncio.wait_for(scanned.wait(), 5)
        expected = await replace_with_pending(env, monkeypatch)
    finally:
        release.set()
        messages = await asyncio.wait_for(task, 5)
    assert messages[-1].startswith("✅")
    assert env.db.get_emoji(str(env.target)) == expected
    assert env.target.read_bytes() == b"new pending bytes"


@pytest.mark.asyncio
async def test_rebuild_preserves_policy_edits_for_same_content_hash(env, monkeypatch):
    scanned, release = pause_scan(env, monkeypatch)
    task = asyncio.create_task(run_rebuild(env))
    try:
        await asyncio.wait_for(scanned.wait(), 5)
        await env.db.update_path(str(env.target), {
            "scope_mode": "local", "origin_target": "group:200", "allowed_bots": "bot_b",
            "is_favorite": 1, "desc": "updated description", "tags": ["updated"],
        })
        expected = env.db.get_emoji(str(env.target))
    finally:
        release.set()
        messages = await asyncio.wait_for(task, 5)
    assert messages[-1].startswith("✅")
    assert env.db.get_emoji(str(env.target)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_has_file", [False, True])
async def test_rebuild_missing_cleanup_preserves_new_owner(env, monkeypatch, replacement_has_file):
    missing = env.target.with_name("missing.png")
    await env.db.insert_batch([{
        "path": str(missing), "hash": hashlib.sha256(b"missing old").hexdigest(),
        "category": "happy", "desc": "missing old",
    }])
    paused, release = asyncio.Event(), asyncio.Event()
    original = env.command._run_rebuild_write

    async def blocked_write(path, operation):
        if path == str(missing):
            paused.set()
            await release.wait()
        return await original(path, operation)

    monkeypatch.setattr(env.command, "_run_rebuild_write", blocked_write)
    task = asyncio.create_task(run_rebuild(env))
    try:
        await asyncio.wait_for(paused.wait(), 5)
        await env.db.delete_paths([str(missing)])
        if replacement_has_file:
            missing.write_bytes(b"new missing-path owner")
        await env.db.insert_batch([{
            "path": str(missing), "hash": hashlib.sha256(b"new missing-path owner").hexdigest(),
            "category": "happy", "desc": "new owner", "scope_mode": "local",
        }])
        expected = env.db.get_emoji(str(missing))
    finally:
        release.set()
        messages = await asyncio.wait_for(task, 5)
    assert messages[-1].startswith("✅")
    assert env.db.get_emoji(str(missing)) == expected
    assert missing.exists() == replacement_has_file


@pytest.mark.asyncio
async def test_rebuild_revalidates_file_hash_before_writing(env, monkeypatch):
    scanned, release = pause_scan(env, monkeypatch)
    task = asyncio.create_task(run_rebuild(env))
    try:
        await asyncio.wait_for(scanned.wait(), 5)
        env.target.write_bytes(b"file changed outside scan")
        expected = env.db.get_emoji(str(env.target))
    finally:
        release.set()
        messages = await asyncio.wait_for(task, 5)
    assert messages[-1].startswith("✅")
    assert env.db.get_emoji(str(env.target)) == expected
    assert env.target.read_bytes() == b"file changed outside scan"


@pytest.mark.asyncio
@pytest.mark.parametrize("unverified_hash", ["", "legacy-filename-hash"])
async def test_rebuild_keeps_missing_rows_without_verified_owner_hash(env, unverified_hash):
    missing = env.target.with_name("unverified.png")
    await env.db.insert_batch([{
        "path": str(missing), "hash": unverified_hash, "category": "happy",
    }])
    expected = env.db.get_emoji(str(missing))
    messages = await run_rebuild(env)
    assert messages[-1].startswith("✅")
    assert env.db.get_emoji(str(missing)) == expected


@pytest.mark.asyncio
async def test_rebuild_refreshes_saved_and_pruned_entries_outside_gallery_gate(env, monkeypatch):
    missing = env.target.with_name("missing.png")
    await env.db.insert_batch([{
        "path": str(missing), "hash": hashlib.sha256(b"missing").hexdigest(),
        "category": "happy",
    }])
    refreshed = []

    async def refresh(plugin, path):
        assert not plugin.db_service._write_lock.locked()
        refreshed.append(path)

    monkeypatch.setattr(index_rebuild_command, "refresh_search_entry", refresh)
    messages = await run_rebuild(env)
    assert messages[-1].startswith("✅")
    assert set(refreshed) == {str(env.target), str(missing)}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["save", "prune"])
async def test_rebuild_cancellation_settles_sql_before_releasing_gate(env, monkeypatch, operation):
    started, release = threading.Event(), threading.Event()
    original = env.db._sync_index_sync if operation == "save" else env.db._delete_paths_sync

    def blocked_sql(*args):
        started.set()
        if not release.wait(5):
            raise RuntimeError("test did not release SQL")
        return original(*args)

    target = env.target
    if operation == "save":
        monkeypatch.setattr(env.db, "_sync_index_sync", blocked_sql)
    else:
        target = env.target.with_name("missing.png")
        await env.db.insert_batch([{
            "path": str(target), "hash": hashlib.sha256(b"missing").hexdigest(),
            "category": "happy",
        }])
        monkeypatch.setattr(env.db, "_delete_paths_sync", blocked_sql)
    task = asyncio.create_task(run_rebuild(env))
    competitor = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        competitor = asyncio.create_task(env.db.insert_batch([{
            "path": str(target), "hash": hashlib.sha256(b"replacement").hexdigest(),
            "category": "happy", "desc": "replacement after cancellation",
        }]))
        await asyncio.sleep(0)
        assert env.db._write_lock.locked()
        assert not competitor.done()
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        if competitor is not None:
            await asyncio.wait_for(competitor, 5)
    assert env.db.get_emoji(str(target))["desc"] == "replacement after cancellation"
