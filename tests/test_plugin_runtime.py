import asyncio
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
        meta = ps._plugins[0]
        assert meta["source"] == "installed"
        assert meta["loaded"] is True
        assert meta["plugin_id"] == "CarefreeSongs712/extdemo"
        assert meta["file"] == "extdemo/main.py"

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
        await ps.load_plugins()
        instance = ps._plugins[0]["instance"]
        task = instance.task
        assert not task.cancelled()
        await ps.reload_plugins()
        await asyncio.sleep(0)
        assert task.cancelled()

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
        ps.apply_injections = lambda *a, **k: None
        ps.startup_plugins = _noop
        ps.start_tick_tasks = lambda *a, **k: None
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


async def _noop(*args, **kwargs):
    return None


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
        await ps.load_plugins()
        assert ps._plugins[0]["loaded"] is True

    asyncio.run(run())
