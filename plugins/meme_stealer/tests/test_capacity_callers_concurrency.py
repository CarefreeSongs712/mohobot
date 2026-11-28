"""Capacity callers must not write an obsolete snapshot after gated deletion."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from meme_stealer_core.core.commands.command_handler import CommandHandler
from meme_stealer_core.core.config.config import PluginConfig
from meme_stealer_core.core.db.database_service import DatabaseService
from meme_stealer_core.core.db.index_manager import IndexManager
from meme_stealer_core.core.events.event_handler import EventHandler
from meme_stealer_core.core.maintenance import service
from meme_stealer_core.core.maintenance.service import MaintenanceService


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["command", "maintenance"])
async def test_capacity_caller_preserves_replaced_non_candidate(tmp_path, monkeypatch, caller):
    config = PluginConfig({"max_reg_num": 1}, tmp_path / "data")
    db = DatabaseService(tmp_path / "emoji.db")
    plugin = SimpleNamespace(plugin_config=config, db_service=db, meme_selector=None)
    plugin.index_manager = IndexManager(plugin)
    handler = EventHandler(plugin)
    target = config.ensure_category_dir("happy") / "evict.png"
    keeper = target.with_name("keeper.png")
    target.write_bytes(b"evicted owner")
    keeper.write_bytes(b"old keeper")
    await db.insert_batch([
        {"path": str(target), "hash": hashlib.sha256(target.read_bytes()).hexdigest(),
         "created_at": 1, "category": "happy"},
        {"path": str(keeper), "hash": hashlib.sha256(keeper.read_bytes()).hexdigest(),
         "created_at": 2, "category": "happy", "use_count": 100,
         "desc": "old keeper", "tags": ["old"], "scope_mode": "public"},
    ])
    new_hash = hashlib.sha256(b"new keeper").hexdigest()

    async def enforce_and_replace(snapshot):
        removed = await handler._enforce_capacity(snapshot)
        assert removed == [str(target)]
        await db.delete_paths([str(keeper)])
        keeper.write_bytes(b"new keeper")
        await db.insert_batch([
            {"path": str(keeper), "hash": new_hash, "category": "happy",
             "desc": "new keeper", "tags": ["new"], "scope_mode": "local",
             "origin_target": "group:200", "allowed_bots": "bot_b"},
        ])
        await db.update_path(str(keeper), {"allowed_bots": "bot_b"})
        return removed

    plugin.event_handler = SimpleNamespace(_enforce_capacity=enforce_and_replace)
    if caller == "command":
        event = SimpleNamespace(plain_result=lambda text: text)
        messages = [message async for message in CommandHandler(plugin).enforce_capacity(event)]
        assert "1/1" in messages[-1]
    else:
        sleeps = 0

        async def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError()

        monkeypatch.setattr(service, "asyncio", SimpleNamespace(
            sleep=sleep, CancelledError=asyncio.CancelledError,
        ))
        await MaintenanceService(plugin)._capacity_control_loop()

    assert keeper.read_bytes() == b"new keeper"
    current = db.get_emoji(str(keeper))
    assert current is not None
    assert current["hash"] == new_hash
    assert current["desc"] == "new keeper"
    assert current["tags"] == ["new"]
    assert current["scope_mode"] == "local"
    assert current["origin_target"] == "group:200"
    assert current["allowed_bots"] == "bot_b"
    assert db.get_emoji(str(target)) is None
