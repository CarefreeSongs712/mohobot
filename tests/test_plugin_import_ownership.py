import asyncio
import importlib
import sys
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent
from types import ModuleType

import pytest

from mohobot.interceptors.plugin_system import PluginNamespaceConflict, PluginSystem
from mohobot.plugins.imports import PluginImports


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(source), encoding="utf-8")
    return path


@contextmanager
def _import_state():
    """保存并还原 sys.path 与 sys.modules 新增项。

    不用 pytest monkeypatch fixture: 项目自研 legacy runner 只支持无参或仅
    ``tmp_path`` 的用例入口, 而 CI 在 push main 时仍会执行该 runner。
    """
    saved_path = sys.path
    saved_items = list(sys.path)
    saved_modules = set(sys.modules)
    try:
        yield
    finally:
        saved_path[:] = saved_items
        sys.path[:] = saved_items
        sys.path = saved_path
        for name in list(sys.modules):
            if name not in saved_modules:
                sys.modules.pop(name, None)


def test_rejected_collision_leaves_live_namespace_and_preexisting_path(tmp_path):
    async def run():
        plugins = tmp_path / "plugins"
        live_entry = _write(plugins, "ownership-live/main.py", """
            class Plugin:
                async def on_message(self, bot_id, event, raw):
                    from .relative import VALUE
                    return True, VALUE
        """)
        _write(plugins, "ownership-live/relative.py", "VALUE = 'live'\n")
        _write(plugins, "ownership_live/main.py", "class Plugin: pass\n")
        path = str(live_entry.parent)
        sys.path = [*sys.path, path]
        system = PluginSystem(str(plugins), str(tmp_path / "data"))
        try:
            assert await system.load_plugins() == 1
            module_name = "mohobot_plugin_ownership_live"
            live_module = sys.modules[module_name]
            assert await system.reload_plugin("ownership_live") is False
            instance = system._plugins[0]["instance"]
            with pytest.raises(PluginNamespaceConflict, match="ownership_live"):
                await system.reload_plugins()
            assert system._plugins[0]["instance"] is instance
            assert sys.modules.get(module_name) is live_module
            assert await system.intercept("bot", object(), {}) == (True, "live")
        finally:
            await system.shutdown_plugins()
        assert any(item is path for item in sys.path)

    with _import_state():
        asyncio.run(run())


async def _check_unload_objects(tmp_path: Path, replace_objects: bool) -> None:
    plugins = tmp_path / "plugins"
    entry = _write(plugins, "ownership_probe/main.py", """
        from ownership_preloaded import VALUE
        from . import eager

        class Plugin:
            async def on_message(self, bot_id, event, raw):
                from . import delayed
                import ownership_absolute
                return True, VALUE + eager.VALUE + delayed.VALUE + ownership_absolute.VALUE
    """)
    preloaded_file = _write(plugins, "ownership_probe/ownership_preloaded.py", "VALUE = 1\n")
    _write(plugins, "ownership_probe/eager.py", "VALUE = 2\n")
    _write(plugins, "ownership_probe/delayed.py", "VALUE = 4\n")
    _write(plugins, "ownership_probe/ownership_absolute.py", "VALUE = 8\n")
    preloaded = ModuleType("ownership_preloaded")
    preloaded.__file__ = str(preloaded_file)
    preloaded.VALUE = 1
    sys.modules[preloaded.__name__] = preloaded
    sys.path = sys.path.copy()
    base = "mohobot_plugin_ownership_probe"
    eager_name = f"{base}.eager"
    delayed_name = f"{base}.delayed"
    unrelated_child = ModuleType(f"{base}.unowned")
    replacement = ModuleType(eager_name)
    replacement.__file__ = str(entry.with_name("eager.py"))
    system = PluginSystem(str(plugins), str(tmp_path / "data"))
    try:
        assert await system.load_plugins() == 1
        assert delayed_name not in sys.modules
        assert "ownership_absolute" not in sys.modules
        assert await system.intercept("bot", object(), {}) == (True, 15)
        assert delayed_name in sys.modules
        assert "ownership_absolute" in sys.modules
        sys.modules[unrelated_child.__name__] = unrelated_child
        path = next(item for item in sys.path if item == str(entry.parent))
        foreign_path = str(entry.parent)
        assert foreign_path == path and foreign_path is not path
        if replace_objects:
            sys.modules[eager_name] = replacement
            index = next(i for i, item in enumerate(sys.path) if item is path)
            sys.path[index] = foreign_path
        assert await system.unload_plugin("ownership_probe") is True
        assert not {base, delayed_name, "ownership_absolute"} & sys.modules.keys()
        assert sys.modules.get(preloaded.__name__) is preloaded
        assert sys.modules.get(unrelated_child.__name__) is unrelated_child
        assert sys.modules.get(eager_name) is (replacement if replace_objects else None)
        assert all(item is not path for item in sys.path)
        assert any(item is foreign_path for item in sys.path) == replace_objects
    finally:
        await system.shutdown_plugins()
        for name in (base, eager_name, delayed_name, "ownership_absolute"):
            sys.modules.pop(name, None)


def test_unload_removes_only_current_objects_owned_by_generation(tmp_path):
    # 两轮各自快照/还原 sys.path 与 sys.modules: 第一轮残留的命名空间子模块
    # 会被新加载器的模块名冲突预检视为外部占用, 必须按轮隔离。
    for label, replace_objects in (("plain", False), ("replaced", True)):
        with _import_state():
            asyncio.run(_check_unload_objects(tmp_path / label, replace_objects=replace_objects))


