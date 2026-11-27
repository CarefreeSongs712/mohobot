"""Runtime market rollback/cleanup uses only synthetic plugins and temporary data."""

import asyncio
from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.plugin_market import journal
from mohobot.plugin_market.service import MarketService
from mohobot.plugins.manifest import parse_manifest
from mohobot.plugins.sources import load_sources_strict, save_source
from mohobot.web_panel.app import WebPanel

OLD = {"schema_version": 1, "version": "1.0.0"}
NEW = {"schema_version": 1, "version": "2.0.0"}


def _package(path: Path, version: str, code: str = "class Plugin: pass\n"):
    path.mkdir(parents=True)
    (path / "main.py").write_text(code, encoding="utf-8")
    (path / "metadata.yaml").write_text(dedent(f"""\
        name: demo
        author: TestFixture
        display_name: Offline runtime fixture
        version: {version}
        description: Check transactional activation
        repo: https://github.com/TestFixture/demo
        license: MIT
        api_version: 1
    """), encoding="utf-8")
    return parse_manifest(path / "metadata.yaml")


async def _fixture(tmp_path, code="class Plugin: pass\n"):
    data = tmp_path / "data"
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "market_peer.py").write_text("class Plugin: pass\n", encoding="utf-8")
    _package(data / "plugins" / "demo", "1.0.0", code)
    await save_source(data, "demo", OLD)
    plugins = PluginSystem(str(builtin), str(data))
    await plugins.load_plugins()
    return data, plugins


async def _replace(data, plugins, code="class Plugin: pass\n"):
    candidate = data / "plugins" / ".fixture-candidate"
    manifest = _package(candidate, "2.0.0", code)

    async def recheck():
        return None

    return await plugins.replace_market_plugin(
        manifest, candidate, NEW, expect_installed=True, recheck=recheck,
    )


_SHUTDOWN = dedent("""\
    import asyncio
    class Plugin:
        allow_shutdown = False
        entered = None
        release = None
        async def on_shutdown(self):
            if self.entered is not None:
                self.entered.set()
            if self.release is not None:
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        pass
            elif not self.allow_shutdown:
                raise RuntimeError("fixture shutdown failed")
""")


async def _probe_old_retirement_remains_owned_until_recovery(tmp_path, operation, failure):
    data, plugins = await _fixture(tmp_path, _SHUTDOWN)
    old = plugins._market_meta("demo")
    peer = plugins._market_meta("market_peer")["instance"]
    instance = old["instance"]
    if failure == "timeout":
        instance.entered = asyncio.Event()
        instance.release = asyncio.Event()
        plugins._hook_timeout = 0.01
    try:
        with pytest.raises(journal.MarketRecoveryError):
            if operation == "update":
                await _replace(data, plugins)
            else:
                await plugins.uninstall_market_plugin("demo", delete_config=False, delete_data=False)
        recovery = plugins._market_recovery
        assert recovery is not None
        assert recovery.previous is old
        assert old["loaded"] and old["_retiring"]
        assert not plugins._can_dispatch(old)
        assert recovery.operation.intent.decision == "pending"
        assert parse_manifest(data / "plugins" / "demo" / "metadata.yaml").version == "1.0.0"
        assert (await load_sources_strict(data))["demo"] == OLD
        assert plugins._market_meta("market_peer")["instance"] is peer
        # Retry must not move files or import over the undrained old instance.
        with pytest.raises((RuntimeError, TimeoutError)):
            await plugins.reload_plugin("demo")
        assert plugins._market_recovery is recovery
        assert plugins._market_meta("demo")["instance"] is instance
        if failure == "timeout":
            assert instance.entered.is_set()
            assert not old["_hook_task"].done()
            instance.release.set()
            await old["_hook_task"]
        else:
            instance.allow_shutdown = True
        assert await plugins.reload_plugin("demo")
        restored = plugins._market_meta("demo")
        assert restored["instance"] is not instance
        assert plugins._can_dispatch(restored)
        assert plugins._market_recovery is None
        assert not list((data / "plugins" / ".market-journal").iterdir())
        assert plugins._market_meta("market_peer")["instance"] is peer
    finally:
        instance.allow_shutdown = True
        if instance.release is not None:
            instance.release.set()
        current = plugins._market_meta("demo")
        if current and current.get("instance"):
            current["instance"].allow_shutdown = True
        await plugins.shutdown_plugins()


