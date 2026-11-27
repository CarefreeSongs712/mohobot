from __future__ import annotations

import asyncio
import json
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import httpx

from _test_helpers import patched_attrs as _patched_attrs
from fastapi.testclient import TestClient

from mohobot.interceptors.plugin_system import PluginNamespaceConflict, PluginSystem
from mohobot.plugin_market.service import MarketService
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
            source_id="official",
            source_generation="official-v1",
        )

    async def ensure_indexes(self) -> None:
        return None

    async def refresh(self) -> None:
        return None

    def source_status(self, source_id):
        return {"status": "fresh", "last_success_at": 123.0}

    async def reload_sources(self):
        self.reload_count += 1
        return self.sources

    def entries(self):
        return [self._entry]

    async def install(
        self, plugin_id: str, *, source_id: str = "official", source_generation=None,
        plugin_system=None,
    ):
        return "demo", Path("demo"), {"activation": "active"}

    async def update(
        self, plugin_id: str, *, source_id: str = "official", before_replace=None,
        source_generation=None, plugin_system=None,
    ):
        if before_replace is not None:
            await before_replace("demo")
        return "demo", Path("demo"), {"activation": "active"}


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
        async def install(self, plugin_id, *, source_id, source_generation=None, plugin_system=None):
            from mohobot.plugins.manifest import parse_manifest

            assert plugin_system is not None
            candidate = data / "plugins" / ".market-prepare-fixture" / name
            candidate.mkdir(parents=True)
            (candidate / "metadata.yaml").write_text(dedent(f"""\
                name: {name}
                author: TestFixture
                display_name: Offline market install fixture
                version: 1.0.0
                description: Check startup result
                repo: https://github.com/TestFixture/offline-install
                license: MIT
                api_version: 1
            """), encoding="utf-8")
            (candidate / "main.py").write_text(dedent(f"""\
                class Plugin:
                    async def on_startup(self):
                        if {fails!r}:
                            raise RuntimeError("fixture startup rejected")
            """), encoding="utf-8")
            manifest = parse_manifest(candidate / "metadata.yaml")
            record = {
                "schema_version": 1,
                "install_method": "market",
                "registry_id": "official",
                "registry_url": "https://example.com/index.json",
                "registry_generation": "official-v1",
                "plugin_id": manifest.plugin_id,
                "repo": manifest.repo,
                "download_url": "https://example.com/plugin.zip",
                "sha256": "a" * 64,
                "version": manifest.version,
                "installed_at": 123.0,
            }

            async def recheck():
                return None

            result = await plugin_system.replace_market_plugin(
                manifest, candidate, record, expect_installed=False, recheck=recheck,
            )
            return name, destination, result

    plugins = PluginSystem(str(builtin), str(data))
    panel = WebPanel(
        password_hash=WebPanel._hash_password("test-password"),
        data_dir=str(data),
        plugin_system=plugins,
        plugin_market=Market(),
    )
    try:
        await plugins.load_plugins()
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


def _probe_source_listing_does_not_prevent_market_first_fetch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("mohobot.plugin_market.sources.socket.getaddrinfo", lambda *args: [])
    calls = []

    def fetch(url):
        calls.append(url)
        name = "official_demo" if "raw.githubusercontent.com" in url else "demo"
        return {"$meta": {"schema_version": 1, "name": "fixture"}, "plugins": {f"TestFixture/{name}": {
            "author": "TestFixture", "name": name, "description": "offline fixture", "version": "1.0.0",
            "repo": "https://github.com/TestFixture/offline-market", "sha256": "a" * 64, "api_version": 1,
        }}}

    market = MarketService(tmp_path, tmp_path / "plugins", fetcher=fetch)
    panel = WebPanel(
        password_hash=WebPanel._hash_password("test-password"),
        data_dir=str(tmp_path),
        plugin_system=_FakePluginSystem(),
        plugin_market=market,
    )
    with TestClient(panel._app) as client:
        token = client.post(
            "/api/login", json={"username": "admin", "password": "test-password"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        source = {"id": "custom", "name": "自定义", "url": "https://example.com/index.json"}
        assert client.post("/api/plugin-sources", headers=headers, json=source).status_code == 200
        assert client.get("/api/plugin-sources", headers=headers).json()[1]["status"] == "unattempted"
        assert not calls
        listing = client.get("/api/plugins/market", headers=headers)
        assert listing.status_code == 200
        assert len(calls) == 2
        assert all(item["status"] == "fresh" and item["last_success_at"] for item in listing.json()["sources"])
        generation = next(item["source_generation"] for item in listing.json()["plugins"] if item["source_id"] == "custom")
        source["url"] += "?changed=1"
        assert client.post("/api/plugin-sources", headers=headers, json=source).status_code == 200
        result = client.post("/api/plugins/market/install", headers=headers, json={
            "plugin_id": "TestFixture/demo", "source_id": "custom", "source_generation": generation,
        })
        assert result.status_code == 409
        assert not (tmp_path / "plugins" / "demo").exists()


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


def test_source_listing_does_not_prevent_market_first_fetch(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    mp = _MP()
    try:
        _probe_source_listing_does_not_prevent_market_first_fetch(case_dir, mp)
    finally:
        mp.undo()
