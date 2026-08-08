"""FastAPI web admin panel with SSE log streaming, file browser, and config editor.

Authentication: single admin password (bcrypt).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

import hashlib
import secrets
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# ── Login Models ──────────────────────────────────────────────


class LoginRequest(BaseModel):
    username: str
    password: str


class ConfigUpdateRequest(BaseModel):
    path: str
    content: str


# ── Web Panel App ─────────────────────────────────────────────


class WebPanel:
    """FastAPI-based web admin panel."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9090,
        username: str = "admin",
        password_hash: str = "",
        data_dir: str = "./data",
    ):
        self._host = host
        self._port = port
        self._username = username
        self._password_hash = password_hash
        self._data_dir = Path(data_dir)
        self._app = FastAPI(title="Mohobot Web Panel")
        self._log_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._active_sse_connections: set[asyncio.Queue] = set()
        self._setup_routes()

        # Store session tokens (simple in-memory)
        self._tokens: dict[str, float] = {}
        self._token_expiry = 3600  # 1 hour

    def _setup_routes(self) -> None:
        app = self._app

        # CORS
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.on_event("startup")
        async def startup():
            logger.info("Web panel started")

        # ── Auth Routes ──────────────────────────────────────

        @app.post("/api/login")
        async def login(req: LoginRequest):
            if req.username != self._username:
                raise HTTPException(status_code=401, detail="Invalid credentials")

            if self._password_hash:
                # Format: method$salt$hash  (e.g. pbkdf2_sha256$salt$hexhash)
                try:
                    method, salt, stored_hash = self._password_hash.split("$", 2)
                except ValueError:
                    raise HTTPException(status_code=401, detail="Invalid credentials")
                if method == "pbkdf2_sha256":
                    test_hash = hashlib.pbkdf2_hmac(
                        "sha256", req.password.encode("utf-8"), salt.encode("utf-8"), 100000
                    ).hex()
                    if test_hash != stored_hash:
                        raise HTTPException(status_code=401, detail="Invalid credentials")
                else:
                    raise HTTPException(status_code=401, detail="Invalid credentials")
            elif req.password == "admin":
                pass  # Default password when hash is empty
            else:
                raise HTTPException(status_code=401, detail="Invalid credentials")

            token = os.urandom(32).hex()
            self._tokens[token] = time.time() + self._token_expiry
            return {"token": token, "username": self._username}

        # ── Auth Middleware ───────────────────────────────────

        async def verify_token(request: Request) -> bool:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
                expiry = self._tokens.get(token, 0)
                if expiry > time.time():
                    return True
            return False

        async def require_auth(request: Request):
            if not await verify_token(request):
                raise HTTPException(status_code=401, detail="Unauthorized")

        # ── SSE Log Stream ───────────────────────────────────

        @app.get("/api/logs/stream")
        async def log_stream(request: Request):
            if not await verify_token(request):
                return JSONResponse(
                    status_code=401, content={"detail": "Unauthorized"}
                )

            queue: asyncio.Queue = asyncio.Queue()
            self._active_sse_connections.add(queue)

            async def event_generator() -> AsyncGenerator[dict, None]:
                try:
                    while True:
                        # Check if client disconnected
                        if await request.is_disconnected():
                            break
                        try:
                            log_entry = await asyncio.wait_for(
                                queue.get(), timeout=20.0
                            )
                            yield {
                                "event": "log",
                                "data": json.dumps(log_entry, ensure_ascii=False),
                            }
                        except asyncio.TimeoutError:
                            # Send keepalive
                            yield {"event": "ping", "data": "keepalive"}
                finally:
                    self._active_sse_connections.discard(queue)

            return EventSourceResponse(event_generator())

        # ── File Browser ─────────────────────────────────────

        @app.get("/api/files")
        async def list_files(request: Request, path: str = ""):
            await require_auth(request)
            # Resolve to absolute path — relative "data/.." breaks relative_to()
            base = (self._data_dir / "..").resolve()
            target = (base / path).resolve()

            # Ensure we don't escape the project directory
            if not str(target).startswith(str(base)):
                raise HTTPException(status_code=403, detail="Access denied")

            if target.is_file():
                content = target.read_text(encoding="utf-8")
                return {
                    "type": "file",
                    "path": str(target.relative_to(base)),
                    "name": target.name,
                    "size": target.stat().st_size,
                    "content": content,
                    "extension": target.suffix,
                }

            # List directory
            if not target.exists():
                raise HTTPException(status_code=404, detail="Not found")

            entries = []
            for entry in sorted(target.iterdir()):
                entries.append({
                    "name": entry.name,
                    "path": str(entry.relative_to(base)),
                    "type": "dir" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else 0,
                    "modified": datetime.fromtimestamp(
                        entry.stat().st_mtime
                    ).isoformat(),
                })

            return {"type": "dir", "path": str(target.relative_to(base)), "entries": entries}

        @app.post("/api/files")
        async def save_file(request: Request, req: ConfigUpdateRequest):
            await require_auth(request)
            base = (self._data_dir / "..").resolve()
            target = (base / req.path).resolve()

            if not str(target).startswith(str(base)):
                raise HTTPException(status_code=403, detail="Access denied")

            target.write_text(req.content, encoding="utf-8")
            logger.info(f"Web panel: saved file {req.path}")
            return {"status": "ok", "path": req.path}

        # ── Statistics ──────────────────────────────────────

        @app.get("/api/stats")
        async def get_stats(request: Request):
            await require_auth(request)

            # Count history files and line counts
            history_dir = self._data_dir / "history"
            total_messages = 0
            total_files = 0
            bot_count = 0

            if history_dir.exists():
                for bot_dir in history_dir.iterdir() if history_dir.is_dir() else []:
                    if bot_dir.is_dir():
                        bot_count += 1
                        for chat_type_dir in bot_dir.iterdir():
                            if chat_type_dir.is_dir():
                                for f in chat_type_dir.iterdir():
                                    if f.suffix == ".jsonl":
                                        total_files += 1
                                        total_messages += sum(
                                            1 for _ in f.read_text().splitlines()
                                            if _.strip()
                                        )

            # Count context files
            contexts_dir = self._data_dir / "contexts"
            context_count = 0
            if contexts_dir.exists():
                for bot_dir in contexts_dir.iterdir() if contexts_dir.is_dir() else []:
                    if bot_dir.is_dir():
                        for chat_type_dir in bot_dir.iterdir():
                            if chat_type_dir.is_dir():
                                for user_dir in chat_type_dir.iterdir():
                                    if user_dir.is_dir():
                                        for f in user_dir.iterdir():
                                            if f.suffix == ".json":
                                                context_count += 1

            return {
                "bots": bot_count,
                "total_messages": total_messages,
                "history_files": total_files,
                "context_files": context_count,
                "uptime": time.time() - self._start_time if hasattr(self, '_start_time') else 0,
            }

        # ── Dashboard Page ───────────────────────────────────

        @app.get("/", response_class=HTMLResponse)
        async def dashboard():
            return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mohobot Web Panel</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #0d1117; color: #c9d1d9; min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center;
                  padding: 16px 0; border-bottom: 1px solid #30363d; margin-bottom: 24px; }
        .header h1 { font-size: 24px; color: #58a6ff; }
        .login-form { max-width: 400px; margin: 100px auto; padding: 32px;
                      background: #161b22; border-radius: 8px; border: 1px solid #30363d; }
        .login-form h2 { margin-bottom: 24px; }
        .login-form input { width: 100%; padding: 10px; margin-bottom: 16px;
                            background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
                            color: #c9d1d9; font-size: 14px; }
        .login-form button { width: 100%; padding: 10px; background: #238636; color: #fff;
                             border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
        .login-form button:hover { background: #2ea043; }
        .login-error { color: #f85149; margin-top: 8px; font-size: 14px; }
        .hidden { display: none; }
        .tabs { display: flex; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
        .tab { padding: 8px 16px; background: #21262d; border: 1px solid #30363d;
               border-radius: 6px; cursor: pointer; color: #8b949e; }
        .tab.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
        .panel { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                 padding: 20px; min-height: 400px; }
        .log-container { max-height: 600px; overflow-y: auto; font-family: 'Courier New', monospace;
                         font-size: 13px; line-height: 1.6; }
        .log-entry { padding: 4px 8px; border-bottom: 1px solid #21262d; }
        .log-entry:hover { background: #21262d; }
        .log-time { color: #8b949e; }
        .log-level { font-weight: bold; padding: 0 4px; }
        .log-level.DEBUG { color: #8b949e; }
        .log-level.INFO { color: #58a6ff; }
        .log-level.WARNING { color: #d29922; }
        .log-level.ERROR { color: #f85149; }
        .file-list { list-style: none; }
        .file-list li { padding: 8px 12px; border-bottom: 1px solid #21262d;
                        cursor: pointer; display: flex; justify-content: space-between; }
        .file-list li:hover { background: #21262d; }
        .file-list .dir { color: #58a6ff; }
        .file-list .file { color: #c9d1d9; }
        .file-editor { width: 100%; min-height: 400px; background: #0d1117; color: #c9d1d9;
                       border: 1px solid #30363d; border-radius: 6px; padding: 12px;
                       font-family: 'Courier New', monospace; font-size: 13px; }
        .save-btn { margin-top: 12px; padding: 8px 16px; background: #238636; color: #fff;
                    border: none; border-radius: 6px; cursor: pointer; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                      gap: 16px; margin-bottom: 24px; }
        .stat-card { background: #21262d; padding: 20px; border-radius: 8px;
                     text-align: center; }
        .stat-card .value { font-size: 36px; font-weight: bold; color: #58a6ff; }
        .stat-card .label { font-size: 14px; color: #8b949e; margin-top: 4px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header"><h1>🤖 Mohobot Web Panel</h1><span id="status-indicator">未连接</span></div>

        <!-- Login -->
        <div id="login-form" class="login-form">
            <h2>登录</h2>
            <input type="text" id="username" placeholder="用户名" value="admin">
            <input type="password" id="password" placeholder="密码">
            <button onclick="login()">登录</button>
            <div id="login-error" class="login-error hidden"></div>
        </div>

        <!-- Main content (hidden until login) -->
        <div id="main-content" class="hidden">
            <div class="stats-grid" id="stats-grid"></div>
            <div class="tabs">
                <div class="tab active" onclick="switchTab('logs')">📋 实时日志</div>
                <div class="tab" onclick="switchTab('files')">📁 文件浏览器</div>
            </div>
            <div class="panel" id="tab-logs"><div class="log-container" id="log-container"></div></div>
            <div class="panel hidden" id="tab-files">
                <div id="file-browser"><p>请先登录</p></div>
                <div id="file-editor-area" class="hidden">
                    <div id="file-path-display"></div>
                    <textarea class="file-editor" id="file-editor-content"></textarea>
                    <button class="save-btn" onclick="saveFile()">💾 保存</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        let token = localStorage.getItem('token') || '';
        let currentFilePath = '';
        let currentDir = '';

        async function login() {
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const errEl = document.getElementById('login-error');
            errEl.classList.add('hidden');

            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username, password})
                });
                if (!res.ok) { errEl.textContent = '登录失败，请检查用户名和密码'; errEl.classList.remove('hidden'); return; }
                const data = await res.json();
                token = data.token;
                localStorage.setItem('token', token);
                document.getElementById('login-form').classList.add('hidden');
                document.getElementById('main-content').classList.remove('hidden');
                loadStats();
                startLogStream();
                loadFileList('');
            } catch(e) {
                errEl.textContent = '网络错误: ' + e.message;
                errEl.classList.remove('hidden');
            }
        }

        // Auto-login if token exists
        if (localStorage.getItem('token')) {
            token = localStorage.getItem('token');
            document.getElementById('login-form').classList.add('hidden');
            document.getElementById('main-content').classList.remove('hidden');
            loadStats();
            startLogStream();
            loadFileList('');
        }

        async function apiFetch(url, options = {}) {
            const headers = options.headers || {};
            headers['Authorization'] = 'Bearer ' + token;
            if (options.body && typeof options.body === 'object') {
                headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(options.body);
            }
            const res = await fetch(url, {...options, headers});
            if (res.status === 401) {
                token = '';
                localStorage.removeItem('token');
                document.getElementById('login-form').classList.remove('hidden');
                document.getElementById('main-content').classList.add('hidden');
                throw new Error('Unauthorized');
            }
            return res;
        }

        function switchTab(name) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.panel').forEach(p => p.classList.add('hidden'));
            document.querySelector(`.tab[onclick*="'${name}'"]`).classList.add('active');
            document.getElementById(`tab-${name}`).classList.remove('hidden');
            if (name === 'files') loadFileList(currentDir);
        }

        // ── Stats ──────────────────────────────────────────

        async function loadStats() {
            try {
                const res = await apiFetch('/api/stats');
                const data = await res.json();
                const grid = document.getElementById('stats-grid');
                grid.innerHTML = `
                    <div class="stat-card"><div class="value">${data.bots}</div><div class="label">已连接 Bot</div></div>
                    <div class="stat-card"><div class="value">${data.total_messages}</div><div class="label">历史消息</div></div>
                    <div class="stat-card"><div class="value">${data.history_files}</div><div class="label">历史文件</div></div>
                    <div class="stat-card"><div class="value">${data.context_files}</div><div class="label">会话文件</div></div>
                `;
            } catch(e) { /* ignore */ }
        }

        // ── SSE Log Stream ─────────────────────────────────

        function startLogStream() {
            const container = document.getElementById('log-container');
            const evtSource = new EventSource('/api/logs/stream?token=' + token);

            evtSource.addEventListener('log', function(e) {
                const data = JSON.parse(e.data);
                const el = document.createElement('div');
                el.className = 'log-entry';
                el.innerHTML = `<span class="log-time">${data.time || ''}</span> ` +
                    `<span class="log-level ${data.level}">[${data.level}]</span> ` +
                    `<span>${data.message || ''}</span>`;
                container.appendChild(el);
                container.scrollTop = container.scrollHeight;
                // Keep max 500 entries
                while (container.children.length > 500) container.removeChild(container.firstChild);
            });

            evtSource.addEventListener('ping', function(e) { /* keepalive */ });
            evtSource.onerror = function() {
                setTimeout(startLogStream, 3000);
            };
        }

        // ── File Browser ───────────────────────────────────

        async function loadFileList(dir) {
            currentDir = dir;
            const area = document.getElementById('file-browser');
            try {
                const res = await apiFetch('/api/files?path=' + encodeURIComponent(dir));
                const data = await res.json();
                if (data.type === 'file') {
                    document.getElementById('file-browser').innerHTML =
                        `<div class="file-list"><li onclick="loadFileList('${dir.substring(0, dir.lastIndexOf('/'))}')">⬆ 返回上级</li></div>`;
                    showEditor(data);
                    return;
                }
                let html = '<ul class="file-list">';
                if (dir) html += `<li onclick="loadFileList('${dir.substring(0, dir.lastIndexOf('/'))}')"><span class="dir">⬆ 返回上级</span></li>`;
                for (const entry of data.entries) {
                    const icon = entry.type === 'dir' ? '📁' : '📄';
                    html += `<li onclick="loadFileList('${entry.path}')">
                        <span class="${entry.type}">${icon} ${entry.name}</span>
                        <span style="color:#8b949e;font-size:12px">${entry.type === 'file' ? (entry.size/1024).toFixed(1)+'KB' : ''}</span>
                    </li>`;
                }
                html += '</ul>';
                area.innerHTML = html;
                document.getElementById('file-editor-area').classList.add('hidden');
            } catch(e) { area.innerHTML = '<p>加载失败: ' + e.message + '</p>'; }
        }

        function showEditor(data) {
            document.getElementById('file-path-display').textContent = '📄 ' + data.path;
            document.getElementById('file-editor-content').value = data.content || '';
            currentFilePath = data.path;
            document.getElementById('file-editor-area').classList.remove('hidden');

            // Syntax-highlight by extension
            const ext = data.extension;
            if (['.json', '.jsonl', '.yaml', '.yml', '.py'].includes(ext)) {
                // Could add basic syntax highlighting here
            }
        }

        async function saveFile() {
            const content = document.getElementById('file-editor-content').value;
            try {
                const res = await apiFetch('/api/files', {
                    method: 'POST',
                    body: {path: currentFilePath, content}
                });
                if (res.ok) alert('保存成功！');
                else alert('保存失败');
            } catch(e) { alert('保存失败: ' + e.message); }
        }
    </script>
</body>
</html>"""

        # ── Health ───────────────────────────────────────────

        @app.get("/api/health")
        async def health():
            return {"status": "ok", "time": datetime.now().isoformat()}

    async def push_log(self, level: str, message: str) -> None:
        """Push a log entry to all connected SSE clients."""
        entry = {
            "time": datetime.now().isoformat(),
            "level": level,
            "message": message,
        }
        dead_connections = set()
        for queue in self._active_sse_connections:
            try:
                await asyncio.wait_for(queue.put(entry), timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                dead_connections.add(queue)
        for q in dead_connections:
            self._active_sse_connections.discard(q)

    async def start(self) -> None:
        """Start the uvicorn server."""
        import uvicorn
        self._start_time = time.time()

        # Patch loguru to forward to SSE
        logger.info("Starting web panel...")

        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        self._server_instance = server
        await server.serve()

    async def stop(self) -> None:
        """Stop the uvicorn server."""
        if hasattr(self, '_server_instance'):
            self._server_instance.should_exit = True
            logger.info("Web panel stopped")