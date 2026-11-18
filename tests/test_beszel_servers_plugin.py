import asyncio
import base64
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from mohobot.models.onebot import PrivateMessageEvent, Sender

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "tests_beszel_servers_plugin", _ROOT / "plugins" / "beszel_servers" / "main.py"
)
plugin_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(plugin_mod)


def _record(name, status, info=None, updated="2026-11-16 15:36:33.374Z"):
    return {
        "id": f"rec_{name}",
        "name": name,
        "status": status,
        "host": "10.0.0.1",
        "port": 45876,
        "updated": updated,
        "info": info or {},
    }


def _private_event(text: str) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=0, self_id=1, post_type="message",
        message_type="private", sub_type="friend",
        message_id=42, user_id=1001,
        message=[{"type": "text", "data": {"text": text}}],
        raw_message=text, font=0,
        sender=Sender(user_id=1001, nickname="测试"),
    )


def test_global_triggers():
    assert plugin_mod.Plugin.global_triggers == {"/servers", "/服务器"}


def test_build_view_sorts_and_trims():
    systems = [
        _record("C", "up", {"cpu": 10, "mp": 20, "dp": 30, "la": [0.1, 0.2, 0.3], "u": 3600, "v": "0.21.1"}),
        _record("A", "up", {"cpu": 40, "mp": 50, "dp": 60, "la": [1.0, 0.9, 0.8], "u": 7200, "v": "0.21.1"}),
        _record("B", "down", {}),
    ]
    view = plugin_mod.build_view(systems, 2)
    assert [row["name"] for row in view["rows"]] == ["A", "B"]
    assert view["total"] == 3
    assert view["hidden"] == 1
    assert view["rows"][0]["cpu"] == 40
    assert view["rows"][0]["status_label"] == "运行中"
    assert view["rows"][1]["status_label"] == "离线"
    assert view["rows"][1]["cpu"] is None
    assert view["updated_text"] == "11-16 23:36"


def test_parse_updated_utc8():
    parsed = plugin_mod._parse_updated("2026-11-16 15:36:33.374Z")
    assert parsed is not None
    assert parsed.strftime("%Y-%m-%d %H:%M:%S") == "2026-11-16 23:36:33"
    assert plugin_mod._parse_updated("") is None
    assert plugin_mod._parse_updated("not-a-date") is None


def test_build_text_reports():
    systems = [
        _record("A", "up", {"cpu": 42, "mp": 55, "dp": 61, "la": [0.5, 0.4, 0.3], "u": 90061, "v": "0.21.2"}),
        _record("B", "down", {}),
        _record("C", "paused", {"cpu": 1}),
    ]
    text = plugin_mod.build_text(plugin_mod.build_view(systems, 2))
    assert "服务器状态（Beszel · 3 台）" in text
    assert "A  运行中  CPU 42%" in text
    assert "B  离线" in text
    assert "另有 1 台未显示" in text
    assert "数据更新 11-16 23:36（UTC+8）" in text


def test_render_card():
    systems = [
        _record("host-a", "up", {"cpu": 42, "mp": 55, "dp": 61, "la": [0.5, 0.4, 0.3], "u": 90061, "v": "0.21.2"}),
        _record("host-b", "down", {}, updated="2026-11-16 14:00:00.000Z"),
    ]
    path = plugin_mod.render_card(plugin_mod.build_view(systems, 12))
    from mohobot.utils.image_card import find_cjk_font

    if path is None:
        assert find_cjk_font() is None
        return
    try:
        assert os.path.getsize(path) > 1000
        with open(path, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"
    finally:
        os.remove(path)


async def test_on_message_requires_config():
    plugin = plugin_mod.Plugin()
    handled, reply = await plugin.on_message("bot_001", _private_event("/servers"), {})
    assert handled is True
    assert "未配置" in reply
    handled, reply = await plugin.on_message("bot_001", _private_event("/服务器"), {})
    assert handled is True
    assert "未配置" in reply


async def test_on_message_ignores_other_text():
    plugin = plugin_mod.Plugin()
    handled, reply = await plugin.on_message("bot_001", _private_event("看看服务器"), {})
    assert handled is False
    assert reply is None



def _jwt(label: str, exp: float | None = None) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp if exp is not None else time.time() + 3600, "sub": label}).encode()
    ).decode().rstrip("=")
    return "e30." + payload + ".sig"


