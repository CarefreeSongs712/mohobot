import asyncio
import json
import sys
from pathlib import Path

from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.services.llm_tools import registry


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
