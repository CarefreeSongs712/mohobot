"""Plugin config transactions against temporary data with deterministic gates."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy

import httpx
import pytest

from mohobot import file_store
from mohobot.interceptors.plugin_system import PluginSystem, PluginSystemRetired
from mohobot.web_panel.app import WebPanel


PLUGIN = "config_transaction_fixture"
SCHEMA = {
    "group": {"type": "string", "default": "100"},
    "request": {"type": "object", "items": {
        "blacklist": {"type": "list"},
        "approvers": {"type": "list"},
        "auto": {"type": "bool", "default": True},
    }},
    "limit": {"type": "int", "default": 1},
}
_MISSING = object()


def make_system(tmp_path, raw=_MISSING):
    package = tmp_path / "builtin" / PLUGIN
    package.mkdir(parents=True)
    (package / "_conf_schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    (package / "main.py").write_text(
        "class Plugin:\n"
        "    def on_startup(self):\n"
        "        self.startup_ref = self.plugin_config\n"
        "        self.startup_limit = self.plugin_config['limit']\n"
        "        self.updates = []\n"
        "    def on_config_update(self, config):\n"
        "        self.updates.append((config, config['limit']))\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "custom-runtime"
    archive = runtime / "plugins_config" / f"{PLUGIN}.json"
    if raw is not _MISSING:
        archive.parent.mkdir(parents=True)
        archive.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    return PluginSystem(plugins_dir=str(package.parent), data_dir=str(runtime)), archive


def instance(system):
    return system._plugins[0]["instance"]


@asynccontextmanager
async def running(system):
    try:
        assert await system.load_plugins() == 1
        yield
    finally:
        await system.shutdown_plugins()


@pytest.mark.asyncio
async def test_explicit_leaves_merge_latest_raw_file_and_keep_injected_dict(tmp_path):
    system, archive = make_system(tmp_path, {"request": {"blacklist": "100"}})
    async with running(system):
        inst = instance(system)
        latest = {"request": {"blacklist": "222,333", "approvers": ["444"], "private": {"x": 1}}, "private": 5}
        await file_store.json_update(archive, lambda _: latest)
        assert await system.save_plugin_config(PLUGIN, {"group": 99, "request": {"auto": False, "private": 7}, "limit": 0})
        assert await file_store.json_read(archive) == {
            **latest, "group": "99", "limit": 0, "request": {**latest["request"], "auto": False},
        }
        assert inst.plugin_config is inst.startup_ref is inst.updates[0][0]
        assert inst.plugin_config == {
            "group": "99", "limit": 0, "request": {"blacklist": ["222", "333"], "approvers": ["444"], "auto": False},
        }
        assert await system.save_plugin_config(PLUGIN, {"group": "", "request": {"blacklist": []}})
        assert (await file_store.json_read(archive))["request"] == {**latest["request"], "blacklist": [], "auto": False}
        assert inst.plugin_config["group"] == ""
        assert inst.plugin_config["request"]["blacklist"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["{}\n", "", "[1]", "{broken json"])
async def test_existing_archive_not_repaired_by_startup_or_read(tmp_path, raw):
    system, archive = make_system(tmp_path, raw)
    async with running(system):
        config = system.get_plugin_config(PLUGIN)
        assert config == instance(system).plugin_config
        assert archive.read_text(encoding="utf-8") == raw
        config["limit"] = 1000
        assert instance(system).plugin_config["limit"] == 1


@pytest.mark.asyncio
async def test_missing_archive_is_seeded_with_effective_defaults(tmp_path):
    system, archive = make_system(tmp_path)
    async with running(system):
        assert await file_store.json_read(archive) == instance(system).plugin_config


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True], ids=["save-first", "repeated-cancel"])
async def test_entered_commit_holds_lifecycle_until_write_and_publication(tmp_path, monkeypatch, cancel_waiter):
    system, archive = make_system(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = file_store.json_update

    async def hold(path, *args, **kwargs):
        assert path == archive
        entered.set()
        await release.wait()
        return await original(path, *args, **kwargs)

    async with running(system):
        old = instance(system)
        monkeypatch.setattr(file_store, "json_update", hold)
        save = asyncio.create_task(system.save_plugin_config(PLUGIN, {"limit": 9}))
        reload = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if cancel_waiter:
                save.cancel()
                await asyncio.sleep(0)  # yield to the cancelled waiter, not a timing threshold
                save.cancel()
                await asyncio.sleep(0)
            attempted = asyncio.Event()

            async def reload_target():
                attempted.set()
                return await system.reload_plugin(PLUGIN)

            reload = asyncio.create_task(reload_target())
            await asyncio.wait_for(attempted.wait(), 3)
            assert system._lock().locked()
            assert not save.done()
            assert not reload.done()
            assert old.updates == []
            release.set()
            if cancel_waiter:
                with pytest.raises(asyncio.CancelledError):
                    await save
            else:
                assert await save
            assert await reload
            assert old.updates == [(old.startup_ref, 9)]
            assert instance(system).startup_limit == 9
            assert instance(system).updates == []
        finally:
            release.set()
            await asyncio.gather(*(t for t in (save, reload) if t is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_empty_schema_patch_neither_writes_nor_publishes(tmp_path, monkeypatch):
    system, archive = make_system(tmp_path)
    async with running(system):
        before = archive.read_bytes()
        previous = deepcopy(instance(system).plugin_config)
        writes = []
        original = file_store.json_update

        async def count(path, *args, **kwargs):
            writes.append(path)
            return await original(path, *args, **kwargs)

        monkeypatch.setattr(file_store, "json_update", count)
        assert await system.save_plugin_config(PLUGIN, {"unknown": 9, "request": {"unknown": 1}})
        assert not writes
        assert not instance(system).updates
        assert instance(system).plugin_config == previous
        assert archive.read_bytes() == before


@pytest.mark.asyncio
async def test_reload_first_and_cancelled_queued_save(tmp_path, monkeypatch):
    system, archive = make_system(tmp_path)
    gate, started, save_attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async with running(system):
        old = instance(system)
        hook = system._call_hook

        async def pause_startup(meta, name, registration_stage=None):
            if name == "on_startup":
                started.set()
                await gate.wait()
            return await hook(meta, name, registration_stage)

        async def queued_save():
            save_attempted.set()
            return await system.save_plugin_config(PLUGIN, {"limit": 17})

        monkeypatch.setattr(system, "_call_hook", pause_startup)
        reload = asyncio.create_task(system.reload_plugin(PLUGIN))
        cancelled_save = save = None
        try:
            await asyncio.wait_for(started.wait(), 3)
            cancelled_save = asyncio.create_task(queued_save())
            await asyncio.wait_for(save_attempted.wait(), 3)
            cancelled_save.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_save
            assert await file_store.json_read(archive) == old.plugin_config
            save_attempted.clear()
            save = asyncio.create_task(queued_save())
            await asyncio.wait_for(save_attempted.wait(), 3)
            assert not save.done()
            gate.set()
            assert await reload
            assert await save
            current = instance(system)
            assert current is not old
            assert old.updates == []
            assert current.startup_limit == 1
            assert current.updates == [(current.startup_ref, 17)]
            assert (await file_store.json_read(archive))["limit"] == 17
        finally:
            gate.set()
            await asyncio.gather(*(t for t in (reload, cancelled_save, save) if t is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_close_admission_during_write_does_not_apply_or_accept_next_write(tmp_path, monkeypatch):
    system, archive = make_system(tmp_path)
    gate, entered = asyncio.Event(), asyncio.Event()
    original = file_store.json_update

    async def hold(path, *args, **kwargs):
        entered.set()
        await gate.wait()
        return await original(path, *args, **kwargs)

    async with running(system):
        inst = instance(system)
        monkeypatch.setattr(file_store, "json_update", hold)
        save = asyncio.create_task(system.commit_plugin_config(PLUGIN, {"limit": 45}))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            system.close_admission()
            gate.set()
            result = await save
            assert result["application"] == "not_running"
            assert result["config"]["limit"] == 45
            assert inst.plugin_config["limit"] == 1
            assert inst.updates == []
            with pytest.raises(PluginSystemRetired):
                await system.commit_plugin_config(PLUGIN, {"limit": 50})
            assert (await file_store.json_read(archive))["limit"] == 45
        finally:
            gate.set()
            await asyncio.gather(save, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "[1]", "{broken json"])
async def test_explicit_save_can_replace_invalid_archive_with_only_patch(tmp_path, raw):
    system, archive = make_system(tmp_path, raw)
    async with running(system):
        assert await system.save_plugin_config(PLUGIN, {"limit": 9})
        assert await file_store.json_read(archive) == {"limit": 9}
        assert instance(system).plugin_config["group"] == "100"


@pytest.mark.asyncio
async def test_authenticated_panel_config_responses(tmp_path, monkeypatch):
    system, archive = make_system(tmp_path, {"limit": "2", "request": {"blacklist": "111,222"}})
    panel = WebPanel(data_dir=str(system.data_dir), password_hash=WebPanel._hash_password("fixture"), plugin_system=system)
    api = f"/api/plugins/{PLUGIN}/config"
    async with running(system), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel._app), base_url="http://offline.test",
    ) as client:
        login = await client.post("/api/login", json={"username": "admin", "password": "fixture"})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"

        async def post(data):
            return await client.post(api, json={"data": data})

        inst = instance(system)
        get = await client.get(api)
        assert get.status_code == 200
        assert get.json() == {"schema": SCHEMA, "config": inst.plugin_config}
        assert (await client.post("/api/plugins/missing/config", json={"data": {"limit": 5}})).status_code == 404
        unchanged = await post({"unknown": 7})
        assert unchanged.json() == {"status": "ok", "config": inst.plugin_config, "application": "unchanged"}
        assert inst.updates == []
        before = archive.read_bytes()

        async def fail(*args, **kwargs):
            raise OSError("private-write-detail")

        with monkeypatch.context() as patch:
            patch.setattr(file_store, "json_update", fail)
            write_failed = await post({"limit": 9})
        assert write_failed.status_code == 500 and "private-write-detail" not in write_failed.text
        assert archive.read_bytes() == before
        assert inst.plugin_config["limit"] == 2 and not inst.updates

        applied = await post({"limit": 9})
        assert applied.json() == {"status": "ok", "config": inst.plugin_config, "application": "applied"}
        assert inst.plugin_config["limit"] == 9 and len(inst.updates) == 1

        def bad_callback(config):
            assert not file_store._get_lock(str(archive.absolute())).locked()
            config["limit"] = 91
            raise ValueError("private-hook-detail")

        system._plugins[0]["error"] = "existing lifecycle error"
        monkeypatch.setattr(inst, "on_config_update", bad_callback)
        apply_failed = await post({"limit": 10})
        assert apply_failed.status_code == 200 and "private-hook-detail" not in apply_failed.text
        assert apply_failed.json()["application"] == "apply_failed"
        assert apply_failed.json()["config"]["limit"] == 10 and inst.plugin_config["limit"] == 91
        assert (await file_store.json_read(archive))["limit"] == 10
        assert system._plugins[0]["error"] == "existing lifecycle error"

        assert await system.set_enabled(PLUGIN, False)
        assert system.list_plugins()[0]["enabled"] is False
        not_running = await post({"limit": 11})
        assert not_running.status_code == 200 and not_running.json()["application"] == "not_running"
        assert inst.plugin_config["limit"] == 91
        assert await system.set_enabled(PLUGIN, True)
        assert instance(system).startup_limit == 11 and instance(system).updates == []
        system.close_admission()
        assert (await post({"limit": 12})).status_code == 409
        assert (await file_store.json_read(archive))["limit"] == 11
