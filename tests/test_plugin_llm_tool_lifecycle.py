import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.services.llm_tools import LLMTool, registry, tool_schema


def _write_plugin(plugins_dir: Path, name: str, source: str) -> Path:
    plugins_dir.mkdir(parents=True, exist_ok=True)
    path = plugins_dir / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path


def _tool_plugin_source(name: str, result: str) -> str:
    return f'''from mohobot.api import LLMTool, registry, tool_schema

def handle():
    return {result!r}

if not registry.contains({name!r}):
    registry.register(LLMTool(tool_schema({name!r}, "probe", {{}}, []), handle))

class Plugin:
    pass
'''


def _schema_names() -> list[str]:
    return [schema["function"]["name"] for schema in registry.schemas()]


def test_preloaded_tools_are_removed_after_import_failure_and_replaced_on_reload(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        for index, failure in enumerate(("raise RuntimeError('load failed')\n", "invalid Python !\n")):
            plugin_name = f"preloaded_import_failure_{index}"
            tool_name = f"preloaded_import_failure_tool_{index}"
            plugins_dir = tmp_path / str(index) / "plugins"
            entry = _write_plugin(
                plugins_dir, plugin_name, _tool_plugin_source(tool_name, "preloaded"),
            )
            module_name = f"plugins.{plugin_name}"
            spec = importlib.util.spec_from_file_location(module_name, entry)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            system = PluginSystem(str(plugins_dir), str(tmp_path / str(index) / "data"))
            try:
                spec.loader.exec_module(module)
                assert await registry.execute(tool_name, {}) == "preloaded"
                entry.write_text(
                    _tool_plugin_source(tool_name, "failed candidate") + failure,
                    encoding="utf-8",
                )
                await system.load_plugins()
                assert system.list_plugins()[0]["loaded"] is False
                assert tool_name not in _schema_names()
                assert "error" in json.loads(await registry.execute(tool_name, {}))

                entry.write_text(_tool_plugin_source(tool_name, "reloaded handler"), encoding="utf-8")
                assert await system.reload_plugin(plugin_name) is True
                assert _schema_names().count(tool_name) == 1
                assert registry._owners[tool_name] == plugin_name
                assert await registry.execute(tool_name, {}) == "reloaded handler"
            finally:
                await system.shutdown_plugins()
                registry.unregister(tool_name)
                sys.modules.pop(module_name, None)

    asyncio.run(run())


def test_rejected_plugin_import_discards_registered_tools(tmp_path: Path) -> None:
    async def run() -> None:
        tool_name = "rejected_plugin_probe"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            "rejected_plugin_probe",
            "from mohobot.api import LLMTool, registry, tool_schema\n"
            f"registry.register(LLMTool(tool_schema({tool_name!r}, 'probe', {{}}, []), lambda: 'stale'))\n"
            "class Plugin:\n"
            "    def on_message(self, bot_id, event, raw): return False, None\n",
        )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "custom-data"))
        try:
            await system.load_plugins()
            assert tool_name not in _schema_names()
            result = json.loads(await registry.execute(tool_name, {}))
            assert "error" in result
        finally:
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_plugin_state_uses_custom_data_dir_for_schema_and_execution(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "custom_state_probe"
        tool_name = "custom_state_probe_tool"
        data_dir = tmp_path / "custom-data"
        data_dir.mkdir()
        state_path = data_dir / "plugins_state.json"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(plugins_dir, plugin_name, _tool_plugin_source(tool_name, "active"))
        system = PluginSystem(str(plugins_dir), str(data_dir))
        try:
            await system.load_plugins()
            assert tool_name in _schema_names()
            assert await registry.execute(tool_name, {}) == "active"

            state_path.write_text(
                json.dumps({plugin_name: {"enabled": False}}), encoding="utf-8"
            )
            assert tool_name not in _schema_names()
            assert "error" in json.loads(await registry.execute(tool_name, {}))

            state_path.write_text(
                json.dumps({plugin_name: {"enabled": True}}), encoding="utf-8"
            )
            assert tool_name in _schema_names()
            assert await registry.execute(tool_name, {}) == "active"

            assert await system.set_enabled(plugin_name, False) is True
            assert tool_name not in _schema_names()
            assert "error" in json.loads(await registry.execute(tool_name, {}))
            assert await system.set_enabled(plugin_name, True) is True
            assert tool_name in _schema_names()
            assert await registry.execute(tool_name, {}) == "active"
        finally:
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_startup_tools_publish_only_when_startup_succeeds(tmp_path: Path) -> None:
    async def run() -> None:
        plugins_dir = tmp_path / "plugins"
        good_tool = "startup_success_probe_tool"
        failed_tool = "startup_failure_probe_tool"
        for plugin_name, tool_name, fails in (
            ("startup_success_probe", good_tool, False),
            ("startup_failure_probe", failed_tool, True),
        ):
            failure = "        raise RuntimeError('startup failed')\n" if fails else ""
            _write_plugin(
                plugins_dir,
                plugin_name,
                "from mohobot.api import LLMTool, registry, tool_schema\n"
                "class Plugin:\n"
                "    async def on_startup(self):\n"
                f"        registry.register(LLMTool(tool_schema({tool_name!r}, 'probe', {{}}, []), lambda: {plugin_name!r}))\n"
                + failure,
            )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        try:
            await system.load_plugins()
            assert good_tool in _schema_names()
            assert failed_tool not in _schema_names()
            assert await registry.execute(good_tool, {}) == "startup_success_probe"
            assert "error" in json.loads(await registry.execute(failed_tool, {}))
        finally:
            registry.unregister(good_tool)
            registry.unregister(failed_tool)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_startup_retry_uses_a_fresh_registration_stage(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "startup_retry_probe"
        tool_name = "startup_retry_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            plugin_name,
            "from mohobot.api import LLMTool, registry, tool_schema\n"
            "attempts = 0\n"
            "class Plugin:\n"
            "    async def on_startup(self):\n"
            "        global attempts\n"
            "        attempts += 1\n"
            f"        registry.register(LLMTool(tool_schema({tool_name!r}, 'probe', {{}}, []), lambda: str(attempts)))\n"
            "        if attempts == 1:\n"
            "            raise RuntimeError('first startup failed')\n"
            "    async def on_message(self, bot_id, event, raw): return False, None\n",
        )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        try:
            await system.load_plugins()
            assert tool_name not in _schema_names()
            assert "error" in json.loads(await registry.execute(tool_name, {}))

            await system.startup_plugins()
            assert tool_name in _schema_names()
            assert await registry.execute(tool_name, {}) == "2"
        finally:
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_startup_retry_restores_import_time_tools(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "startup_import_retry_probe"
        tool_name = "startup_import_retry_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            plugin_name,
            "from mohobot.api import LLMTool, registry, tool_schema\n"
            "attempts = 0\n"
            f"def handle(): return {tool_name!r}\n"
            f"registry.register(LLMTool(tool_schema({tool_name!r}, 'probe', {{}}, []), handle))\n"
            "class Plugin:\n"
            "    async def on_startup(self):\n"
            "        global attempts\n"
            "        attempts += 1\n"
            "        if attempts == 1:\n"
            "            raise RuntimeError('first startup failed')\n",
        )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        try:
            await system.load_plugins()
            assert tool_name not in _schema_names()
            assert "error" in json.loads(await registry.execute(tool_name, {}))

            await system.startup_plugins()
            assert tool_name in _schema_names()
            assert registry._owners[tool_name] == plugin_name
            assert await registry.execute(tool_name, {}) == tool_name
        finally:
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_eager_plugin_tools_join_the_owner_lifecycle(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "eager_owner_probe"
        tool_name = "eager_owner_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            plugin_name,
            "from mohobot.api import LLMTool, registry, tool_schema\n"
            f"def handle(): return 'dynamic'\n"
            f"if not registry.contains({tool_name!r}):\n"
            f"    registry.register(LLMTool(tool_schema({tool_name!r}, 'probe', {{}}, []), handle))\n"
            "class Plugin:\n"
            "    pass\n",
        )

        def eager_handle():
            return "eager"

        eager_handle.__module__ = f"plugins.{plugin_name}"
        registry.register(LLMTool(tool_schema(tool_name, "probe", {}, []), eager_handle))
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        try:
            await system.load_plugins()
            assert tool_name in _schema_names()
            assert registry._owners[tool_name] == plugin_name
            assert await registry.execute(tool_name, {}) == "eager"

            assert await system.unload_plugin(plugin_name) is True
            assert tool_name not in _schema_names()
            assert "error" in json.loads(await registry.execute(tool_name, {}))
        finally:
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_eager_plugin_tools_are_removed_when_disabled(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "eager_disabled_probe"
        tool_name = "eager_disabled_probe_tool"
        plugins_dir = tmp_path / "plugins"
        data_dir = tmp_path / "data"
        _write_plugin(
            plugins_dir,
            plugin_name,
            "from mohobot.api import LLMTool, registry, tool_schema\n"
            f"def handle(): return 'dynamic'\n"
            f"if not registry.contains({tool_name!r}):\n"
            f"    registry.register(LLMTool(tool_schema({tool_name!r}, 'probe', {{}}, []), handle))\n"
            "class Plugin:\n"
            "    pass\n",
        )
        data_dir.mkdir(parents=True)
        (data_dir / "plugins_state.json").write_text(
            json.dumps({plugin_name: {"enabled": False}}), encoding="utf-8"
        )

        def eager_handle():
            return "eager"

        eager_handle.__module__ = f"plugins.{plugin_name}"
        registry.register(LLMTool(tool_schema(tool_name, "probe", {}, []), eager_handle))
        system = PluginSystem(str(plugins_dir), str(data_dir))
        try:
            await system.load_plugins()
            assert tool_name not in _schema_names()
            assert "error" in json.loads(await registry.execute(tool_name, {}))

            assert await system.set_enabled(plugin_name, True) is True
            assert tool_name in _schema_names()
            assert registry._owners[tool_name] == plugin_name
            assert await registry.execute(tool_name, {}) == "dynamic"
        finally:
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_eager_plugin_tools_are_removed_on_global_shutdown(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "eager_shutdown_probe"
        tool_name = "eager_shutdown_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            plugin_name,
            "from mohobot.api import LLMTool, registry, tool_schema\n"
            f"def handle(): return 'dynamic'\n"
            f"if not registry.contains({tool_name!r}):\n"
            f"    registry.register(LLMTool(tool_schema({tool_name!r}, 'probe', {{}}, []), handle))\n"
            "class Plugin:\n"
            "    pass\n",
        )

        def eager_handle():
            return "eager"

        eager_handle.__module__ = f"plugins.{plugin_name}"
        registry.register(LLMTool(tool_schema(tool_name, "probe", {}, []), eager_handle))
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        shutdown = False
        try:
            await system.load_plugins()
            assert tool_name in _schema_names()
            assert registry._owners[tool_name] == plugin_name

            await system.shutdown_plugins()
            shutdown = True
            assert tool_name not in _schema_names()
            assert "error" in json.loads(await registry.execute(tool_name, {}))
        finally:
            registry.unregister(tool_name)
            if not shutdown:
                await system.shutdown_plugins()

    asyncio.run(run())


def test_reload_replaces_tools_and_failed_reload_keeps_previous_tools(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "reload_tool_probe"
        tool_name = "reload_tool_probe_tool"
        plugins_dir = tmp_path / "plugins"
        entry = _write_plugin(plugins_dir, plugin_name, _tool_plugin_source(tool_name, "old"))
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        try:
            await system.load_plugins()
            assert await registry.execute(tool_name, {}) == "old"

            entry.write_text(_tool_plugin_source(tool_name, "new value"), encoding="utf-8")
            assert await system.reload_plugin(plugin_name) is True
            assert _schema_names().count(tool_name) == 1
            assert await registry.execute(tool_name, {}) == "new value"

            entry.write_text("invalid Python !\n", encoding="utf-8")
            assert await system.reload_plugin(plugin_name) is False
            assert tool_name in _schema_names()
            assert await registry.execute(tool_name, {}) == "new value"
        finally:
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_unload_waits_for_tool_execution_and_removes_tool(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "inflight_tool_probe"
        tool_name = "inflight_tool_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            plugin_name,
            '''import asyncio
from mohobot.api import LLMTool, registry, tool_schema

started = None
release = None

async def handle():
    started.set()
    await release.wait()
    return "done"

registry.register(LLMTool(tool_schema("inflight_tool_probe_tool", "probe", {}, []), handle))

class Plugin:
    pass
''',
        )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        module = None
        try:
            await system.load_plugins()
            module_name = next(
                meta["module_name"] for meta in system._plugins if meta["name"] == plugin_name
            )
            module = sys.modules[module_name]
            module.started = asyncio.Event()
            module.release = asyncio.Event()

            call = asyncio.create_task(registry.execute(tool_name, {}))
            await module.started.wait()
            unload = asyncio.create_task(system.unload_plugin(plugin_name))
            await asyncio.sleep(0)
            assert not unload.done()
            assert "error" in json.loads(await registry.execute(tool_name, {}))

            module.release.set()
            assert await call == "done"
            assert await unload is True
            assert tool_name not in _schema_names()
            assert "error" in json.loads(await registry.execute(tool_name, {}))
        finally:
            module = sys.modules.get(f"mohobot_plugin_{plugin_name}")
            if module is not None and module.release is not None:
                module.release.set()
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_unload_timeout_restores_tool_admission(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "drain_timeout_probe"
        tool_name = "drain_timeout_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            plugin_name,
            f'''import asyncio
from mohobot.api import LLMTool, registry, tool_schema

first_started = asyncio.Event()
second_started = asyncio.Event()
release = asyncio.Event()
calls = 0

async def handle():
    global calls
    calls += 1
    (first_started if calls == 1 else second_started).set()
    await release.wait()
    return "done"

registry.register(LLMTool(tool_schema({tool_name!r}, "probe", {{}}, []), handle))

class Plugin:
    pass
''',
        )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        calls: list[asyncio.Task[str]] = []
        module = None
        previous_timeout = registry._owner_drain_timeout
        registry._owner_drain_timeout = 0.2
        try:
            await system.load_plugins()
            module_name = next(
                meta["module_name"] for meta in system._plugins if meta["name"] == plugin_name
            )
            module = sys.modules[module_name]
            calls.append(asyncio.create_task(registry.execute(tool_name, {})))
            await module.first_started.wait()

            assert await system.unload_plugin(plugin_name) is False
            assert system.list_plugins()[0]["loaded"] is True

            calls.append(asyncio.create_task(registry.execute(tool_name, {})))
            await asyncio.wait_for(module.second_started.wait(), timeout=0.5)
            module.release.set()
            assert await asyncio.gather(*calls) == ["done", "done"]
        finally:
            if module is not None:
                module.release.set()
            if calls:
                await asyncio.gather(*calls, return_exceptions=True)
            registry.unregister(tool_name)
            try:
                await system.shutdown_plugins()
            finally:
                registry._owner_drain_timeout = previous_timeout

    asyncio.run(run())


def test_cancelled_unload_restores_tool_admission(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "drain_cancel_probe"
        tool_name = "drain_cancel_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            plugin_name,
            f'''import asyncio
from mohobot.api import LLMTool, registry, tool_schema

first_started = asyncio.Event()
second_started = asyncio.Event()
release = asyncio.Event()
calls = 0

async def handle():
    global calls
    calls += 1
    (first_started if calls == 1 else second_started).set()
    await release.wait()
    return "done"

registry.register(LLMTool(tool_schema({tool_name!r}, "probe", {{}}, []), handle))

class Plugin:
    pass
''',
        )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        calls: list[asyncio.Task[str]] = []
        module = None
        try:
            await system.load_plugins()
            module_name = next(
                meta["module_name"] for meta in system._plugins if meta["name"] == plugin_name
            )
            module = sys.modules[module_name]
            call = asyncio.create_task(registry.execute(tool_name, {}))
            calls.append(call)
            await module.first_started.wait()

            unload = asyncio.create_task(system.unload_plugin(plugin_name))
            for _ in range(10):
                if plugin_name in registry._inactive_owners:
                    break
                await asyncio.sleep(0)
            assert plugin_name in registry._inactive_owners
            unload.cancel()
            try:
                await unload
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled unload unexpectedly completed")

            second_call = asyncio.create_task(registry.execute(tool_name, {}))
            calls.append(second_call)
            await asyncio.wait_for(module.second_started.wait(), timeout=0.5)
            module.release.set()
            assert await asyncio.gather(*calls) == ["done", "done"]
        finally:
            if module is not None:
                module.release.set()
            for task in calls:
                if not task.done():
                    task.cancel()
            if calls:
                await asyncio.gather(*calls, return_exceptions=True)
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_shutdown_cancels_stalled_tool_before_plugin_hook(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "shutdown_drain_probe"
        tool_name = "shutdown_drain_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            plugin_name,
            f'''import asyncio
from mohobot.api import LLMTool, registry, tool_schema

started = asyncio.Event()
finished = asyncio.Event()
shutdown_saw_finished = False

async def handle():
    started.set()
    try:
        await asyncio.Event().wait()
    finally:
        finished.set()

registry.register(LLMTool(tool_schema({tool_name!r}, "probe", {{}}, []), handle))

class Plugin:
    async def on_shutdown(self):
        global shutdown_saw_finished
        shutdown_saw_finished = finished.is_set()
''',
        )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        previous_timeout = registry._owner_drain_timeout
        registry._owner_drain_timeout = 0.2
        call = None
        module = None
        shutdown = False
        try:
            await system.load_plugins()
            module_name = next(
                meta["module_name"] for meta in system._plugins if meta["name"] == plugin_name
            )
            module = sys.modules[module_name]
            call = asyncio.create_task(registry.execute(tool_name, {}))
            await module.started.wait()

            await asyncio.wait_for(system.shutdown_plugins(), timeout=1.0)
            shutdown = True
            assert call.cancelled()
            assert module.finished.is_set()
            assert module.shutdown_saw_finished is True
            assert tool_name not in _schema_names()
        finally:
            if call is not None and not call.done():
                call.cancel()
            if call is not None:
                await asyncio.gather(call, return_exceptions=True)
            registry.unregister(tool_name)
            try:
                if not shutdown:
                    await system.shutdown_plugins()
            finally:
                registry._owner_drain_timeout = previous_timeout

    asyncio.run(run())


def test_failed_reload_restores_tools_removed_by_old_shutdown(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "reload_unregister_probe"
        tool_name = "reload_unregister_probe_tool"
        plugins_dir = tmp_path / "plugins"
        entry = _write_plugin(
            plugins_dir,
            plugin_name,
            f'''from mohobot.api import LLMTool, registry, tool_schema

def handle():
    return "old"

registry.register(LLMTool(tool_schema({tool_name!r}, "probe", {{}}, []), handle))

class Plugin:
    def on_shutdown(self):
        registry.unregister({tool_name!r})
''',
        )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        try:
            await system.load_plugins()
            assert await registry.execute(tool_name, {}) == "old"

            entry.write_text("invalid Python !\n", encoding="utf-8")
            assert await system.reload_plugin(plugin_name) is False
            assert _schema_names().count(tool_name) == 1
            assert registry._owners[tool_name] == plugin_name
            assert await registry.execute(tool_name, {}) == "old"

            assert await system.unload_plugin(plugin_name) is True
            assert tool_name not in _schema_names()
            assert tool_name not in registry._owners
        finally:
            registry.unregister(tool_name)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_constructor_tool_registration_is_staged_and_failed_construction_discards_tools(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        good_plugin = "constructor_tool_probe"
        good_tool = "constructor_tool_probe_tool"
        failed_plugin = "constructor_failure_probe"
        failed_tool = "constructor_failure_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(
            plugins_dir,
            good_plugin,
            "from mohobot.api import LLMTool, registry, tool_schema\n"
            "class Plugin:\n"
            "    def __init__(self):\n"
            f"        registry.register(LLMTool(tool_schema({good_tool!r}, 'probe', {{}}, []), lambda: 'ready'))\n"
            "    async def on_message(self, bot_id, event, raw): return False, None\n",
        )
        _write_plugin(
            plugins_dir,
            failed_plugin,
            "from mohobot.api import LLMTool, registry, tool_schema\n"
            "class Plugin:\n"
            "    def __init__(self):\n"
            f"        registry.register(LLMTool(tool_schema({failed_tool!r}, 'probe', {{}}, []), lambda: 'stale'))\n"
            "        raise RuntimeError('constructor failed')\n"
            "    async def on_message(self, bot_id, event, raw): return False, None\n",
        )
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        try:
            await system.load_plugins()
            assert good_tool in _schema_names()
            assert failed_tool not in _schema_names()
            assert await registry.execute(good_tool, {}) == "ready"
            assert "error" in json.loads(await registry.execute(failed_tool, {}))
        finally:
            registry.unregister(good_tool)
            registry.unregister(failed_tool)
            await system.shutdown_plugins()

    asyncio.run(run())


def test_global_shutdown_removes_plugin_tools(tmp_path: Path) -> None:
    async def run() -> None:
        plugin_name = "global_shutdown_probe"
        tool_name = "global_shutdown_probe_tool"
        plugins_dir = tmp_path / "plugins"
        _write_plugin(plugins_dir, plugin_name, _tool_plugin_source(tool_name, "active"))
        system = PluginSystem(str(plugins_dir), str(tmp_path / "data"))
        shutdown = False
        try:
            await system.load_plugins()
            assert tool_name in _schema_names()
            assert await registry.execute(tool_name, {}) == "active"

            await system.shutdown_plugins()
            shutdown = True
            assert tool_name not in _schema_names()
            assert "error" in json.loads(await registry.execute(tool_name, {}))
        finally:
            registry.unregister(tool_name)
            if not shutdown:
                await system.shutdown_plugins()

    asyncio.run(run())
