import asyncio
from pathlib import Path

from mohobot.interceptors.plugin_system import PluginSystem


def test_unload_plugin_stops_and_removes_only_target(tmp_path: Path) -> None:
    async def run() -> None:
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        system = PluginSystem(str(plugins), str(tmp_path / "data"))
        for name in ("alpha", "beta"):
            (plugins / f"{name}.py").write_text(
                "class Plugin:\n"
                "    async def on_message(self, bot_id, event, raw): return (False, None)\n"
                "    async def on_shutdown(self): self.stopped = True\n",
                encoding="utf-8",
            )
        await system.load_plugins()
        beta = next(m["instance"] for m in system._plugins if m["name"] == "beta")
        alpha = next(m["instance"] for m in system._plugins if m["name"] == "alpha")

        assert await system.unload_plugin("alpha") is True
        assert alpha.stopped is True
        assert all(m["name"] != "alpha" for m in system._plugins)
        assert next(m["instance"] for m in system._plugins if m["name"] == "beta") is beta

    asyncio.run(run())
