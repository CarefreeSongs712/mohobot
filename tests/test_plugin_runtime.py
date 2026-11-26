import asyncio
import json
import sys
from pathlib import Path

import pytest

from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.plugins.manifest import ManifestError, parse_manifest


def _plugin_system(tmp_path: Path, data_name: str = "data") -> PluginSystem:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    return PluginSystem(str(plugins), str(tmp_path / data_name))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")




_MISSING = object()


class _MonkeyPatch:
    """最小 monkeypatch 替身: legacy runner 只支持无参或仅 tmp_path 的用例入口。"""

    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []

    def setattr(self, obj, name: str, value) -> None:  # noqa: A003 - 对齐 pytest 接口
        self._undo.append((obj, name, vars(obj).get(name, _MISSING)))
        setattr(obj, name, value)

    def setitem(self, mapping, key, value) -> None:
        self._undo.append((mapping, key, mapping.get(key, _MISSING)))
        mapping[key] = value

    def undo(self) -> None:
        while self._undo:
            obj, name, old = self._undo.pop()
            if isinstance(obj, dict):
                if old is _MISSING:
                    obj.pop(name, None)
                else:
                    obj[name] = old
            elif old is _MISSING:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, old)
def test_directory_plugin_relative_import_reloads(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        plugin_dir = tmp_path / "plugins" / "pkgplug"
        _write(plugin_dir / "helper.py", "VALUE = 1\n")
        _write(
            plugin_dir / "main.py",
            "from .helper import VALUE\n"
            "class Plugin:\n"
            "    def __init__(self): self.value = VALUE\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        await ps.load_plugins()
        assert ps._plugins[0]["instance"].value == 1

        _write(plugin_dir / "helper.py", "VALUE = 2\n")
        await ps.reload_plugins()
        assert ps._plugins[0]["instance"].value == 2

    asyncio.run(run())


def test_reload_removes_modules_and_sys_path(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        plugin_dir = tmp_path / "plugins" / "gone"
        _write(
            plugin_dir / "main.py",
            "class Plugin:\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        await ps.load_plugins()
        assert "mohobot_plugin_gone" in sys.modules
        assert str(plugin_dir.absolute()) in sys.path

        import shutil

        shutil.rmtree(plugin_dir)
        await ps.reload_plugins()
        assert "mohobot_plugin_gone" not in sys.modules
        assert str(plugin_dir.absolute()) not in sys.path

    asyncio.run(run())


def test_reload_plugin_only_replaces_target(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        for name in ("alpha", "beta"):
            _write(
                tmp_path / "plugins" / f"{name}.py",
                "class Plugin:\n"
                "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
            )
        await ps.load_plugins()
        old_beta = next(m["instance"] for m in ps._plugins if m["name"] == "beta")

        assert await ps.reload_plugin("alpha") is True
        new_beta = next(m["instance"] for m in ps._plugins if m["name"] == "beta")
        assert new_beta is old_beta
        await ps.shutdown_plugins()

    asyncio.run(run())


def test_sync_hook_is_rejected(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "badhook.py",
            "class Plugin:\n"
            "    def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        await ps.load_plugins()
        assert ps._plugins[0]["loaded"] is False
        assert "async" in ps._plugins[0]["error"]

    asyncio.run(run())


def test_manifest_parse_and_validation(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    _write(plugin_dir / "main.py", "class Plugin:\n    pass\n")
    _write(
        plugin_dir / "metadata.yaml",
        "name: demo\n"
        "author: CarefreeSongs712\n"
        "display_name: 演示\n"
        "version: 1.0.0\n"
        "description: demo plugin\n"
        "repo: https://github.com/CarefreeSongs712/mohobot-plugin-demo\n"
        "license: MIT\n"
        "api_version: 1\n",
    )
    manifest = parse_manifest(plugin_dir / "metadata.yaml")
    assert manifest.plugin_id == "CarefreeSongs712/demo"
    assert manifest.entry == "main.py"

    _write(
        plugin_dir / "metadata.yaml",
        "name: Demo-Plugin\n"
        "author: CarefreeSongs712\n"
        "display_name: 演示\n"
        "version: 1.0.0\n"
        "description: demo plugin\n"
        "repo: https://github.com/CarefreeSongs712/mohobot-plugin-demo\n"
        "license: MIT\n"
        "api_version: 1\n",
    )
    with pytest.raises(ManifestError):
        parse_manifest(plugin_dir / "metadata.yaml")


def test_installed_plugin_requires_manifest(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        installed = tmp_path / "data" / "plugins" / "extdemo"
        _write(
            installed / "main.py",
            "class Plugin:\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        await ps.load_plugins()
        assert ps._plugins[0]["source"] == "installed"
        assert ps._plugins[0]["loaded"] is False
        assert "metadata" in ps._plugins[0]["error"]

    asyncio.run(run())


def test_installed_plugin_loads_with_manifest(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        installed = tmp_path / "data" / "plugins" / "extdemo"
        _write(
            installed / "main.py",
            "class Plugin:\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        _write(
            installed / "metadata.yaml",
            "name: extdemo\n"
            "author: CarefreeSongs712\n"
            "display_name: 外置演示\n"
            "version: 1.0.0\n"
            "description: installed plugin\n"
            "repo: https://github.com/CarefreeSongs712/mohobot-plugin-extdemo\n"
            "license: MIT\n"
            "api_version: 1\n",
        )
        await ps.load_plugins()
        try:
            meta = ps._plugins[0]
            assert meta["source"] == "installed"
            assert meta["loaded"] is True
            assert meta["plugin_id"] == "CarefreeSongs712/extdemo"
            assert meta["file"] == "extdemo/main.py"
        finally:
            # 清理已加载插件, 避免模块残留在 sys.modules 影响后续用例顺序。
            await ps.shutdown_plugins()

    asyncio.run(run())


def test_installed_plugin_overrides_bundled_name(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "alpha.py",
            "class Plugin:\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        installed = tmp_path / "data" / "plugins" / "alpha"
        _write(
            installed / "main.py",
            "class Plugin:\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        _write(
            installed / "metadata.yaml",
            "name: alpha\n"
            "author: CarefreeSongs712\n"
            "display_name: 冲突\n"
            "version: 1.0.0\n"
            "description: conflict\n"
            "repo: https://github.com/CarefreeSongs712/mohobot-plugin-alpha\n"
            "license: MIT\n"
            "api_version: 1\n",
        )
        await ps.load_plugins()
        loaded = [m for m in ps._plugins if m.get("loaded")]
        assert [m["source"] for m in loaded] == ["installed"]
        assert len(ps._plugins) == 1
        await ps.shutdown_plugins()

    asyncio.run(run())


def test_staging_directories_are_not_loaded(tmp_path: Path) -> None:
    async def run() -> None:
        staging = tmp_path / "data" / "plugins" / ".plugin-install-deadbeef"
        _write(
            staging / "main.py",
            "class Plugin:\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        ps = _plugin_system(tmp_path)
        await ps.load_plugins()
        assert ps._plugins == []

    asyncio.run(run())


def test_full_reload_cancels_plugin_owned_tasks(tmp_path: Path) -> None:
    async def run() -> None:
        from mohobot.services.task_supervisor import TaskSupervisor

        ps = _plugin_system(tmp_path)
        supervisor = TaskSupervisor()
        ps.set_task_supervisor(supervisor)
        _write(
            tmp_path / "plugins" / "worker.py",
            "import asyncio\n"
            "class Plugin:\n"
            "    _supervisor = None\n"
            "    @classmethod\n"
            "    def inject_task_supervisor(cls, supervisor): cls._supervisor = supervisor\n"
            "    async def on_startup(self):\n"
            "        self.task = self._supervisor.create_task(\n"
            "            asyncio.sleep(60), name='worker-task', owner='plugins')\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        try:
            assert await ps.load_plugins() == 1
            instance = ps._plugins[0]["instance"]
            task = instance.task
            assert not task.cancelled()
            await ps.reload_plugins()
            await asyncio.sleep(0)
            assert task.cancelled()
        finally:
            await ps.shutdown_plugins()
            await supervisor.shutdown()

    asyncio.run(run())


def test_installed_plugins_do_not_shadow_stdlib(tmp_path: Path) -> None:
    async def run() -> None:
        import json

        installed = tmp_path / "data" / "plugins" / "json"
        _write(
            installed / "main.py",
            "class Plugin:\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        _write(
            installed / "metadata.yaml",
            "name: json\n"
            "author: CarefreeSongs712\n"
            "display_name: shadow\n"
            "version: 1.0.0\n"
            "description: shadow test\n"
            "repo: https://github.com/CarefreeSongs712/mohobot-plugin-json\n"
            "license: MIT\n"
            "api_version: 1\n",
        )
        ps = _plugin_system(tmp_path)
        await ps.load_plugins()
        assert str((tmp_path / "data" / "plugins").absolute()) not in sys.path
        assert "site-packages" not in str(json.__file__)
        assert str(tmp_path) not in str(json.__file__)

    asyncio.run(run())


def test_plugin_owned_tasks_with_custom_owner_are_cancelled(tmp_path: Path) -> None:
    async def run() -> None:
        from mohobot.services.task_supervisor import TaskSupervisor

        ps = _plugin_system(tmp_path)
        supervisor = TaskSupervisor()
        ps.set_task_supervisor(supervisor)
        _write(
            tmp_path / "plugins" / "custom_owner.py",
            "import asyncio\n"
            "class Plugin:\n"
            "    _supervisor = None\n"
            "    @classmethod\n"
            "    def inject_task_supervisor(cls, supervisor): cls._supervisor = supervisor\n"
            "    async def on_startup(self):\n"
            "        self.task = self._supervisor.create_task(\n"
            "            asyncio.sleep(60), name='custom-owner-task', owner='events')\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        await ps.load_plugins()
        task = ps._plugins[0]["instance"].task
        await ps.reload_plugins()
        await asyncio.sleep(0)
        assert task.cancelled()
        await ps.shutdown_plugins()

    asyncio.run(run())


def test_single_reload_and_unload_cancel_custom_owned_tasks(tmp_path: Path) -> None:
    async def run() -> None:
        from mohobot.services.task_supervisor import TaskSupervisor

        ps = _plugin_system(tmp_path)
        supervisor = TaskSupervisor()
        ps.set_task_supervisor(supervisor)
        _write(
            tmp_path / "plugins" / "custom_owner.py",
            """import asyncio
class Plugin:
    _supervisor = None
    @classmethod
    def inject_task_supervisor(cls, supervisor): cls._supervisor = supervisor
    async def on_startup(self):
        self.task = self._supervisor.create_task(
            asyncio.sleep(60), name='custom-owner-task', owner='events')
    async def on_message(self, bot_id, event, raw): return (False, None)
""",
        )
        await ps.load_plugins()
        first = ps._plugins[0]["instance"].task
        assert await ps.reload_plugin("custom_owner") is True
        await asyncio.sleep(0)
        assert first.cancelled()
        second = ps._plugins[0]["instance"].task
        assert await ps.unload_plugin("custom_owner") is True
        await asyncio.sleep(0)
        assert second.cancelled()

    asyncio.run(run())


def test_reload_respects_disabled_state(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "alpha.py",
            """class Plugin:
    async def on_message(self, bot_id, event, raw): return (False, None)
""",
        )
        await ps.load_plugins()
        assert ps._plugins[0]["loaded"] is True
        state_path = tmp_path / "data" / "plugins_state.json"
        _write(state_path, '{"alpha": {"enabled": false}}')
        assert await ps.reload_plugin("alpha") is False
        meta = next(m for m in ps._plugins if m["name"] == "alpha")
        assert meta["enabled"] is False
        assert meta["loaded"] is False
        assert meta.get("instance") is None

    asyncio.run(run())


def test_shutdown_created_tasks_are_cancelled(tmp_path: Path) -> None:
    async def run() -> None:
        from mohobot.services.task_supervisor import TaskSupervisor

        ps = _plugin_system(tmp_path)
        supervisor = TaskSupervisor()
        ps.set_task_supervisor(supervisor)
        _write(
            tmp_path / "plugins" / "shutdown_task.py",
            """import asyncio
class Plugin:
    _supervisor = None
    @classmethod
    def inject_task_supervisor(cls, supervisor): cls._supervisor = supervisor
    async def on_shutdown(self):
        self.task = self._supervisor.create_task(
            asyncio.sleep(60), name='shutdown-task', owner='events')
    async def on_message(self, bot_id, event, raw): return (False, None)
""",
        )
        await ps.load_plugins()
        old = ps._plugins[0]["instance"]
        assert await ps.reload_plugin("shutdown_task") is True
        await asyncio.sleep(0)
        assert old.task.cancelled()

    asyncio.run(run())


def test_plugin_dir_appended_does_not_shadow_stdlib(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "shadow_probe" / "metadata.yaml",
            """name: shadow_probe
author: CarefreeSongs712
version: 1.0.0
description: shadow
repo: https://github.com/CarefreeSongs712/shadow
license: MIT
api_version: 1
""",
        )
        _write(
            tmp_path / "plugins" / "shadow_probe" / "main.py",
            """class Plugin:
    async def on_message(self, bot_id, event, raw):
        return (False, None)
""",
        )
        _write(
            tmp_path / "plugins" / "shadow_probe" / "colorsys.py",
            "SENTINEL = True\n",
        )
        await ps.load_plugins()
        import colorsys

        assert "shadow_probe" not in (getattr(colorsys, "__file__", "") or "")

    asyncio.run(run())


def test_root_metadata_does_not_apply_to_single_file_plugin(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "metadata.yaml",
            """name: root_manifest
author: CarefreeSongs712
version: 1.0.0
description: root
repo: https://github.com/CarefreeSongs712/root
license: MIT
api_version: 1
""",
        )
        _write(
            tmp_path / "plugins" / "single_probe.py",
            """class Plugin:
    async def on_message(self, bot_id, event, raw):
        return (False, None)
""",
        )
        await ps.load_plugins()
        assert [meta["name"] for meta in ps.list_plugins()] == ["single_probe"]

    asyncio.run(run())


def test_reload_waits_for_inflight_dispatch(tmp_path: Path) -> None:
    async def run() -> None:
        from types import SimpleNamespace

        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "slow_probe.py",
            """import asyncio
class Plugin:
    started = asyncio.Event()
    release = asyncio.Event()
    async def on_message(self, bot_id, event, raw):
        self.started.set()
        await self.release.wait()
        return (True, "ok")
    async def on_shutdown(self):
        pass
""",
        )
        await ps.load_plugins()
        plugin = ps._plugins[0]["instance"]
        event = SimpleNamespace(message=[{"type": "text", "data": {"text": "/slow"}}])
        handler = asyncio.create_task(ps.intercept("bot_001", event, {}))
        await plugin.started.wait()
        reload = asyncio.create_task(ps.reload_plugin("slow_probe"))
        await asyncio.sleep(0.05)
        assert not reload.done()
        plugin.release.set()
        await handler
        assert await reload is True

    asyncio.run(run())


def test_no_prefix_handler_is_tracked_by_dispatch_barrier(tmp_path: Path) -> None:
    async def run() -> None:
        from types import SimpleNamespace

        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "prefix_probe.py",
            """import asyncio
class Plugin:
    no_prefix_triggers = {"go"}
    async def on_message(self, bot_id, event, raw):
        await asyncio.sleep(0.2)
        return (True, "ok")
""",
        )
        await ps.load_plugins()
        event = SimpleNamespace(message=[{"type": "text", "data": {"text": "go"}}])
        task = asyncio.create_task(ps.dispatch_observed("bot_001", event, {}))
        await asyncio.sleep(0.05)
        assert ps._active_dispatches == 1
        handled, reply = await task
        assert handled is True and reply == "ok"

    asyncio.run(run())


def test_module_name_collision_is_reported(tmp_path: Path) -> None:
    async def run() -> None:
        body = (
            "class Plugin:\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n"
        )
        ps = _plugin_system(tmp_path)
        _write(tmp_path / "plugins" / "foo-bar.py", body)
        _write(tmp_path / "plugins" / "foo_bar.py", body)
        await ps.load_plugins()
        errors = [m["error"] for m in ps._plugins if m.get("error")]
        assert any("模块名冲突" in e for e in errors)
        assert sum(1 for m in ps._plugins if m.get("loaded")) == 1
        assert await ps.reload_plugin("foo_bar") is False
        loaded = [m["name"] for m in ps._plugins if m.get("loaded")]
        assert loaded == ["foo-bar"]

    asyncio.run(run())


def test_stale_staging_directories_are_cleaned(tmp_path: Path) -> None:
    async def run() -> None:
        import os
        import time

        ps = _plugin_system(tmp_path)
        stale = tmp_path / "plugins" / ".plugin-install-stale"
        stale.mkdir()
        old = time.time() - 7200
        os.utime(stale, (old, old))
        fresh = tmp_path / "plugins" / ".plugin-install-fresh"
        fresh.mkdir()
        await ps.load_plugins()
        assert not stale.exists()
        assert fresh.exists()

    asyncio.run(run())


def test_reload_waits_for_inflight_handler(tmp_path: Path) -> None:
    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "blocking.py",
            "import asyncio\n"
            "class Plugin:\n"
            "    async def on_message(self, bot_id, event, raw):\n"
            "        self.started.set()\n"
            "        await self.release.wait()\n"
            "        return (True, 'done')\n",
        )
        await ps.load_plugins()
        instance = ps._plugins[0]["instance"]
        instance.started = started
        instance.release = release
        call = asyncio.create_task(ps.intercept("bot_001", object(), {}))
        await started.wait()
        reload = asyncio.create_task(ps.reload_plugins())
        await asyncio.sleep(0.05)
        assert not reload.done()
        release.set()
        await call
        await reload

    asyncio.run(run())


def test_lifecycle_hook_timeout_is_bounded(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        ps._hook_timeout = 0.01
        _write(
            tmp_path / "plugins" / "slow.py",
            "import asyncio\n"
            "class Plugin:\n"
            "    async def on_startup(self): await asyncio.sleep(60)\n"
            "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        )
        assert await ps.load_plugins() == 0
        assert ps._plugins[0]["loaded"] is False
        assert "on_startup" in ps._plugins[0]["error"]

    asyncio.run(run())


def test_cold_startup_injects_runtime_before_startup_hook(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        ps.set_admin_ids([7])
        ps.set_runtime_refs(ws_server=object(), message_handler=object())
        _write(
            tmp_path / "plugins" / "cold_refs.py",
            "class Plugin:\n"
            "    ws = None\n"
            "    handler = None\n"
            "    admins = []\n"
            "    @classmethod\n"
            "    def inject_ws_server(cls, value): cls.ws = value\n"
            "    @classmethod\n"
            "    def inject_message_handler(cls, value): cls.handler = value\n"
            "    @classmethod\n"
            "    def inject_admin_ids(cls, value): cls.admins = list(value)\n"
            "    async def on_startup(self):\n"
            "        if self.ws is None or self.handler is None or self.admins != ['7']:\n"
            "            raise RuntimeError('host refs missing at startup')\n"
            "        self.started = True\n"
            "    async def on_message(self, bot_id, event, raw): return (True, 'ok')\n",
        )
        assert await ps.load_plugins() == 1
        meta = ps._plugins[0]
        assert meta["error"] is None and ps._can_dispatch(meta)
        assert getattr(meta["instance"], "started", False) is True
        assert await ps.intercept("bot_001", object(), {}) == (True, "ok")
        await ps.shutdown_plugins()

    asyncio.run(run())


def _probe_interrupted_shutdown(tmp_path: Path, finish_shutdown: bool) -> None:
    from mohobot.services.task_supervisor import TaskSupervisor

    async def run() -> None:
        ps = _plugin_system(tmp_path)
        ps.set_task_supervisor(TaskSupervisor())
        _write(
            tmp_path / "plugins" / "retry_shutdown.py",
            "class Plugin:\n"
            "    @classmethod\n"
            "    def inject_task_supervisor(cls, supervisor): cls.supervisor = supervisor\n"
            "    async def on_message(self, bot_id, event, raw): return (True, 'old')\n",
        )
        assert await ps.load_plugins() == 1
        meta = ps._plugins[0]
        instance = meta["instance"]
        entered = asyncio.Event()
        child_started = asyncio.Event()
        child_closing = asyncio.Event()
        release_child = asyncio.Event()
        release_hook = asyncio.Event()
        hook_cancelled = asyncio.Event()
        calls = 0

        async def child():
            child_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                child_closing.set()
                await release_child.wait()

        async def shutdown():
            nonlocal calls, child_task
            calls += 1
            if calls > 1:
                assert child_task.done()
                return
            child_task = instance.supervisor.create_task(child(), name="hook-child", owner="child")
            await child_started.wait()
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                hook_cancelled.set()
                await release_hook.wait()
                if not finish_shutdown:
                    raise

        child_task = None
        instance.on_shutdown = shutdown
        remove = asyncio.create_task(ps.unload_plugin("retry_shutdown"))
        retry = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            remove.cancel()
            with pytest.raises(asyncio.CancelledError):
                await remove
            await asyncio.wait_for(hook_cancelled.wait(), timeout=5)
            hook = meta["_hook_task"]
            assert not hook.done()
            assert meta["instance"] is instance and not ps._can_dispatch(meta)
            assert await ps.intercept("bot_001", object(), {}) == (False, None)

            release_hook.set()
            assert (await asyncio.wait({hook}, timeout=5))[0] == {hook}
            assert hook.cancelled() is not finish_shutdown
            retry = asyncio.create_task(ps.unload_plugin("retry_shutdown"))
            await asyncio.wait_for(child_closing.wait(), timeout=5)
            assert not retry.done()
            assert meta["instance"] is instance and calls == 1
            release_child.set()
            assert await asyncio.wait_for(retry, timeout=5)
            assert calls == (1 if finish_shutdown else 2)
            assert meta not in ps._plugins
        finally:
            release_hook.set()
            release_child.set()
            remove.cancel()
            await asyncio.gather(remove, *([retry] if retry else []), return_exceptions=True)
            await ps.shutdown_plugins()

    asyncio.run(run())


def _probe_toggle_publication(tmp_path: Path, monkeypatch, enabled: bool) -> None:
    from mohobot import file_store

    async def run() -> None:
        ps = _plugin_system(tmp_path)
        _write(tmp_path / "plugins" / "toggle_retry.py", "class Plugin: pass\n")
        initial = {"toggle_retry": {"enabled": not enabled, "other": "preserved"}}
        _write(ps._state_file, json.dumps(initial))
        assert await ps.load_plugins() == int(not enabled)
        writing = asyncio.Event()
        release = asyncio.Event()
        original_update = file_store.json_update

        async def gated_update(*args, **kwargs):
            writing.set()
            await release.wait()
            return await original_update(*args, **kwargs)

        monkeypatch.setattr(file_store, "json_update", gated_update)
        toggle = asyncio.create_task(ps.set_enabled("toggle_retry", enabled))
        try:
            await asyncio.wait_for(writing.wait(), timeout=5)
            toggle.cancel()
            await asyncio.sleep(0)
            assert not toggle.done() and ps._lock().locked()
            assert ps.list_plugins()[0]["enabled"] is not enabled
            assert not ps.list_plugins()[0]["loaded"]
            assert ps._load_state() == initial
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(toggle, timeout=5)
            assert ps.list_plugins()[0]["enabled"] is enabled
            assert ps.list_plugins()[0]["loaded"] is enabled
            assert ps._load_state() == {"toggle_retry": {"enabled": enabled, "other": "preserved"}}
        finally:
            release.set()
            await asyncio.gather(toggle, return_exceptions=True)
            await ps.shutdown_plugins()

    asyncio.run(run())


def _probe_enable_write_failure(tmp_path: Path, monkeypatch) -> None:
    from mohobot import file_store

    async def run() -> None:
        ps = _plugin_system(tmp_path)
        name = "enable_write_retry"
        _write(tmp_path / "plugins" / f"{name}.py", "class Plugin: pass\n")
        initial = {name: {"enabled": False, "other": "preserved"}}
        _write(ps._state_file, json.dumps(initial))
        started = []
        stopped = []
        load_one = ps._load_one
        update = file_store.json_update

        def load_candidate(*args, **kwargs):
            meta = load_one(*args, **kwargs)
            if meta.get("loaded"):
                instance = meta["instance"]
                instance.on_startup = lambda: started.append(instance)
                instance.on_shutdown = lambda: stopped.append(instance)
            return meta

        async def fail_update(*args, **kwargs):
            assert len(started) == 1
            assert not ps.list_plugins()[0]["loaded"]
            raise OSError("fixture: state write failed")

        monkeypatch.setattr(ps, "_load_one", load_candidate)
        monkeypatch.setattr(file_store, "json_update", fail_update)
        assert await ps.load_plugins() == 0
        try:
            assert not await ps.set_enabled(name, True)
            assert started == stopped and len(stopped) == 1
            assert type(stopped[0]).__module__ not in sys.modules
            row = ps.list_plugins()[0]
            assert not row["enabled"] and not row["loaded"]
            assert "state write failed" in row["error"]
            assert ps._load_state() == initial

            monkeypatch.setattr(file_store, "json_update", update)
            assert await ps.set_enabled(name, True)
            assert ps.list_plugins()[0]["loaded"]
            assert started[1] is not stopped[0]
            assert ps._load_state() == {name: {**initial[name], "enabled": True}}
        finally:
            await ps.shutdown_plugins()

    asyncio.run(run())


def _probe_observed_fallthrough(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    async def run() -> None:
        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "observed_retire.py",
            "class Plugin:\n"
            "    no_prefix_triggers = ['probe']\n"
            "    async def on_message(self, *_args): return True, 'target'\n",
        )
        _write(
            tmp_path / "plugins" / "observed_retire_neighbor.py",
            "class Plugin:\n"
            "    no_prefix_triggers = ['probe']\n"
            "    async def on_message(self, *_args): return True, 'neighbor'\n",
        )
        assert await ps.load_plugins() == 2
        target = ps._plugins[0]["instance"]
        entered = asyncio.Event()
        pausing = asyncio.Event()
        release = asyncio.Event()
        pause_dispatch = ps._pause_dispatches
        old_calls = []

        async def observe(*_args):
            entered.set()
            await release.wait()
            return False, None

        async def message(*_args):
            old_calls.append("old")
            return True, "old"

        async def pause(targets):
            pausing.set()
            await pause_dispatch(targets)

        target.on_message_observed = observe
        target.on_message = message
        monkeypatch.setattr(ps, "_pause_dispatches", pause)
        event = SimpleNamespace(message=[{"type": "text", "data": {"text": "probe"}}])
        dispatch = asyncio.create_task(ps.dispatch_observed("bot", event, {}))
        reload = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            reload = asyncio.create_task(ps.reload_plugin("observed_retire"))
            await asyncio.wait_for(pausing.wait(), timeout=5)
            assert not reload.done() and not ps.list_plugins()[0]["loaded"]
            release.set()
            assert await dispatch == (True, "neighbor")
            assert await reload and not old_calls
            assert await ps.dispatch_observed("bot", event, {}) == (True, "target")
        finally:
            release.set()
            await asyncio.gather(dispatch, *([reload] if reload is not None else []), return_exceptions=True)
            await ps.shutdown_plugins()

    asyncio.run(run())


def test_tick_stop_exits_loop_after_callback_suppresses_cancel(tmp_path: Path) -> None:
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        _write(
            tmp_path / "plugins" / "tick_stop.py",
            "class Plugin:\n"
            "    async def on_tick(self): await self.tick()\n",
        )
        assert await ps.load_plugins() == 1
        instance = ps._plugins[0]["instance"]
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def tick():
            nonlocal calls
            calls += 1
            # Detect entering another iteration without waiting for another tick.
            instance.interval_sec = 3600
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        instance.tick = tick
        instance.interval_sec = 1
        ps.start_tick_tasks()
        task = ps._tick_tasks_by_name["tick_stop"]
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            ps.stop_tick_tasks()
            await asyncio.wait_for(cancelled.wait(), timeout=5)
            ps._drain_timeout = 0
            with pytest.raises(TimeoutError):
                await ps.stop_tick_tasks_async()
            assert ps._tick_tasks_by_name["tick_stop"] is task and not task.done()
            assert task.cancelling() == 1
            release.set()
            assert (await asyncio.wait({task}, timeout=1))[0] == {task}
            assert calls == 1 and not task.cancelled()
            assert "tick_stop" not in ps._tick_tasks_by_name
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await ps.shutdown_plugins()

    asyncio.run(run())

def test_interrupted_shutdown_keeps_hook_and_children_owned(tmp_path: Path) -> None:
    for finish_shutdown in (False, True):
        case_dir = tmp_path / ("finished" if finish_shutdown else "cancelled")
        case_dir.mkdir()
        _probe_interrupted_shutdown(case_dir, finish_shutdown)


def test_plugin_toggle_finishes_publication_when_writer_waiter_is_cancelled(tmp_path: Path) -> None:
    for enabled in (False, True):
        case_dir = tmp_path / f"toggle-{int(enabled)}"
        case_dir.mkdir()
        patcher = _MonkeyPatch()
        try:
            _probe_toggle_publication(case_dir, patcher, enabled)
        finally:
            patcher.undo()


def test_enable_write_failure_retires_unpublished_candidate(tmp_path: Path) -> None:
    patcher = _MonkeyPatch()
    try:
        _probe_enable_write_failure(tmp_path, patcher)
    finally:
        patcher.undo()


def test_observed_fallthrough_skips_instance_entering_retirement(tmp_path: Path) -> None:
    patcher = _MonkeyPatch()
    try:
        _probe_observed_fallthrough(tmp_path, patcher)
    finally:
        patcher.undo()


def test_failed_startup_candidate_can_be_reloaded_after_fix(tmp_path: Path) -> None:
    """冷启动 on_startup 失败后修复文件, 面板重载/全部重载/启用都能恢复。"""
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        plugin_dir = tmp_path / "plugins" / "retryme"
        _write(
            plugin_dir / "main.py",
            "class Plugin:\n"
            "    async def on_startup(self):\n"
            "        raise RuntimeError('first startup fails')\n",
        )
        assert await ps.load_plugins() == 0
        meta = ps._plugins[0]
        assert meta["loaded"] is False
        assert meta["_imports"] is not None
        assert "mohobot_plugin_retryme" in sys.modules
        _write(
            plugin_dir / "main.py",
            "class Plugin:\n"
            "    async def on_startup(self):\n"
            "        return None\n",
        )
        assert await ps.reload_plugin("retryme") is True
        assert ps.list_plugins()[0]["loaded"] is True
        assert await ps.reload_plugins() == 1
        assert await ps.set_enabled("retryme", True) is True
        await ps.shutdown_plugins()

    asyncio.run(run())


def test_one_shutdown_timeout_does_not_skip_other_plugins(tmp_path: Path) -> None:
    """一个插件 on_shutdown 挂起不得中断其余插件的关闭 (审查 P3)。"""
    async def run() -> None:
        ps = _plugin_system(tmp_path)
        ps._hook_timeout = 0.1
        _write(
            tmp_path / "plugins" / "hangplug" / "main.py",
            "import asyncio\n"
            "class Plugin:\n"
            "    calls = []\n"
            "    async def on_shutdown(self):\n"
            "        type(self).calls.append('hang')\n"
            "        await asyncio.Event().wait()\n",
        )
        _write(
            tmp_path / "plugins" / "goodplug" / "main.py",
            "class Plugin:\n"
            "    calls = []\n"
            "    async def on_shutdown(self):\n"
            "        type(self).calls.append('good')\n",
        )
        assert await ps.load_plugins() == 2
        classes = {meta["name"]: meta["instance"].__class__ for meta in ps._plugins}
        try:
            await asyncio.wait_for(ps.shutdown_plugins(), timeout=5)
            # 先关闭的是 hangplug(列表逆序), 超时后 goodplug 仍必须收到 on_shutdown。
            assert classes["goodplug"].calls == ["good"]
            assert classes["hangplug"].calls == ["hang"]
            hang = next(meta for meta in ps._plugins if meta["name"] == "hangplug")
            assert hang.get("_retiring") is True
        finally:
            for meta in ps._plugins:
                hook = meta.get("_hook_task")
                if hook is not None and not hook.done():
                    hook.cancel()
                    await asyncio.gather(hook, return_exceptions=True)

    asyncio.run(run())
