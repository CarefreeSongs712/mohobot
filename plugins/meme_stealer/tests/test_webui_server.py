"""独立端口 WebUI：登录、会话、Quart 兼容请求层与静态资源。"""

import asyncio
import socket
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from meme_stealer_core.web.http import jsonify, request  # noqa: E402
from meme_stealer_core.web.server import SESSION_COOKIE, WebUIServer  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PASSWORD = "s3cret-pass"


class FakeAPI:
    """路由处理器沿用 Quart 写法：全局 request + jsonify + (响应, 状态码)。"""

    def __init__(self):
        self.calls = []

    async def handle_echo(self):
        payload = await request.get_json() or {}
        return jsonify(
            {
                "method": request.method,
                "q": request.args.get("q", ""),
                "n": request.args.get("n", 0, type=int),
                "payload": payload,
            }
        )

    async def handle_upload(self):
        files = await request.files
        form = await request.form
        upload = files.get("file")
        return jsonify(
            {
                "name": upload.filename,
                "size": len(upload.read()),
                "category": form.get("category", ""),
            }
        )

    async def handle_missing(self):
        return jsonify({"success": False, "error": "nope"}), 404

    async def handle_boom(self):
        raise RuntimeError("kaboom")

    def iter_routes(self):
        yield "/echo", self.handle_echo, ("GET", "POST")
        yield "/upload", self.handle_upload, ("POST",)
        yield "/missing", self.handle_missing, ("GET",)
        yield "/boom", self.handle_boom, ("GET",)


def _server(password: str = PASSWORD) -> WebUIServer:
    return WebUIServer(
        FakeAPI(),
        pages_dir=PLUGIN_DIR / "pages" / "dashboard",
        i18n_dir=PLUGIN_DIR / "i18n",
        password_getter=lambda: password,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("meme_stealer_core.web.server.LOGIN_DELAY_SECONDS", 0)
    with TestClient(_server().app, base_url="http://testserver") as test_client:
        yield test_client


def _login(client, password=PASSWORD):
    return client.post("/login", json={"password": password})


def test_pages_require_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "./login"
    assert client.get("/app.js").status_code == 401
    assert client.get("/api/echo").status_code == 401
    # 登录页本身和 logo 可匿名访问
    assert "WebUI 密码" in client.get("/login").text
    assert client.get("/logo.png").status_code == 200


def test_wrong_password_is_rejected(client):
    resp = _login(client, "wrong")
    assert resp.status_code == 401
    assert SESSION_COOKIE not in resp.cookies
    assert client.get("/api/echo").status_code == 401


def test_login_sets_httponly_cookie_and_unlocks_dashboard(client):
    resp = _login(client)
    assert resp.status_code == 200 and resp.json() == {"success": True}
    set_cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie

    index = client.get("/")
    assert index.status_code == 200
    assert '<script src="./bridge.js"></script>' in index.text
    assert "__STEALER_I18N__" in index.text
    assert '"zh-CN"' in index.text
    assert client.get("/bridge.js").text.count("AstrBotPluginPage") == 1
    assert client.get("/app.js").status_code == 200
    assert client.get("/vendor/vue.global.prod.js").status_code == 200


def test_static_files_cannot_escape_dashboard_dir(client):
    _login(client)
    for path in ("/..%2Fmain.py", "/.hidden", "/vendor/..%2F..%2Fmain.py", "/nope.js"):
        assert client.get(path).status_code == 404, path


def test_api_requests_use_quart_style_handlers(client):
    _login(client)
    got = client.get("/api/echo", params={"q": "猫", "n": "7"}).json()
    assert got == {"method": "GET", "q": "猫", "n": 7, "payload": {}}

    posted = client.post("/api/echo", json={"a": 1}).json()
    assert posted["method"] == "POST" and posted["payload"] == {"a": 1}

    upload = client.post(
        "/api/upload", files={"file": ("a.png", b"12345", "image/png")}, data={"category": "happy"}
    ).json()
    assert upload == {"name": "a.png", "size": 5, "category": "happy"}

    missing = client.get("/api/missing")
    assert missing.status_code == 404 and missing.json()["error"] == "nope"
    boom = client.get("/api/boom")
    assert boom.status_code == 500 and "kaboom" in boom.json()["error"]
    assert client.get("/api/unknown").status_code == 404
    assert client.post("/api/missing").status_code == 405


def test_cross_origin_posts_are_refused(client):
    _login(client)
    resp = client.post("/api/echo", json={}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    ok = client.post("/api/echo", json={}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


def test_logout_and_password_change_revoke_sessions(monkeypatch):
    monkeypatch.setattr("meme_stealer_core.web.server.LOGIN_DELAY_SECONDS", 0)
    server = _server()
    with TestClient(server.app) as client:
        _login(client)
        assert client.get("/api/echo").status_code == 200
        server.revoke_all_sessions()
        assert client.get("/api/echo").status_code == 401
        _login(client)
        client.post("/logout")
        assert client.get("/api/echo").status_code == 401


def test_empty_password_never_authenticates(monkeypatch):
    monkeypatch.setattr("meme_stealer_core.web.server.LOGIN_DELAY_SECONDS", 0)
    with TestClient(_server(password="").app) as client:
        assert _login(client, "").status_code == 401


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_start_stop_releases_port_and_port_conflict_is_contained():
    pytest.importorskip("uvicorn")
    port = _free_port()
    server = _server()
    assert await server.start("127.0.0.1", port)
    assert server.running

    # 同端口第二个实例：uvicorn 内部 sys.exit()，不能冒泡结束进程
    other = _server()
    assert await other.start("127.0.0.1", port) is False
    assert not other.running

    await server.stop()
    assert not server.running
    # 端口已释放，可以重新绑定（插件热重载场景）
    again = _server()
    assert await again.start("127.0.0.1", port)
    await again.stop()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_start_refuses_without_password():
    server = _server(password="")
    assert await server.start("127.0.0.1", _free_port()) is False
