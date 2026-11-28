"""Capacity and command deletions must preserve a newly approved path owner."""

import asyncio
import hashlib
import threading
from types import SimpleNamespace

import pytest

from meme_stealer_core.api import image_mutations
from meme_stealer_core.core.commands.command_handler import CommandHandler
from meme_stealer_core.core.commands.image_mgmt_command import ImageManagementCommand
from meme_stealer_core.core.commands import image_mgmt_command
from meme_stealer_core.core.config.config import PluginConfig
from meme_stealer_core.core.db.database_service import DatabaseService
from meme_stealer_core.core.db.index_manager import IndexManager
from meme_stealer_core.core.db import index_manager
from meme_stealer_core.core.events.event_handler import EventHandler
from meme_stealer_core.core.events import event_handler
from meme_stealer_core.core.util import safe_io
from meme_stealer_core.plugin_api import PluginAPI


@pytest.fixture
async def env(tmp_path):
    cfg = PluginConfig({"max_reg_num": 1}, tmp_path / "data")
    db = DatabaseService(tmp_path / "emoji.db")
    plugin = SimpleNamespace(
        plugin_config=cfg, db_service=db, meme_selector=None,
        base_dir=cfg.data_dir, cache_dir=cfg.cache_dir, categories_dir=cfg.categories_dir,
    )
    plugin.index_manager = IndexManager(plugin)
    plugin.event_handler = EventHandler(plugin)
    target = cfg.ensure_category_dir("happy") / "same.png"
    target.write_bytes(b"old gallery bytes")
    old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    keeper = target.with_name("keeper.png")
    keeper.write_bytes(b"frequently used gallery bytes")
    await db.insert_batch([
        {"path": str(target), "hash": old_hash, "category": "happy", "created_at": 1,
         "desc": "old description", "tags": ["old"], "scope_mode": "public"},
        {"path": str(keeper), "hash": hashlib.sha256(keeper.read_bytes()).hexdigest(),
         "category": "happy", "created_at": 2, "use_count": 100},
    ])
    src = cfg.pending_dir / target.name
    src.write_bytes(b"new pending bytes")
    new_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    pid = await db.insert_pending({
        "path": str(src), "hash": new_hash, "category": "happy",
        "desc": "new description", "tags": ["new"], "scope_mode": "local",
        "origin_target": "group:200", "allowed_bots": "bot_b",
    })
    return SimpleNamespace(
        cfg=cfg, db=db, plugin=plugin, api=PluginAPI(plugin), target=target,
        old_hash=old_hash, new_hash=new_hash, src=src, pid=pid, keeper=keeper,
    )


async def run_command(env, operation):
    event = SimpleNamespace(plain_result=lambda text: text)
    if operation == "capacity":
        results = CommandHandler(env.plugin).enforce_capacity(event)
    else:
        command = ImageManagementCommand(env.plugin)
        method = command.delete_image if operation == "delete" else command.blacklist_image
        results = method(event, str(env.target))
    return [message async for message in results]


def prepare_api_delete(env, monkeypatch):
    read = asyncio.Event()

    async def get_json():
        read.set()
        return {"hash": env.old_hash}

    monkeypatch.setattr(image_mutations, "request", SimpleNamespace(get_json=get_json))
    monkeypatch.setattr(image_mutations, "jsonify", lambda value: value)
    return read


async def replace_with_pending(env):
    await env.api.handle_delete_image()
    result = await env.api._approve_one(env.db, env.pid)
    assert result == (True, "")
    await env.db.update_path(str(env.target), {"allowed_bots": "bot_b"})


def assert_new_owner(env):
    assert env.db.get_pending(env.pid) is None
    assert not env.src.exists()
    assert env.target.read_bytes() == b"new pending bytes"
    current = env.db.get_emoji(str(env.target))
    assert current is not None
    for key, expected in {
        "hash": env.new_hash, "desc": "new description", "tags": ["new"],
        "scope_mode": "local", "origin_target": "group:200", "allowed_bots": "bot_b",
    }.items():
        assert current[key] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["capacity", "delete", "blacklist"])
