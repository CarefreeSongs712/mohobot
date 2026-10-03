"""表情包管理 WebUI：独立端口托管 pages/dashboard，并把 /api/* 交给 PluginAPI。

- 前端沿用 AstrBot 插件页面的写法（``window.AstrBotPluginPage`` 桥接），
  这里注入一个同名桥接脚本，前端代码不用改。
- 登录：插件配置里的 ``webui_password``；会话令牌放在 HttpOnly +
  SameSite=Strict 的 Cookie 里，进程内存保存，插件重载后需要重新登录。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from ..host import logger
from .http import RequestAdapter, bind_request, reset_request, to_response

SESSION_COOKIE = "meme_stealer_session"
SESSION_TTL_SECONDS = 12 * 3600
LOGIN_DELAY_SECONDS = 0.5

BRIDGE_JS = r"""(function () {
'use strict';
var BASE = './api';

function buildQuery(params) {
    if (!params) return '';
    var sp = new URLSearchParams();
    Object.keys(params).forEach(function (k) {
        if (params[k] !== undefined && params[k] !== null) sp.append(k, params[k]);
    });
    var s = sp.toString();
    return s ? '?' + s : '';
}

async function parse(res) {
    if (res.status === 401) {
        location.href = './login';
        throw new Error('登录已过期');
    }
    var text = await res.text();
    try { return JSON.parse(text); } catch (e) {
        throw new Error(text || ('HTTP ' + res.status));
    }
}

async function apiGet(endpoint, params) {
    return parse(await fetch(BASE + '/' + endpoint + buildQuery(params), {
        credentials: 'same-origin'
    }));
}

async function apiPost(endpoint, payload) {
    return parse(await fetch(BASE + '/' + endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
    }));
}

async function upload(endpoint, file) {
    var fd = new FormData();
    fd.append('file', file, file.name);
    return parse(await fetch(BASE + '/' + endpoint, {
        method: 'POST', credentials: 'same-origin', body: fd
    }));
}

var darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
var listeners = [];

function getLocale() {
    try { return localStorage.getItem('meme_stealer_locale') || navigator.language || 'zh-CN'; }
    catch (e) { return navigator.language || 'zh-CN'; }
}

function getI18n() { return window.__STEALER_I18N__ || {}; }

function getContext() {
    return { locale: getLocale(), i18n: getI18n(), isDark: !!(darkQuery && darkQuery.matches) };
}

if (darkQuery && darkQuery.addEventListener) {
    darkQuery.addEventListener('change', function () {
        var ctx = getContext();
        listeners.forEach(function (cb) { try { cb(ctx); } catch (e) {} });
    });
}

window.AstrBotPluginPage = {
    apiGet: apiGet,
    apiPost: apiPost,
    upload: upload,
    getLocale: getLocale,
    setLocale: function (locale) {
        try { localStorage.setItem('meme_stealer_locale', locale); } catch (e) {}
        location.reload();
    },
    getI18n: getI18n,
    getContext: getContext,
    onContext: function (cb) { if (typeof cb === 'function') listeners.push(cb); }
};
})();
"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>表情包管理 · 登录</title>
<link rel="icon" type="image/png" href="./logo.png">
<style>
:root { color-scheme: light dark; --bg: #f4f5f7; --card: #fff; --text: #1f2328; --dim: #6b7280;
        --accent: #b8860b; --border: #d9dce1; --err: #c0392b; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #15171a; --card: #1e2125; --text: #e6e6e6; --dim: #9aa0a6;
          --accent: #d4a73a; --border: #33373d; --err: #ff7b72; }
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
       background: var(--bg); color: var(--text);
       font: 15px/1.5 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
form { width: 100%; max-width: 340px; background: var(--card); border: 1px solid var(--border);
       border-radius: 12px; padding: 28px 24px; display: grid; gap: 14px; }
.head { display: flex; align-items: center; gap: 10px; }
.head img { width: 36px; height: 36px; border-radius: 8px; }
h1 { font-size: 18px; margin: 0; }
p { margin: 0; color: var(--dim); font-size: 13px; }
input { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border);
        background: transparent; color: inherit; font: inherit; }
input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
button { padding: 10px; border: 0; border-radius: 8px; background: var(--accent); color: #fff;
         font: inherit; font-weight: 600; cursor: pointer; }
button:disabled { opacity: .6; cursor: wait; }
.err { color: var(--err); font-size: 13px; min-height: 1.2em; }
</style>
</head>
<body>
<form id="f">
  <div class="head"><img src="./logo.png" alt=""><h1>表情包管理</h1></div>
  <p>输入插件配置中的 WebUI 密码。</p>
  <input id="pw" type="password" autocomplete="current-password" placeholder="密码" required autofocus>
  <button id="btn" type="submit">登录</button>
  <div class="err" id="err" role="alert"></div>