def _configure(plugin, **overrides):
    config = {
        "base_url": "https://hub.test",
        "email": "admin@example.test",
        "password": "test-password",
        "timeout_secs": 8,
        "max_systems": 12,
    }
    config.update(overrides)
    plugin.on_config_update(config)


async def _with_mock_client(plugin, handler, base_url="https://hub.test"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    key = (base_url, 8.0)
    plugin._clients[key] = client
    plugin._client_refs[key] = 0
    plugin._active_client_key = key
    return client


def test_snapshot_isolated_from_mutable_config():
    plugin = plugin_mod.Plugin()
    _configure(plugin)
    first = plugin._snapshot()
    plugin.on_config_update({"base_url": "https://new.test", "email": "new@example.test",
                             "password": "new-password", "timeout_secs": 8, "max_systems": 12})
    second = plugin._snapshot()
    assert first.base_url == "https://hub.test" and first.email == "admin@example.test"
    assert first.generation != second.generation
    assert second.base_url == "https://new.test" and second.email == "new@example.test"


async def test_login_and_token_cache():
    calls = []
    token = _jwt("cached")

    def handler(request):
        calls.append((request.method, request.headers.get("Authorization")))
        if request.method == "POST":
            return httpx.Response(200, json={"token": token})
        return httpx.Response(200, json={"items": [{"name": "host", "status": "up"}]})

    async def scenario():
        plugin = plugin_mod.Plugin()
        _configure(plugin)
        client = await _with_mock_client(plugin, handler)
        try:
            assert (await plugin._fetch_systems())[0]["name"] == "host"
            await plugin._fetch_systems()
        finally:
            await client.aclose()
        assert [c[0] for c in calls] == ["POST", "GET", "GET"]
        assert calls[1][1] == calls[2][1] == token

    await scenario()


async def test_401_relogin_retries_once_and_updates_token():
    calls = []
    tokens = iter([_jwt("old"), _jwt("new")])

    def handler(request):
        calls.append((request.method, request.headers.get("Authorization")))
        if request.method == "POST":
            return httpx.Response(200, json={"token": next(tokens)})
        if len([c for c in calls if c[0] == "GET"]) == 1:
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json={"items": [{"name": "host", "status": "up"}]})

    async def scenario():
        plugin = plugin_mod.Plugin()
        _configure(plugin)
        client = await _with_mock_client(plugin, handler)
        try:
            systems = await plugin._fetch_systems()
            assert systems[0]["name"] == "host"
        finally:
            await client.aclose()
        assert [c[0] for c in calls] == ["POST", "GET", "POST", "GET"]
        assert calls[2][1] != calls[1][1]
        assert plugin._token_generation == plugin._config_generation

    await scenario()