async def test_unlink_serializes_real_api_delete_and_pending_path_reuse(env, monkeypatch, operation):
    started, release = asyncio.Event(), asyncio.Event()
    original_remove = safe_io.safe_remove_file

    async def blocked_remove(path):
        if str(path) == str(env.target):
            started.set()
            await release.wait()
        return await original_remove(path)

    for module in (event_handler, image_mgmt_command, index_manager):
        monkeypatch.setattr(module, "safe_remove_file", blocked_remove, raising=False)
    read = prepare_api_delete(env, monkeypatch)
    command = asyncio.create_task(run_command(env, operation))
    replacement = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        gated = env.db._write_lock.locked()
        replacement = asyncio.create_task(replace_with_pending(env))
        await asyncio.wait_for(read.wait(), 5)
        if not gated:
            # On the broken implementation the actual API and approval both
            # commit before the old deletion resumes, reproducing the loss.
            await asyncio.wait_for(replacement, 5)
    finally:
        release.set()
        await asyncio.wait_for(command, 5)
        if replacement:
            await asyncio.wait_for(replacement, 5)
    assert_new_owner(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["capacity", "delete", "blacklist"])
@pytest.mark.parametrize("stage", ["unlink", "sqlite"])
async def test_cancellation_settles_real_unlink_and_sqlite_before_releasing_gate(
    env, monkeypatch, operation, stage,
):
    started, release = threading.Event(), threading.Event()
    if stage == "unlink":
        original_remove = safe_io.os.remove

        def blocked_remove(path):
            if str(path) == str(env.target):
                started.set()
                assert release.wait(5)
            return original_remove(path)

        monkeypatch.setattr(safe_io.os, "remove", blocked_remove)
    else:
        original_delete = env.db._delete_paths_sync

        def blocked_delete(paths, expected_hash=None):
            if str(env.target) in paths:
                started.set()
                assert release.wait(5)
            return original_delete(paths, expected_hash)

        monkeypatch.setattr(env.db, "_delete_paths_sync", blocked_delete)
    read = prepare_api_delete(env, monkeypatch)
    command = asyncio.create_task(run_command(env, operation))
    replacement = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        command.cancel()
        await asyncio.sleep(0)
        command.cancel()
        await asyncio.sleep(0)
        assert env.db._write_lock.locked()
        assert not command.done()
        replacement = asyncio.create_task(replace_with_pending(env))
        await asyncio.wait_for(read.wait(), 5)
        assert not replacement.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(command, 5)
        if replacement:
            await asyncio.wait_for(replacement, 5)
    assert not env.db._write_lock.locked()
    assert_new_owner(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["capacity", "delete", "blacklist"])
async def test_file_content_conflict_preserves_current_row_and_file(env, operation):
    env.target.write_bytes(b"different unindexed content")
    await run_command(env, operation)
    assert env.target.read_bytes() == b"different unindexed content"
    assert env.db.get_emoji(str(env.target))["hash"] == env.old_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"is_favorite": True}, {"character": "alice"},
    {"retention_class": "external"}, {"retention_class": "pinned"},
])
async def test_capacity_rechecks_new_protection_after_candidate_selection(env, updates):
    started, release = asyncio.Event(), asyncio.Event()
    command = None

    class DelayedCapacityGate(asyncio.Lock):
        async def __aenter__(self):
            if asyncio.current_task() is command:
                started.set()
                await release.wait()
            return await super().__aenter__()

    env.db._write_lock = DelayedCapacityGate()
    command = asyncio.create_task(run_command(env, "capacity"))
    try:
        await asyncio.wait_for(started.wait(), 5)
        await env.db.update_path(str(env.target), updates)
    finally:
        release.set()
        await asyncio.wait_for(command, 5)
    assert env.target.read_bytes() == b"old gallery bytes"
    current = env.db.get_emoji(str(env.target))
    assert current["hash"] == env.old_hash
    assert all(current[key] == value for key, value in updates.items())


@pytest.mark.asyncio
@pytest.mark.parametrize("old_hash", ["", "not-a-content-hash"])
async def test_capacity_conservatively_skips_unverifiable_legacy_owner(env, old_hash):
    await env.db.update_path(str(env.target), {"hash": old_hash})
    await run_command(env, "capacity")
    assert env.target.read_bytes() == b"old gallery bytes"
    assert env.db.get_emoji(str(env.target))["hash"] == old_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["capacity", "delete", "blacklist"])
async def test_stale_candidate_revalidates_owner_before_unlink_and_caller_save(env, monkeypatch, operation):
    started, release = asyncio.Event(), asyncio.Event()
    command = None

    class DelayedCommandGate(asyncio.Lock):
        async def __aenter__(self):
            if asyncio.current_task() is command:
                started.set()
                await release.wait()
            return await super().__aenter__()

    env.db._write_lock = DelayedCommandGate()
    prepare_api_delete(env, monkeypatch)
    command = asyncio.create_task(run_command(env, operation))
    try:
        await asyncio.wait_for(started.wait(), 5)
        await replace_with_pending(env)
    finally:
        release.set()
        await asyncio.wait_for(command, 5)
    assert_new_owner(env)