</form>
<script>
document.getElementById('f').addEventListener('submit', async function (e) {
  e.preventDefault();
  var btn = document.getElementById('btn'), err = document.getElementById('err');
  btn.disabled = true; err.textContent = '';
  try {
    var res = await fetch('./login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: document.getElementById('pw').value })
    });
    var data = await res.json().catch(function () { return {}; });
    if (res.ok && data.success) { location.href = './'; return; }
    err.textContent = data.error || '登录失败';
  } catch (ex) { err.textContent = '网络错误：' + ex.message; }
  btn.disabled = false;
});
</script>
</body>
</html>
"""


def _digest(value: str) -> bytes:
    return hashlib.sha256(str(value or "").encode("utf-8")).digest()


class WebUIServer:
    """单个 uvicorn 实例；插件重载时由 ``stop()`` 释放端口。"""

    def __init__(
        self,
        plugin_api: Any,
        pages_dir: Path,
        i18n_dir: Path,
        password_getter: Callable[[], str],
    ) -> None:
        self._plugin_api = plugin_api
        self._pages_dir = Path(pages_dir).resolve()
        self._i18n_dir = Path(i18n_dir)
        self._password_getter = password_getter
        self._tokens: dict[str, float] = {}
        self._login_lock = asyncio.Lock()
        self._handlers: dict[str, dict[str, Callable[[], Any]]] = {}
        for route, handler, methods in plugin_api.iter_routes():
            path = route.strip("/")
            for method in methods:
                self._handlers.setdefault(path, {})[method.upper()] = handler
        self._server: Any = None
        self._serve_task: asyncio.Task | None = None
        self.address = ""
        self.app = self._build_app()

    # ── 应用 ─────────────────────────────────────────────

    def _build_app(self) -> Starlette:
        routes = [
            Route("/", self._index, methods=["GET"]),
            Route("/index.html", self._index, methods=["GET"]),
            Route("/login", self._login_page, methods=["GET"]),
            Route("/login", self._login, methods=["POST"]),
            Route("/logout", self._logout, methods=["POST"]),
            Route("/bridge.js", self._bridge_js, methods=["GET"]),
            Route("/api/{path:path}", self._api, methods=["GET", "POST"]),
            Route("/vendor/{filename}", self._vendor, methods=["GET"]),
            Route("/{filename}", self._static, methods=["GET"]),
        ]
        return Starlette(routes=routes)

    # ── 会话 ─────────────────────────────────────────────

    def _password(self) -> str:
        try:
            return str(self._password_getter() or "")
        except Exception:
            return ""

    def _is_authed(self, request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            return False
        expires = self._tokens.get(token)
        if expires is None:
            return False
        if expires < time.time():
            self._tokens.pop(token, None)
            return False
        return True

    def _prune_tokens(self) -> None:
        now = time.time()
        for token in [t for t, exp in self._tokens.items() if exp < now]:
            self._tokens.pop(token, None)

    def revoke_all_sessions(self) -> None:
        """密码修改后调用：已登录的会话全部失效。"""
        self._tokens.clear()

    @staticmethod
    def _same_origin(request: Request) -> bool:
        """POST 的 Origin（有时）必须与 Host 一致，防跨站提交。"""
        origin = request.headers.get("origin")
        if not origin:
            return True
        host = request.headers.get("host", "")
        return urlsplit(origin).netloc == host

    async def _login_page(self, request: Request) -> Response:
        if self._is_authed(request):
            return RedirectResponse("./", status_code=303)
        return HTMLResponse(LOGIN_HTML, headers={"Cache-Control": "no-store"})

    async def _login(self, request: Request) -> Response:
        if not self._same_origin(request):
            return JSONResponse({"success": False, "error": "来源不匹配"}, status_code=403)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        given = str((payload or {}).get("password", "") if isinstance(payload, dict) else "")
        client = request.client.host if request.client else "?"
        # 串行化 + 固定延迟：耗时恒定，限制暴力尝试速度
        async with self._login_lock:
            await asyncio.sleep(LOGIN_DELAY_SECONDS)
            expected = self._password()
            ok = bool(expected) and hmac.compare_digest(_digest(given), _digest(expected))
            if not ok:
                logger.warning(f"[WebUI] 登录失败（密码错误）: {client}")
                return JSONResponse({"success": False, "error": "密码错误"}, status_code=401)
            self._prune_tokens()
            token = secrets.token_hex(32)
            self._tokens[token] = time.time() + SESSION_TTL_SECONDS
        logger.info(f"[WebUI] 登录成功: {client}")
        response = JSONResponse({"success": True})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    async def _logout(self, request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        self._tokens.pop(token, None)
        response = JSONResponse({"success": True})
        response.delete_cookie(SESSION_COOKIE)
        return response

    # ── 页面与静态资源 ────────────────────────────────────

    def _i18n_bundle(self) -> dict[str, Any]:
        bundle: dict[str, Any] = {}
        for locale_file in sorted(self._i18n_dir.glob("*.json")):
            try:
                data = json.loads(locale_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                bundle[locale_file.stem] = {"pages": data.get("pages", {})}
        return bundle

    async def _index(self, request: Request) -> Response:
        if not self._is_authed(request):
            return RedirectResponse("./login", status_code=303)
        html = (self._pages_dir / "index.html").read_text(encoding="utf-8")
        i18n_json = json.dumps(self._i18n_bundle(), ensure_ascii=False).replace("</", "<\\/")
        inject = (
            f"<script>window.__STEALER_I18N__ = {i18n_json};</script>\n"
            '<script src="./bridge.js"></script>\n'
        )
        html = html.replace("</head>", inject + "</head>", 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def _bridge_js(self, request: Request) -> Response:
        return Response(BRIDGE_JS, media_type="text/javascript; charset=utf-8")

    def _file_response(self, path: Path) -> Response:
        try:
            resolved = path.resolve()
            resolved.relative_to(self._pages_dir)
        except (OSError, ValueError):
            return Response("not found", status_code=404)
        if not resolved.is_file():
            return Response("not found", status_code=404)
        return FileResponse(str(resolved))

    async def _static(self, request: Request) -> Response:
        filename = str(request.path_params.get("filename", ""))
        if Path(filename).name != filename or filename.startswith("."):
            return Response("not found", status_code=404)
        # 登录页要用 logo；其余资源需登录
        if filename != "logo.png" and not self._is_authed(request):
            return Response("unauthorized", status_code=401)
        if filename == "index.html":
            return RedirectResponse("./", status_code=303)
        return self._file_response(self._pages_dir / filename)

    async def _vendor(self, request: Request) -> Response:
        if not self._is_authed(request):
            return Response("unauthorized", status_code=401)
        filename = str(request.path_params.get("filename", ""))
        if Path(filename).name != filename:
            return Response("not found", status_code=404)
        return self._file_response(self._pages_dir / "vendor" / filename)

    # ── API ──────────────────────────────────────────────

    async def _api(self, request: Request) -> Response:
        if not self._is_authed(request):
            return JSONResponse({"success": False, "error": "未登录"}, status_code=401)
        if request.method == "POST" and not self._same_origin(request):
            return JSONResponse({"success": False, "error": "来源不匹配"}, status_code=403)
        path = str(request.path_params.get("path", "")).strip("/")
        methods = self._handlers.get(path)
        if not methods:
            return JSONResponse({"success": False, "error": f"not found: {path}"}, status_code=404)
        handler = methods.get(request.method.upper())
        if handler is None:
            return JSONResponse({"success": False, "error": "method not allowed"}, status_code=405)

        adapter = RequestAdapter(request)
        token = bind_request(adapter)
        try:
            response = to_response(await handler())
        except Exception as e:
            logger.error(f"[WebUI] 接口 /api/{path} 处理失败: {e}", exc_info=True)
            response = JSONResponse({"success": False, "error": str(e)}, status_code=500)
        finally:
            reset_request(token)
            await adapter.close()
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    # ── 生命周期 ─────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._serve_task is not None and not self._serve_task.done()

    async def start(self, host: str, port: int) -> bool:
        """启动监听；端口被占用等失败时返回 False，不影响 mohobot 主进程。"""
        if self.running:
            return True
        if not self._password():
            logger.warning("[WebUI] 未设置 webui_password，WebUI 不启动")
            return False
        import uvicorn

        class _EmbeddedServer(uvicorn.Server):
            """不接管进程信号：mohobot 自己处理 SIGINT/SIGTERM，关闭走 on_shutdown。"""

            @contextlib.contextmanager
            def capture_signals(self):
                yield

            def install_signal_handlers(self) -> None:  # uvicorn < 0.29
                pass

        config = uvicorn.Config(self.app, host=host, port=int(port), log_level="warning")
        server = _EmbeddedServer(config)
        self._server = server
        self._serve_task = asyncio.create_task(self._serve(server), name="meme-stealer-webui")
        # 等到真正开始监听（或启动失败）
        while not server.started and not self._serve_task.done():
            await asyncio.sleep(0.05)
        if not server.started:
            self._server = None
            return False
        self.address = f"http://{host}:{port}"
        logger.info(f"[WebUI] 表情包管理页面已启动: {self.address}")
        return True

    @staticmethod
    async def _serve(server: Any) -> None:
        try:
            await server.serve()
        except SystemExit:
            # uvicorn 绑定端口失败时调用 sys.exit()，不能让它冒泡结束整个 mohobot
            logger.error("[WebUI] 启动失败（端口可能已被占用），请修改 webui_port 后重试")

    async def stop(self) -> None:
        server, task = self._server, self._serve_task
        self._server, self._serve_task = None, None
        self._tokens.clear()
        if server is None or task is None:
            return
        server.should_exit = True
        drained = False
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
                drained = True
            except asyncio.TimeoutError:
                logger.warning("[WebUI] 关闭超时，强制释放端口")
            except (asyncio.CancelledError, Exception):
                pass
        if not drained:
            for sock_server in list(getattr(server, "servers", None) or []):
                try:
                    sock_server.close()
                except Exception:
                    pass
            if not task.done():
                task.cancel()
        logger.info("[WebUI] 已停止")
