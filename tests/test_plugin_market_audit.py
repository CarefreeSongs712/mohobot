"""Domain audit must use the authenticated actor and never change the business result."""

import asyncio
import json
from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.plugins.sources import save_source
from mohobot.web_panel.app import WebPanel

PASSWORD = "test-password"
USERNAME = "offline-operator"
SOURCE_RECORD = {"schema_version": 1, "version": "1.0.0"}


def _package(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "main.py").write_text("class Plugin: pass\n", encoding="utf-8")
    (path / "metadata.yaml").write_text(dedent("""\
        name: demo
        author: TestFixture
        display_name: Audit fixture
        version: 1.0.0
        description: Audit fixture
        repo: https://github.com/TestFixture/demo
        license: MIT
        api_version: 1
    """), encoding="utf-8")


def _audit_entries(data: Path) -> list[dict]:
    path = data / "audit" / "web_admin.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def _login(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


class _FakeSource:
    def __init__(self, source_id: str, name: str, url: str) -> None:
        self.id = source_id
        self.name = name
        self.url = url
        self.official = False


class _FakeMarket:
    """Only the surface the plugin-source and market install routes touch."""

    def __init__(self) -> None:
        self.sources: list[_FakeSource] = []
        self.installed: list[tuple[str, str]] = []

    async def reload_sources(self):
        return self.sources

    async def add_source(self, *, source_id, name, url):
        source = _FakeSource(source_id, name, url)
        self.sources.append(source)
        return source

    async def remove_source(self, source_id):
        self.sources = [source for source in self.sources if source.id != source_id]

    def source_status(self, source_id):
        return {"status": "fresh", "last_success_at": None}

    def entries(self):
        return []

    async def install(self, plugin_id, *, source_id="official", source_generation=None, plugin_system=None):
        self.installed.append((plugin_id, source_id))
        return "demo", Path("demo"), {"activation": "active"}


def _panel(data: Path, *, market, plugins=None) -> WebPanel:
    return WebPanel(
        username=USERNAME,
        password_hash=WebPanel._hash_password(PASSWORD),
        data_dir=str(data),
        plugin_system=plugins,
        plugin_market=market,
    )


@pytest.mark.asyncio
async def test_source_and_install_audit_use_authenticated_actor(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    market = _FakeMarket()
    system = PluginSystem(str(tmp_path / "builtin"), str(data))
    panel = _panel(data, market=market, plugins=system)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=panel._app), base_url="http://offline.test",
        ) as client:
            headers = await _login(client)
            added = await client.post(
                "/api/plugin-sources", headers=headers,
                json={"id": "custom", "name": "Custom", "url": "https://example.com/index.json"},
            )
            assert added.status_code == 200, added.text
            installed = await client.post(
                "/api/plugins/market/install", headers=headers,
                json={"plugin_id": "TestFixture/demo"},
            )
            assert installed.status_code == 200, installed.text
        entries = _audit_entries(data)
        domain = {entry["action"]: entry for entry in entries if entry["action"] in {
            "plugin_source_add", "plugin_market_install",
        }}
        assert domain["plugin_source_add"]["actor"] == USERNAME
        assert domain["plugin_market_install"]["actor"] == USERNAME
        assert domain["plugin_market_install"]["success"] is True
    finally:
        await panel.stop()


@pytest.mark.asyncio
async def _probe_source_audit_failure(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    market = _FakeMarket()
    panel = _panel(data, market=market)

    async def broken_write(**kwargs):
        raise OSError("fixture audit storage failure")

    monkeypatch.setattr(panel._audit, "write", broken_write)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=panel._app), base_url="http://offline.test",
        ) as client:
            headers = await _login(client)
            added = await client.post(
                "/api/plugin-sources", headers=headers,
                json={"id": "custom", "name": "Custom", "url": "https://example.com/index.json"},
            )
            assert added.status_code == 200, added.text
            assert added.json()["audit"] == "not_written"
            assert [source.id for source in market.sources] == ["custom"]
            listed = await client.get("/api/plugin-sources", headers=headers)
            assert listed.status_code == 200, listed.text
            assert [source["id"] for source in listed.json()] == ["custom"]
            removed = await client.delete("/api/plugin-sources/custom", headers=headers)
            assert removed.status_code == 200, removed.text
            assert removed.json()["audit"] == "not_written"
            assert market.sources == []
    finally:
        await panel.stop()


@pytest.mark.asyncio
async def _probe_uninstall_audit_failure(tmp_path, monkeypatch):
    data = tmp_path / "data"
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    _package(data / "plugins" / "demo")
    await save_source(data, "demo", SOURCE_RECORD)
    system = PluginSystem(str(builtin), str(data))
    await system.load_plugins()
    panel = _panel(data, market=None, plugins=system)

    async def broken_write(**kwargs):
        raise OSError("fixture audit storage failure")

    monkeypatch.setattr(panel._audit, "write", broken_write)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=panel._app), base_url="http://offline.test",
        ) as client:
            headers = await _login(client)
            removed = await client.delete("/api/plugins/demo", headers=headers)
        assert removed.status_code == 200, removed.text
        payload = removed.json()
        assert payload["audit"] == "not_written"
        assert Path(payload["recycle"]).is_dir()
        assert not (data / "plugins" / "demo").exists()
        assert system._market_meta("demo") is None
    finally:
        await system.shutdown_plugins()
        await panel.stop()


_MISSING = object()


class _MP:
    """最小 monkeypatch 替身: legacy runner 只支持无参或仅 tmp_path 的入口。"""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value=None):  # noqa: A003 - 对齐 pytest 接口
        self._undo.append((target, name, vars(target).get(name, _MISSING)))
        setattr(target, name, value)

    def undo(self):
        while self._undo:
            target, name, old = self._undo.pop()
            if old is _MISSING:
                try:
                    delattr(target, name)
                except AttributeError:
                    pass
            else:
                setattr(target, name, old)


def test_source_audit_failure_keeps_business_result(tmp_path):
    mp = _MP()
    try:
        asyncio.run(_probe_source_audit_failure(tmp_path, mp))
    finally:
        mp.undo()


def test_uninstall_audit_failure_keeps_business_result(tmp_path):
    mp = _MP()
    try:
        asyncio.run(_probe_uninstall_audit_failure(tmp_path, mp))
    finally:
        mp.undo()