async def _probe_committed_cleanup_retry_does_not_reactivate_target(tmp_path, monkeypatch, operation, settle):
    data, plugins = await _fixture(tmp_path)
    peer = plugins._market_meta("market_peer")["instance"]
    discard = journal._discard
    failed = False

    def transient(path):
        nonlocal failed
        if path.name.startswith(".done-") and not failed:
            failed = True
            raise PermissionError("fixture transient final discard")
        return discard(path)

    monkeypatch.setattr(journal, "_discard", transient)
    try:
        if operation == "update":
            result = await _replace(data, plugins)
        else:
            _, result = await plugins.uninstall_market_plugin("demo", delete_config=False, delete_data=False)
        assert failed and result["warning"]
        assert plugins._market_recovery is not None
        assert plugins._market_recovery.operation.intent.decision == "committed"
        published = plugins._market_meta("demo")
        if settle == "shutdown":
            await plugins.shutdown_plugins()
        else:
            assert await plugins.reload_plugin("demo") == (operation == "update")
            assert plugins._market_meta("demo") is published
            assert plugins._market_meta("market_peer")["instance"] is peer
        assert plugins._market_recovery is None
        assert not list((data / "plugins" / ".market-journal").iterdir())
        assert (await load_sources_strict(data)).get("demo") == (NEW if operation == "update" else None)
    finally:
        await plugins.shutdown_plugins()


@pytest.mark.asyncio
async def test_failed_restore_keeps_recovery_intent_for_retry(tmp_path):
    old_code = dedent("""\
        from pathlib import Path
        class Plugin:
            @classmethod
            def inject_plugin_data_dir(cls, path):
                cls.plugin_data_dir = Path(path)
            async def on_startup(self):
                marker = type(self).plugin_data_dir / "restore-attempts"
                attempts = int(marker.read_text() if marker.exists() else "0") + 1
                marker.write_text(str(attempts))
                if attempts == 2:
                    raise RuntimeError("fixture restoration fails once")
    """)
    candidate_code = "class Plugin:\n    async def on_startup(self):\n        raise RuntimeError('candidate activation fails')\n"
    data, plugins = await _fixture(tmp_path, old_code)
    assert (data / "plugins_data" / "demo" / "restore-attempts").read_text() == "1"
    try:
        with pytest.raises(journal.MarketRecoveryError, match="原版本激活失败"):
            await _replace(data, plugins, candidate_code)

        recovery = plugins._market_recovery
        assert recovery is not None
        assert recovery.restoring_previous
        assert recovery.operation.intent.decision == "rolled_back"
        assert not plugins._can_dispatch(plugins._market_meta("demo"))
        assert (data / "plugins_data" / "demo" / "restore-attempts").read_text() == "2"

        assert await plugins.reload_plugin("demo")
        restored = plugins._market_meta("demo")
        assert plugins._can_dispatch(restored)
        assert plugins._market_recovery is None
        assert (data / "plugins_data" / "demo" / "restore-attempts").read_text() == "3"
    finally:
        await plugins.shutdown_plugins()


async def _probe_persistent_terminal_cleanup_failure_does_not_skip_shutdown(tmp_path, monkeypatch):
    data, plugins = await _fixture(tmp_path)
    peer = plugins._market_meta("market_peer")["instance"]
    shutdown_calls = []

    async def peer_shutdown():
        shutdown_calls.append("market_peer")

    peer.on_shutdown = peer_shutdown
    discard = journal._discard

    def persistent_failure(path):
        if path.name.startswith(".done-"):
            raise PermissionError("fixture persistent terminal cleanup failure")
        return discard(path)

    monkeypatch.setattr(journal, "_discard", persistent_failure)
    try:
        result = await _replace(data, plugins)
        assert result["warning"]
        candidate = plugins._market_meta("demo")
        async def candidate_shutdown():
            shutdown_calls.append("demo")
        candidate["instance"].on_shutdown = candidate_shutdown

        await plugins.shutdown_plugins()

        assert sorted(shutdown_calls) == ["demo", "market_peer"]
        assert not candidate["loaded"]
        assert not plugins._market_meta("market_peer")["loaded"]
        assert plugins._market_recovery is not None
        assert any(
            path.name.startswith(".done-")
            for path in (data / "plugins" / ".market-journal").iterdir()
        )

        monkeypatch.setattr(journal, "_discard", discard)
        await plugins.shutdown_plugins()
        assert plugins._market_recovery is None
        assert not list((data / "plugins" / ".market-journal").iterdir())
    finally:
        monkeypatch.setattr(journal, "_discard", discard)
        await plugins.shutdown_plugins()


