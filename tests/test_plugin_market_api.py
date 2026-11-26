from __future__ import annotations

import asyncio
import json
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from mohobot.interceptors.plugin_system import PluginNamespaceConflict, PluginSystem
from mohobot.web_panel.app import WebPanel


class _FakeMarket:
    def __init__(self) -> None:
        self.sources = [
            SimpleNamespace(id="official", name="官方", url="https://example.com/index.json", official=True)
        ]
        self.reload_count = 0
        self._entry = SimpleNamespace(
            plugin_id="CarefreeSongs712/demo",
            author="CarefreeSongs712",
            name="demo",
            display_name="演示",
            version="1.0.0",
            description="demo",
            repo="https://github.com/CarefreeSongs712/mohobot-plugin-demo",
            sha256="a" * 64,
            api_version=1,
            license="MIT",
            tags=("demo",),
        )

    async def refresh(self) -> None:
        return None

    async def reload_sources(self):
        self.reload_count += 1
        return self.sources

    def entries(self):
        return [self._entry]

    async def install(self, plugin_id: str, *, source_id: str = "official"):
        return "demo", Path("demo")

    async def update(self, plugin_id: str, *, source_id: str = "official", before_replace=None):
        if before_replace is not None:
            await before_replace("demo")
        return "demo", Path("demo")


class _FakePluginSystem:
    installed_dir = Path("plugins")

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        return name == "demo"

    async def unload_plugin(self, name: str) -> bool:
        return True

    async def reload_plugins(self) -> int:
        return 1

    async def reload_plugin(self, name: str) -> bool:
        return name == "demo"

    def list_plugins(self):
        return [{"name": "demo", "source": "installed", "enabled": True, "loaded": True, "error": None}]