def test_foreign_entry_collision_keeps_current_plugin_dispatching(tmp_path):
    async def run():
        plugins = tmp_path / "plugins"
        _write(plugins, "ownership_conflict.py", """
            class Plugin:
                async def on_message(self, bot_id, event, raw):
                    return True, "old"
                async def on_shutdown(self):
                    self.stopped = True
        """)
        system = PluginSystem(str(plugins), str(tmp_path / "data"))
        name = "mohobot_plugin_ownership_conflict"
        foreign = ModuleType(name)
        sys.modules[name] = foreign
        try:
            assert await system.load_plugins() == 0
            assert sys.modules[name] is foreign
            assert "插件模块名冲突" in system.list_plugins()[0]["error"]
            del sys.modules[name]
            assert await system.reload_plugin("ownership_conflict") is True
            instance = system._plugins[0]["instance"]
            sys.modules[name] = foreign
            assert await system.reload_plugin("ownership_conflict") is False
            with pytest.raises(PluginNamespaceConflict, match=name):
                await system.reload_plugins()
            assert sys.modules[name] is foreign
            assert not hasattr(instance, "stopped")
            assert await system.intercept("bot", object(), {}) == (True, "old")
        finally:
            await system.shutdown_plugins()
        assert sys.modules[name] is foreign

    with _import_state():
        asyncio.run(run())


def test_shared_root_keeps_path_and_absolute_namespace_until_last_owner_detaches(tmp_path):
    root = tmp_path / "shared"
    _write(root, "ownership_namespace/helper.py", "VALUE = 7\n")
    package = "ownership_namespace"
    helper = f"{package}.helper"
    with _import_state():
        sys.path = sys.path.copy()
        first = PluginImports("mohobot_plugin_ownership_first", str(root))
        second = PluginImports("mohobot_plugin_ownership_second", str(root))
        first.attach()
        second.attach()
        try:
            path = next(item for item in sys.path if item == str(root))
            assert importlib.import_module(helper).VALUE == 7
            module = sys.modules[package]
            assert module.__file__ is None
            first.detach()
            assert any(item is path for item in sys.path)
            assert sys.modules[package] is module
            assert importlib.import_module(helper).VALUE == 7
            second.detach()
            assert all(item is not path for item in sys.path)
            assert package not in sys.modules
            assert helper not in sys.modules
        finally:
            first.detach()
            second.detach()
            sys.modules.pop(helper, None)
            sys.modules.pop(package, None)


def test_failed_replacement_does_not_overwrite_foreign_module_on_rollback(tmp_path):
    async def run():
        plugins = tmp_path / "plugins"
        entry = _write(plugins, "ownership_rollback/main.py", """
            import ownership_rollback_helper
            class Plugin: pass
        """)
        _write(plugins, "ownership_rollback/ownership_rollback_helper.py", "VALUE = 1\n")
        sys.path = sys.path.copy()
        system = PluginSystem(str(plugins), str(tmp_path / "data"))
        helper = "ownership_rollback_helper"
        try:
            assert await system.load_plugins() == 1
            original = sys.modules[helper]
            entry.write_text(dedent('''
                import sys
                from types import ModuleType
                sys.modules['ownership_rollback_helper'] = ModuleType('ownership_rollback_helper')
                raise ValueError('fixture replacement failure')
            '''), encoding="utf-8")
            assert await system.reload_plugin("ownership_rollback") is False
            assert not system.list_plugins()[0]["loaded"]
            assert "previous plugin modules are occupied" in system.list_plugins()[0]["error"]
            assert helper in sys.modules and sys.modules[helper] is not original
            assert "mohobot_plugin_ownership_rollback" not in sys.modules
        finally:
            await system.shutdown_plugins()
            sys.modules.pop(helper, None)

    with _import_state():
        asyncio.run(run())


def test_vanished_bytecode_cache_does_not_interrupt_unload(tmp_path):
    async def run():
        plugins = tmp_path / "plugins"
        entry = _write(plugins, "ownership_cache/main.py", "class Plugin: pass\n")
        cache_dir = entry.parent / "__pycache__"
        cache_dir.mkdir(exist_ok=True)
        system = PluginSystem(str(plugins), str(tmp_path / "data"))
        assert await system.load_plugins() == 1

        def vanished(path, *, ignore_errors):
            assert path == cache_dir
            assert ignore_errors
            raise FileNotFoundError(path)

        import mohobot.interceptors.plugin_system as plugin_system

        original_rmtree = plugin_system.shutil.rmtree
        plugin_system.shutil.rmtree = vanished
        try:
            await system.shutdown_plugins()
        finally:
            plugin_system.shutil.rmtree = original_rmtree
        assert not system.list_plugins()[0]["loaded"]
        assert "mohobot_plugin_ownership_cache" not in sys.modules
        assert str(entry.parent) not in sys.path

    with _import_state():
        asyncio.run(run())


def test_failed_import_releases_only_its_own_helpers(tmp_path):
    async def run():
        plugins = tmp_path / "plugins"
        entry = _write(plugins, "ownership_failure/main.py", """
            from . import owned
            import ownership_existing
            raise ValueError("fixture startup failure")
        """)
        _write(plugins, "ownership_failure/owned.py", "VALUE = 1\n")
        existing = ModuleType("ownership_existing")
        existing.__file__ = str(entry.with_name("foreign.py"))
        sys.modules[existing.__name__] = existing
        sys.path = sys.path.copy()
        system = PluginSystem(str(plugins), str(tmp_path / "data"))
        try:
            assert await system.load_plugins() == 0
            assert "fixture startup failure" in system.list_plugins()[0]["error"]
            assert "mohobot_plugin_ownership_failure" not in sys.modules
            assert "mohobot_plugin_ownership_failure.owned" not in sys.modules
            assert sys.modules.get(existing.__name__) is existing
            assert str(entry.parent) not in sys.path
        finally:
            await system.shutdown_plugins()

    with _import_state():
        asyncio.run(run())
