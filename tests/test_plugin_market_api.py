from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

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

    async def update(self, plugin_id: str, *, source_id: str = "official"):
        return "demo", Path("demo")


class _FakePluginSystem:
    installed_dir = Path("plugins")

    async def reload_plugins(self) -> int:
        return 1

    async def reload_plugin(self, name: str) -> bool:
        return name == "demo"

    def list_plugins(self):
        return []


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


def test_market_update_endpoint(tmp_path: Path) -> None:
    client, headers = _client(tmp_path)
    response = client.post(
        "/api/plugins/market/update",
        headers=headers,
        json={"plugin_id": "CarefreeSongs712/demo"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "demo"


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