def _client(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    panel = WebPanel(
        password_hash=WebPanel._hash_password("test-password"),
        data_dir=str(tmp_path),
        plugin_system=_FakePluginSystem(),
        plugin_market=_FakeMarket(),
    )
    client = TestClient(panel._app)
    response = client.post("/api/login", json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200
    return client, {"Authorization": f"Bearer {response.json()['token']}"}


def test_toggle_unknown_plugin_returns_404(tmp_path: Path) -> None:
    client, headers = _client(tmp_path)
    response = client.post(
        "/api/plugins/toggle",
        headers=headers,
        json={"name": "missing", "enabled": False},
    )
    assert response.status_code == 404


def test_plugin_namespace_conflict_returns_409(tmp_path: Path) -> None:
    """两种入口的命名空间冲突都映射为 409。

    不用 parametrize/monkeypatch: legacy runner 只支持无参或仅 ``tmp_path``
    的用例入口, 而 CI 在 push main 时仍会执行该 runner。
    """
    cases = [
        ("reload", "reload_plugins", None),
        ("toggle", "set_enabled", {"name": "demo", "enabled": True}),
    ]
    for endpoint, method, payload in cases:
        original = getattr(_FakePluginSystem, method)

        async def conflict(*args, **kwargs):
            raise PluginNamespaceConflict("fixture namespace conflict")

        setattr(_FakePluginSystem, method, conflict)
        try:
            case_dir = tmp_path / endpoint
            case_dir.mkdir()
            client, headers = _client(case_dir)
            kwargs = {"json": payload} if payload is not None else {}
            response = client.post(f"/api/plugins/{endpoint}", headers=headers, **kwargs)
            assert response.status_code == 409
            assert response.json()["detail"] == "fixture namespace conflict"
        finally:
            setattr(_FakePluginSystem, method, original)


def test_market_endpoint_requires_auth(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get("/api/plugins/market").status_code == 401


def test_market_endpoint_returns_entries(tmp_path: Path) -> None:
    client, headers = _client(tmp_path)
    response = client.get("/api/plugins/market", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["plugins"][0]["plugin_id"] == "CarefreeSongs712/demo"
    assert payload["sources"][0]["id"] == "official"


def test_market_install_endpoint(tmp_path: Path) -> None:
    client, headers = _client(tmp_path)
    response = client.post(
        "/api/plugins/market/install",
        headers=headers,
        json={"plugin_id": "CarefreeSongs712/demo"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "demo"


async def _check_market_install_activation(tmp_path: Path, enabled: bool, fails: bool) -> None:
    data = tmp_path / "data"
    data.mkdir()
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    # The unrelated plugin stays loaded even when the newly installed one fails.
    (builtin / "cold_install_peer.py").write_text("class Plugin: pass\n", encoding="utf-8")
    name = "cold_install_target"
    destination = data / "plugins" / name
    state = json.dumps({name: {"enabled": False}})
    if not enabled:
        (data / "plugins_state.json").write_text(state, encoding="utf-8")

    class Market(_FakeMarket):
        async def install(self, plugin_id, *, source_id):
            destination.mkdir(parents=True)
            (destination / "metadata.yaml").write_text(dedent(f"""\
                name: {name}
                author: TestFixture
                display_name: Offline market install fixture
                version: 1.0.0
                description: Check startup result
                repo: https://github.com/TestFixture/offline-install
                license: MIT
                api_version: 1
            """), encoding="utf-8")
            (destination / "main.py").write_text(dedent(f"""\
                class Plugin:
                    async def on_startup(self):
                        if {fails!r}:
                            raise RuntimeError("fixture startup rejected")
            """), encoding="utf-8")
            return name, destination

    plugins = PluginSystem(str(builtin), str(data))
    panel = WebPanel(
        password_hash=WebPanel._hash_password("test-password"),
        data_dir=str(data),
        plugin_system=plugins,
        plugin_market=Market(),
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel._app),
                                     base_url="http://test") as client:
            login = await client.post("/api/login", json={"username": "admin", "password": "test-password"})
            assert login.status_code == 200
            response = await client.post(
                "/api/plugins/market/install",
                headers={"Authorization": f"Bearer {login.json()['token']}"},
                json={"plugin_id": "TestFixture/cold_install_target"},
            )
        items = {item["name"]: item for item in plugins.list_plugins()}
        assert items["cold_install_peer"]["loaded"] is True
        assert items[name]["enabled"] is enabled
        # loaded 语义随生命周期修复变化（#149/#130）：失败候选可能保持实例但未就绪。
        # 这里只断言两种实现都成立的可观察结果。
        if enabled and fails:
            assert items[name]["error"] == "fixture startup rejected"
        else:
            assert items[name]["loaded"] is enabled
            assert items[name]["error"] is None
        assert destination.is_dir()
        if enabled and fails:
            assert items[name]["error"] == "fixture startup rejected"
            assert response.status_code == 400, response.text
            assert "fixture startup rejected" in response.json()["detail"]
            assert "已安装" in response.json()["detail"]
        else:
            assert items[name]["error"] is None
            assert response.status_code == 200, response.text
            assert response.json()["activation"] == ("active" if enabled else "disabled")
            if not enabled:
                assert (data / "plugins_state.json").read_text(encoding="utf-8") == state
    finally:
        await plugins.shutdown_plugins()
        await panel.stop()


def test_market_install_reports_target_activation(tmp_path: Path) -> None:
    """三种安装场景各自独立目录执行。

    不用 parametrize: legacy runner 只支持无参或仅 ``tmp_path`` 的用例入口,
    而 CI 在 push main 时仍会执行该 runner。
    """
    for enabled, fails in ((True, True), (False, True), (True, False)):
        case_dir = tmp_path / f"enabled-{int(enabled)}-fails-{int(fails)}"
        case_dir.mkdir()
        asyncio.run(_check_market_install_activation(case_dir, enabled, fails))


def test_market_update_endpoint(tmp_path: Path) -> None:
    client, headers = _client(tmp_path)
    response = client.post(
        "/api/plugins/market/update",
        headers=headers,
        json={"plugin_id": "CarefreeSongs712/demo"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "demo"


def test_add_source_refreshes_market_sources(tmp_path: Path) -> None:
    market = _FakeMarket()
    panel = WebPanel(
        password_hash=WebPanel._hash_password("test-password"),
        data_dir=str(tmp_path),
        plugin_system=_FakePluginSystem(),
        plugin_market=market,
    )
    client = TestClient(panel._app)
    token = client.post(
        "/api/login", json={"username": "admin", "password": "test-password"}
    ).json()["token"]
    response = client.post(
        "/api/plugin-sources",
        headers={"Authorization": f"Bearer {token}"},
        json={"id": "custom", "name": "自定义", "url": "https://example.com/index.json"},
    )
    assert response.status_code == 200
    assert market.reload_count == 1


def test_source_list_reloads_from_disk(tmp_path: Path) -> None:
    market = _FakeMarket()
    panel = WebPanel(
        password_hash=WebPanel._hash_password("test-password"),
        data_dir=str(tmp_path),
        plugin_system=_FakePluginSystem(),
        plugin_market=market,
    )
    client = TestClient(panel._app)
    token = client.post(
        "/api/login", json={"username": "admin", "password": "test-password"}
    ).json()["token"]
    response = client.get(
        "/api/plugin-sources", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert market.reload_count == 1