async def test_config_change_while_login_in_flight_never_crosses_hub():
    started = asyncio.Event()
    release = asyncio.Event()
    requests = []

    class OldHub(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requests.append(("old", request.method, request.headers.get("Authorization")))
            if request.method == "POST":
                started.set()
                await release.wait()
                return httpx.Response(200, json={"token": _jwt("old")})
            return httpx.Response(200, json={"items": []})

    class NewHub(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requests.append(("new", request.method, request.headers.get("Authorization")))
            if request.method == "POST":
                return httpx.Response(200, json={"token": _jwt("new")})
            return httpx.Response(200, json={"items": []})

    async def scenario():
        plugin = plugin_mod.Plugin()
        _configure(plugin)
        old_client = httpx.AsyncClient(transport=OldHub())
        plugin._clients[("https://hub.test", 8.0)] = old_client
        task = asyncio.create_task(plugin._fetch_systems())
        await started.wait()
        plugin.on_config_update({
            "base_url": "https://new.test", "email": "new@example.test",
            "password": "new-password", "timeout_secs": 8, "max_systems": 12,
        })
        release.set()
        new_client = None
        try:
            await asyncio.wait_for(task, timeout=5)  # old snapshot may finish on old hub
            # The old generation's login result must never be published.
            assert plugin._token == "" and plugin._token_generation == -1
            # New client is created on demand, like production: the old
            # snapshot's completion must not affect the new hub's client.
            new_client = httpx.AsyncClient(transport=NewHub())
            plugin._clients[("https://new.test", 8.0)] = new_client
            plugin._client_refs[("https://new.test", 8.0)] = 0
            await plugin._fetch_systems()            # but next request must be new-hub + new token
        finally:
            await old_client.aclose()
            if new_client is not None:
                await new_client.aclose()
        new_gets = [auth for hub, method, auth in requests if hub == "new" and method == "GET"]
        assert new_gets, requests
        old_token = "e30." + _jwt("old").split(".")[1] + ".sig"
        assert all(auth and auth != old_token for auth in new_gets), requests
        assert plugin._token_generation == plugin._config_generation

    await scenario()


async def test_403_retries_once():
    count = {"get": 0}

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"token": _jwt("token")})
        count["get"] += 1
        if count["get"] == 1:
            return httpx.Response(403, json={"message": "forbidden"})
        return httpx.Response(200, json={"items": [{"name": "host", "status": "up"}]})

    async def scenario():
        plugin = plugin_mod.Plugin()
        _configure(plugin)
        client = await _with_mock_client(plugin, handler)
        try:
            assert (await plugin._fetch_systems())[0]["name"] == "host"
        finally:
            await client.aclose()

    await scenario()


async def test_auth_failure_is_friendly():
    def handler(request):
        return httpx.Response(400, json={"message": "Failed to authenticate."})

    async def scenario():
        plugin = plugin_mod.Plugin()
        _configure(plugin)
        client = await _with_mock_client(plugin, handler)
        try:
            try:
                await plugin._fetch_systems()
            except plugin_mod.BeszelAuthError as exc:
                assert plugin_mod._friendly_error(exc) == "登录失败（邮箱或密码错误）"
            else:
                raise AssertionError("expected BeszelAuthError")
        finally:
            await client.aclose()

    await scenario()


async def test_timeout_is_friendly():
    def handler(request):
        raise httpx.ReadTimeout("test timeout", request=request)

    async def scenario():
        plugin = plugin_mod.Plugin()
        _configure(plugin)
        client = await _with_mock_client(plugin, handler)
        try:
            try:
                await plugin._fetch_systems()
            except httpx.ReadTimeout as exc:
                assert plugin_mod._friendly_error(exc) == "连接超时"
            else:
                raise AssertionError("expected timeout")
        finally:
            await client.aclose()

    await scenario()


async def test_on_shutdown_closes_clients():
    async def scenario():
        plugin = plugin_mod.Plugin()
        client = httpx.AsyncClient(trust_env=False)
        plugin._clients[("https://hub.test", 8.0)] = client
        await plugin.on_shutdown()
        assert plugin._clients == {}
        assert client.is_closed

    await scenario()


def test_bad_jwt_does_not_crash():
    assert plugin_mod._jwt_exp("not-a-jwt") == 0.0
    assert plugin_mod._jwt_exp("a.b.c") == 0.0



