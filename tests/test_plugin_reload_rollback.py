import asyncio
import textwrap
from pathlib import Path

from mohobot.interceptors.plugin_system import PluginSystem


def _write_plugin(root: Path, value: int) -> None:
    plugin = root / "plugins" / "rollback_plugin"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "metadata.yaml").write_text(
        textwrap.dedent(
            """
            name: rollback_plugin
            author: CarefreeSongs712
            display_name: Rollback Probe
            version: 0.1.0
            description: reload rollback probe
            repo: https://github.com/CarefreeSongs712/mohobot-plugin-rollback-probe
            license: MIT
            api_version: 1
            entry: main.py
            tags: [test]
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "helper.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    (plugin / "main.py").write_text(
        "from .helper import VALUE\n"
        "\n"
        "class Plugin:\n"
        "    info = {\"description\": f\"rollback value={VALUE}\"}\n",
        encoding="utf-8",
    )


def test_reload_failure_keeps_previous_instance(tmp_path: Path) -> None:
    async def run() -> None:
        _write_plugin(tmp_path, 1)
        system = PluginSystem(str(tmp_path / "plugins"), str(tmp_path / "data"))
        await system.load_plugins()
        before = next(m for m in system._plugins if m["name"] == "rollback_plugin")
        instance = before["instance"]
        assert instance.info["description"] == "rollback value=1"

        entry = tmp_path / "plugins" / "rollback_plugin" / "main.py"
        entry.write_text("this is not python !!!\n", encoding="utf-8")
        assert await system.reload_plugin("rollback_plugin") is False

        after = next(m for m in system._plugins if m["name"] == "rollback_plugin")
        assert after["loaded"] is True
        assert after["instance"] is instance
        assert after["instance"].info["description"] == "rollback value=1"

        _write_plugin(tmp_path, 2)
        assert await system.reload_plugin("rollback_plugin") is True
        updated = next(m for m in system._plugins if m["name"] == "rollback_plugin")
        assert updated["instance"].info["description"] == "rollback value=2"

    asyncio.run(run())
