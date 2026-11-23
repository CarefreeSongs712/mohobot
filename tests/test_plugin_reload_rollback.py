import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("helper_removed_before_reload", [False, True])
def test_reload_failure_keeps_previous_instance(tmp_path: Path, helper_removed_before_reload: bool) -> None:
    async def run() -> None:
        _write_plugin(tmp_path, 1)
        system = PluginSystem(str(tmp_path / "plugins"), str(tmp_path / "data"))
        try:
            assert await system.load_plugins() == 1
            before = next(m for m in system._plugins if m["name"] == "rollback_plugin")
            instance = before["instance"]
            assert instance.info["description"] == "rollback value=1"
            module_name = type(instance).__module__
            helper_name = f"{module_name}.helper"
            original_entry = sys.modules[module_name]
            original_helper = sys.modules[helper_name]
            if helper_removed_before_reload:
                # The old generation no longer owns this sys.modules slot.
                del sys.modules[helper_name]

            entry = tmp_path / "plugins" / "rollback_plugin" / "main.py"
            entry.write_text("this is not python !!!\n", encoding="utf-8")
            assert await system.reload_plugin("rollback_plugin") is False

            after = next(m for m in system._plugins if m["name"] == "rollback_plugin")
            assert after["loaded"] is True
            assert after["instance"] is instance
            assert after["instance"].info["description"] == "rollback value=1"
            assert sys.modules[module_name] is original_entry
            if helper_removed_before_reload:
                assert helper_name not in sys.modules
            else:
                assert sys.modules[helper_name] is original_helper

            _write_plugin(tmp_path, 2)
            assert await system.reload_plugin("rollback_plugin") is True
            updated = next(m for m in system._plugins if m["name"] == "rollback_plugin")
            assert updated["instance"].info["description"] == "rollback value=2"
        finally:
            await system.shutdown_plugins()

    asyncio.run(run())


@pytest.mark.parametrize("next_action", ["restore", "retry", "disable", "shutdown"])
def test_cancelled_reload_keeps_rollback_until_cleanup(tmp_path: Path, monkeypatch, next_action: str) -> None:
    blocked_cleanup = next_action != "restore"
    async def run() -> None:
        _write_plugin(tmp_path, 1)
        system = PluginSystem(str(tmp_path / "plugins"), str(tmp_path / "data"))
        assert await system.load_plugins() == 1
        old = system._plugins[0]["instance"]
        module_name = type(old).__module__
        helper_name = f"{module_name}.helper"
        old_modules = {name: sys.modules[name] for name in (module_name, helper_name)}
        startup_entered = asyncio.Event()
        startup_cancelled = asyncio.Event()
        release = asyncio.Event()
        candidate = None
        calls = []

        async def startup_old():
            calls.append("old-startup")
            assert sys.modules[helper_name] is old_modules[helper_name]
            assert await system.intercept("bot", object(), {}) == (False, None)

        async def message_old(*_args):
            return True, "old"

        async def candidate_startup():
            startup_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                startup_cancelled.set()
                if blocked_cleanup:
                    await release.wait()
                raise

        async def candidate_shutdown():
            calls.append("candidate-shutdown")
            assert sys.modules[module_name] is not old_modules[module_name]

        load_one = system._load_one

        def load_candidate(*args, **kwargs):
            nonlocal candidate
            candidate = load_one(*args, **kwargs)
            candidate["instance"].on_startup = candidate_startup
            candidate["instance"].on_shutdown = candidate_shutdown
            return candidate

        old.on_startup = startup_old
        old.on_message = message_old
        monkeypatch.setattr(system, "_load_one", load_candidate)
        _write_plugin(tmp_path, 2)
        reload = asyncio.create_task(system.reload_plugin("rollback_plugin"))
        try:
            await asyncio.wait_for(startup_entered.wait(), timeout=30)
            assert await system.intercept("bot", object(), {}) == (False, None)
            if blocked_cleanup:
                system._drain_timeout = 0
            reload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(reload, timeout=5)
            await asyncio.wait_for(startup_cancelled.wait(), timeout=5)
            if blocked_cleanup:
                assert system._plugins[0] is candidate
                assert not system.list_plugins()[0]["loaded"]
                assert sys.modules[module_name] is not old_modules[module_name]
                hook = candidate["_hook_task"]
                assert not hook.done() and calls == []
                release.set()
                assert (await asyncio.wait({hook}, timeout=5))[0] == {hook}
                system._drain_timeout = 5
                if next_action == "disable":
                    assert await system.set_enabled("rollback_plugin", False)
                elif next_action == "shutdown":
                    await system.shutdown_plugins()
                else:
                    # Recovery uses the saved instance even if the failed entry disappeared.
                    (tmp_path / "plugins" / "rollback_plugin" / "main.py").unlink()
                    assert await system.reload_plugin("rollback_plugin") is False

            if next_action in {"disable", "shutdown"}:
                assert calls == ["candidate-shutdown"]
                assert not system.list_plugins()[0]["loaded"]
                assert all(name not in sys.modules for name in old_modules)
                if next_action == "disable":
                    assert system._load_state()["rollback_plugin"]["enabled"] is False
                return

            assert calls == ["candidate-shutdown", "old-startup"]
            assert system._plugins[0]["instance"] is old
            assert system.list_plugins()[0]["loaded"]
            assert all(sys.modules[name] is module for name, module in old_modules.items())
            assert await system.intercept("bot", object(), {}) == (True, "old")
            assert await system.set_enabled("rollback_plugin", False)
            assert all(name not in sys.modules for name in old_modules)
        finally:
            release.set()
            await asyncio.gather(reload, return_exceptions=True)
            await system.shutdown_plugins()

    asyncio.run(run())