def test_base_url_validation():
    plugin = plugin_mod.Plugin()
    for bad in (
        "ftp://hub.test",
        "https://user:pass@hub.test",
        "https://hub.test/?x=1",
        "https://hub.test/#frag",
    ):
        _configure(plugin, base_url=bad)
        try:
            plugin._snapshot()
        except plugin_mod.BeszelConfigError:
            pass
        else:
            raise AssertionError(f"expected rejected base_url: {bad}")
    _configure(plugin, base_url="https://hub.test/base/")
    assert plugin._snapshot().base_url == "https://hub.test/base"


async def test_client_pool_retires_idle_after_config_switch():
    def handler(items):
        def _handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"token": _jwt("token")})
            return httpx.Response(200, json={"items": items})
        return _handler

    async def scenario():
        plugin = plugin_mod.Plugin()
        _configure(plugin, base_url="https://old.test")
        old = httpx.AsyncClient(transport=httpx.MockTransport(handler([{"name": "old", "status": "up"}])), trust_env=False)
        old_key = ("https://old.test", 8.0)
        plugin._clients[old_key] = old
        plugin._client_refs[old_key] = 0
        plugin._active_client_key = old_key
        # First request through the production path keeps old.test as active.
        assert (await plugin._fetch_systems())[0]["name"] == "old"
        # Switch config, then a new real fetch must retire the old client.
        _configure(plugin, base_url="https://new.test")
        new = httpx.AsyncClient(transport=httpx.MockTransport(handler([{"name": "new", "status": "up"}])), trust_env=False)
        new_key = ("https://new.test", 8.0)
        plugin._clients[new_key] = new
        plugin._client_refs[new_key] = 0
        try:
            assert (await plugin._fetch_systems())[0]["name"] == "new"
            # Retirement must happen on the production release path, before
            # shutdown, not only when the plugin is torn down.
            assert old.is_closed, "old client should be retired by release"
            assert old_key not in plugin._clients
            assert new_key in plugin._clients
        finally:
            await plugin.on_shutdown()
        assert new.is_closed
        assert plugin._clients == {}

    await scenario()


async def test_shutdown_blocks_new_clients():
    plugin = plugin_mod.Plugin()
    _configure(plugin)
    await plugin.on_shutdown()
    try:
        await plugin._acquire_client(plugin._snapshot())
    except plugin_mod.BeszelUnavailableError:
        pass
    else:
        raise AssertionError("expected BeszelUnavailableError after shutdown")