async def _probe_uninstall_api_uses_runtime_transaction(tmp_path, monkeypatch, with_market, failure):
    data, plugins = await _fixture(tmp_path)
    old = plugins._market_meta("demo")["instance"]
    peer = plugins._market_meta("market_peer")["instance"]
    market = MarketService(data, data / "plugins", fetcher=lambda url: {}) if with_market else None
    panel = WebPanel(
        password_hash=WebPanel._hash_password("test-password"), data_dir=str(data),
        plugin_system=plugins, plugin_market=market,
    )
    collision = None
    if failure == "preflight":
        async def reject(*args, **kwargs):
            raise journal.MarketRecoveryError("fixture preflight failed")
        monkeypatch.setattr(plugins._market_journal, "begin_uninstall", reject)
    elif failure == "collision":
        from uuid import UUID
        operation_id = UUID(int=1)
        monkeypatch.setattr(journal, "uuid4", lambda: operation_id)
        collision = data / "plugin_recycle" / f"demo-{operation_id.hex}"
        collision.mkdir(parents=True)
        (collision / "sentinel").write_text("preserve existing recycle entry", encoding="utf-8")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel._app), base_url="http://test") as client:
            token = (await client.post("/api/login", json={"username": "admin", "password": "test-password"})).json()["token"]
            response = await client.delete("/api/plugins/demo", headers={"Authorization": f"Bearer {token}"})
        # 事务待恢复/事务目标冲突都抛 MarketRecoveryError, 按可重试状态映射为
        # 409 (#172 审查 P3); 无失败时正常 200。
        expected_status = 409 if failure else 200
        assert response.status_code == expected_status, response.text
        assert plugins._market_meta("market_peer")["instance"] is peer
        assert plugins._can_dispatch(plugins._market_meta("market_peer"))
        if failure:
            assert plugins._market_meta("demo")["instance"] is old
            assert plugins._market_recovery is None
            if collision is not None:
                assert (collision / "sentinel").read_text() == "preserve existing recycle entry"
            assert plugins._can_dispatch(plugins._market_meta("demo"))
            assert (await load_sources_strict(data))["demo"] == OLD
        else:
            assert plugins._market_meta("demo") is None
            assert not (data / "plugins" / "demo").exists()
            assert Path(response.json()["recycle"]).is_dir()
            assert "demo" not in await load_sources_strict(data)
    finally:
        await plugins.shutdown_plugins()


_MISSING = object()


class _MP:
    """最小 monkeypatch 替身: legacy runner 只支持无参或仅 tmp_path 的入口。"""

    def __init__(self):
        self._undo = []

    def _resolve(self, target):
        if isinstance(target, str):
            import importlib
            parts = target.split(".")
            obj = importlib.import_module(parts[0])
            for part in parts[1:-1]:
                obj = getattr(obj, part)
            return obj, parts[-1]
        return target, None

    def setattr(self, target, name, value=None):  # noqa: A003 - 对齐 pytest 接口
        obj, attr = self._resolve(target)
        if attr is not None:
            name, value = attr, name
        self._undo.append((obj, name, vars(obj).get(name, _MISSING)))
        setattr(obj, name, value)

    def setitem(self, mapping, key, value):
        self._undo.append((mapping, key, mapping.get(key, _MISSING)))
        mapping[key] = value

    def undo(self):
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


def test_old_retirement_remains_owned_until_recovery(tmp_path):
    for index, values in enumerate(__import__("itertools").product(["update", "uninstall"], ["raise", "timeout"])):
        operation, failure = values
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        mp = _MP()
        try:
            asyncio.run(_probe_old_retirement_remains_owned_until_recovery(case_dir, operation, failure))
        finally:
            mp.undo()


def test_committed_cleanup_retry_does_not_reactivate_target(tmp_path):
    for index, values in enumerate(__import__("itertools").product(["update", "uninstall"], ["reload", "shutdown"])):
        operation, settle = values
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        mp = _MP()
        try:
            asyncio.run(_probe_committed_cleanup_retry_does_not_reactivate_target(case_dir, mp, operation, settle))
        finally:
            mp.undo()


def test_persistent_terminal_cleanup_failure_does_not_skip_shutdown(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    mp = _MP()
    try:
        asyncio.run(_probe_persistent_terminal_cleanup_failure_does_not_skip_shutdown(case_dir, mp))
    finally:
        mp.undo()


def test_uninstall_api_uses_runtime_transaction(tmp_path):
    for index, values in enumerate(__import__("itertools").product([True, False], ["preflight", "collision", None])):
        with_market, failure = values
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        mp = _MP()
        try:
            asyncio.run(_probe_uninstall_api_uses_runtime_transaction(case_dir, mp, with_market, failure))
        finally:
            mp.undo()
