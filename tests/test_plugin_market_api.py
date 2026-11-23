from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from mohobot.interceptors.plugin_system import PluginNamespaceConflict
from mohobot.web_panel.app import WebPanel

_MISSING = object()


@contextmanager
def _patched_attrs(target, **attrs):
    """临时替换属性, 结束后恢复(兼容 tests/_run_all.py 无 fixture 环境)。"""
    old = {}
    for name in attrs:
        namespace = getattr(target, "__dict__", {})
        old[name] = (namespace.get(name, _MISSING), getattr(target, name))
    try:
        for name, value in attrs.items():
            setattr(target, name, value)
        yield target
    finally:
        for name, (previous, value) in old.items():
            if previous is _MISSING:
                delattr(target, name)
            else:
                setattr(target, name, value)


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


def test_toggle_unknown_plugin_returns_404(tmp_path: Path) -> None:
    client, headers = _client(tmp_path)
    response = client.post(
        "/api/plugins/toggle",
        headers=headers,
        json={"name": "missing", "enabled": False},
    )
    assert response.status_code == 404


def test_plugin_namespace_conflict_returns_409(tmp_path):
    async def conflict(*args):
        raise PluginNamespaceConflict("fixture namespace conflict")

    cases = (
        ("reload", "reload_plugins", None),
        ("toggle", "set_enabled", {"name": "demo", "enabled": True}),
    )
    for endpoint, method, payload in cases:
        case_dir = tmp_path / endpoint
        case_dir.mkdir()
        with _patched_attrs(_FakePluginSystem, **{method: conflict}):
            client, headers = _client(case_dir)
            kwargs = {"json": payload} if payload is not None else {}
            response = client.post(f"/api/plugins/{endpoint}", headers=headers, **kwargs)
            assert response.status_code == 409
            assert response.json()["detail"] == "fixture namespace conflict"


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