async def test_concurrent_401_refresh_single_flight():
    state = {"post": 0}
    old_token = _jwt("old")
    new_token = _jwt("new")

    class Handler(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.method == "POST":
                state["post"] += 1
                token = old_token if state["post"] == 1 else new_token
                return httpx.Response(200, json={"token": token})
            if request.headers.get("Authorization") == old_token:
                return httpx.Response(401, json={"message": "expired"})
            return httpx.Response(200, json={"items": [{"name": "host", "status": "up"}]})

    async def scenario():
        plugin = plugin_mod.Plugin()
        _configure(plugin)
        client = httpx.AsyncClient(transport=Handler(), trust_env=False)
        key = ("https://hub.test", 8.0)
        plugin._clients[key] = client
        plugin._client_refs[key] = 0
        plugin._active_client_key = key
        try:
            results = await asyncio.gather(*(plugin._fetch_systems() for _ in range(12)))
        finally:
            await client.aclose()
        assert all(r and r[0]["name"] == "host" for r in results)
        assert state["post"] == 2, state

    await scenario()


def test_safe_error_text_redacts_userinfo():
    request = httpx.Request("GET", "https://user:pass@hub.test/api/x")
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError("403", request=request, response=response)
    text = plugin_mod._safe_error_text(error)
    assert "user" not in text and "pass" not in text
    generic = plugin_mod._safe_error_text(RuntimeError("failed https://user:pass@hub.test/x"))
    assert "pass" not in generic



def test_non_finite_metrics_are_ignored():
    systems = [
        {"name": "nan", "status": "up",
         "info": {"cpu": float("nan"), "mp": float("inf"), "dp": 12,
                  "la": [float("nan"), 0.5], "u": float("inf")}},
    ]
    row = plugin_mod.build_view(systems, 12)["rows"][0]
    assert row["cpu"] is None and row["mem"] is None
    assert row["disk"] == 12
    assert row["uptime"] is None
    assert row["load"][0] is None and row["load"][1] == 0.5


async def test_real_http_chain():
    from aiohttp import web

    old_env = {}
    for key in list(os.environ):
        if key.upper() in {"ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}:
            old_env[key] = os.environ.pop(key)

    token = _jwt("real")

    async def login(_request):
        return web.json_response({"token": token})

    async def records(request):
        if request.headers.get("Authorization") != token:
            return web.json_response({"message": "unauthorized"}, status=401)
        return web.json_response({"items": [{"name": "real-host", "status": "up",
                                            "info": {"cpu": 3, "mp": 4, "dp": 5}}]})

    app = web.Application()
    app.router.add_post("/api/collections/users/auth-with-password", login)
    app.router.add_get("/api/collections/systems/records", records)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    plugin = plugin_mod.Plugin()
    _configure(plugin, base_url=f"http://127.0.0.1:{port}")
    try:
        systems = await plugin._fetch_systems()
        assert systems and systems[0]["name"] == "real-host"
    finally:
        await plugin.on_shutdown()
        await runner.cleanup()
        os.environ.update(old_env)



def test_offline_rows_clear_metrics_but_keep_version():
    systems = [
        {"name": "down", "status": "down",
         "info": {"cpu": 42, "mp": 55, "dp": 61, "la": [1.0], "u": 7200, "v": "0.21.1"}},
    ]
    row = plugin_mod.build_view(systems, 12)["rows"][0]
    assert row["status"] == "down"
    assert row["cpu"] is None and row["mem"] is None and row["disk"] is None
    assert row["load"] == [] and row["uptime"] is None
    assert row["version"] == "0.21.1"
    text = plugin_mod.build_text(plugin_mod.build_view(systems, 12))
    assert "down  离线" in text



async def test_max_systems_is_capped_at_50():
    plugin = plugin_mod.Plugin()
    _configure(plugin, max_systems=999)
    systems = [{"name": f"host-{i:03d}", "status": "up", "info": {"cpu": 1}} for i in range(60)]

    async def fake_fetch(_snapshot=None):
        return systems

    original = plugin._fetch_systems
    plugin._fetch_systems = fake_fetch
    try:
        handled, reply = await plugin.on_message("bot_001", _private_event("/servers"), {})
    finally:
        plugin._fetch_systems = original
    assert handled is True
    assert reply is not None
    assert "另有 10 台未显示" in reply


async def test_real_http_chain_registers_client():
    from aiohttp import web

    old_env = {}
    for key in list(os.environ):
        if key.upper() in {"ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}:
            old_env[key] = os.environ.pop(key)
    token = _jwt("real-reg")
    async def login(_request):
        return web.json_response({"token": token})
    async def records(request):
        if request.headers.get("Authorization") != token:
            return web.json_response({"message": "unauthorized"}, status=401)
        return web.json_response({"items": [{"name": "real-host", "status": "up", "info": {"cpu": 1}}]})
    app = web.Application()
    app.router.add_post("/api/collections/users/auth-with-password", login)
    app.router.add_get("/api/collections/systems/records", records)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    plugin = plugin_mod.Plugin()
    _configure(plugin, base_url=f"http://127.0.0.1:{port}")
    try:
        systems = await plugin._fetch_systems()
        assert systems and systems[0]["name"] == "real-host"
        # Mutating away the client registration in _acquire_client must fail here.
        assert plugin._clients and all(not c.is_closed for c in plugin._clients.values())
    finally:
        await plugin.on_shutdown()
        await runner.cleanup()
        os.environ.update(old_env)
