"""FastAPI web admin panel — 7-section dashboard.

Sections:
  1. 数据总览 (dashboard): system + framework stats, bot list, LLM token usage
  2. 配置文件 (config): visual editing of global + per-bot configs
  3. 模型配置 (models): provider / api key / model editing
  4. 插件管理 (plugins): enable/disable + plugin info
  5. 对话数据 (contexts): browse/edit sessions & messages
  6. 实时日志 (logs): SSE stream with level filtering
  7. 系统设置 (settings): password change, service restart
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, AsyncGenerator

from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ── Request Models ────────────────────────────────────────────


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


class ConfigUpdateRequest(BaseModel):
    data: dict[str, Any]


class BotConfigUpdateRequest(BaseModel):
    data: dict[str, Any]


class PluginToggleRequest(BaseModel):
    name: str
    enabled: bool


class SessionCreateRequest(BaseModel):
    name: str = "新会话"


class MessageUpdateRequest(BaseModel):
    index: int
    content: str
    role: str | None = None


def _remove_file_background(path: str):
    """FileResponse 下载完成后删除临时文件。"""
    def _cleanup() -> None:
        try:
            os.remove(path)
        except OSError:
            pass
    return _cleanup


# ── Web Panel App ─────────────────────────────────────────────


class WebPanel:
    """FastAPI-based web admin panel with 7 dashboard sections."""

    _SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
    _MASK = "********"

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
        return f"pbkdf2_sha256${salt}${digest}"

    @staticmethod
    def _verify_password(password: str, stored: str) -> bool:
        try:
            method, salt, expected = stored.split("$", 2)
            if method != "pbkdf2_sha256":
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False

    @classmethod
    def _safe_id(cls, value: str, label: str = "参数") -> str:
        if not cls._SAFE_ID.fullmatch(value):
            raise HTTPException(status_code=400, detail=f"{label} 无效")
        return value

    @classmethod
    def _mask_secret(cls, value: Any) -> str:
        return cls._MASK if value else ""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9090,
        username: str = "admin",
        password_hash: str = "",
        data_dir: str = "./data",
        config_path: str = "./config/global.yaml",
        bot_manager=None,
        context_manager=None,
        llm_service=None,
        plugin_system=None,
        ban_store=None,
        ban_filter=None,
        restart_callback=None,
    ):
        self._host = host
        self._port = port
        self._username = username
        # Resolve the current persisted configuration when available.  A panel
        # must never silently fall back to the historical admin/admin login.
        if not password_hash:
            try:
                from mohobot.models.config import GlobalConfig
                password_hash = GlobalConfig.load(config_path).web_panel.password_hash
            except Exception:
                password_hash = ""
        env_password = os.environ.get("MOHOBOT_WEB_PASSWORD", "")
        self._password_hash = password_hash
        self._data_dir = Path(data_dir)
        if env_password and not self._password_hash:
            self._password_hash = self._hash_password(env_password)
            try:
                from mohobot.models.config import GlobalConfig
                cfg = GlobalConfig.load(config_path)
                cfg.web_panel.password_hash = self._password_hash
                cfg.save(config_path)
            except Exception as exc:
                logger.error(f"Could not persist MOHOBOT_WEB_PASSWORD hash: {exc}")
                raise
        if not self._password_hash:
            raise ValueError("WebPanel requires web_panel.password_hash or MOHOBOT_WEB_PASSWORD")
        self._config_path = Path(config_path)
        self._bot_manager = bot_manager
        self._audit = None
        self._context_manager = context_manager
        self._llm_service = llm_service
        self._plugin_system = plugin_system
        self._ban_store = ban_store
        self._ban_filter = ban_filter
        self._restart_callback = restart_callback

        self._app = FastAPI(title="Mohobot Web Panel")
        self._log_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._active_sse_connections: set[asyncio.Queue] = set()
        self._tokens: dict[str, float] = {}
        self._sse_tickets: dict[str, float] = {}
        self._token_expiry = 3600  # 1 hour
        self._start_time = time.time()
        # framework 统计缓存(60s TTL): history 遍历开销大, 避免每次刷新阻塞
        self._fw_stats_cache: tuple[float, dict] | None = None
        from mohobot.services.audit import AuditLogger
        self._audit = AuditLogger(str(self._data_dir))
        self._setup_routes()

        # Forward loguru messages to SSE subscribers (best-effort)
        self._install_log_sink()

    # ── Log forwarding to SSE ─────────────────────────────────

    def _install_log_sink(self) -> None:
        """Sink loguru records and broadcast them to all active SSE connections."""
        from loguru import logger as lg

        def _sink(message) -> None:
            record = message.record
            from mohobot.utils.time_utils import to_utc8
            entry = {
                "time": to_utc8(record["time"]).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record["level"].name,
                "message": record["message"],
            }
            # Broadcast to every connected SSE queue (best-effort, drop if full)
            for q in list(self._active_sse_connections):
                try:
                    if q.full():
                        q.get_nowait()
                    q.put_nowait(entry)
                except Exception:
                    pass

        # NOTE: no format= here — with format set, loguru passes the formatted
        # STRING to the sink instead of the Message object, breaking .record
        self._sink_id = lg.add(_sink, level="DEBUG", enqueue=False)

    # ── Routes ────────────────────────────────────────────────

    def _setup_routes(self) -> None:
        app = self._app

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://127.0.0.1", "http://localhost"],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

        # ── Auth helpers ──────────────────────────────────────

        async def _verify_token(request: Request) -> bool:
            # Expired entries are removed opportunistically on every auth check.
            now = time.time()
            for token, expiry in list(self._tokens.items()):
                if expiry <= now:
                    self._tokens.pop(token, None)
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return False
            token = auth[7:]
            expiry = self._tokens.get(token, 0)
            return bool(expiry > now and hmac.compare_digest(token, token))

        async def _require_auth(request: Request):
            if not await _verify_token(request):
                raise HTTPException(status_code=401, detail="Unauthorized")

        async def _audit(request: Request, action: str, target: str = "", details: Any = None, success: bool = True):
            token = request.headers.get("Authorization", "")[7:]
            from mohobot.services.audit import AuditLogger
            audit_logger = AuditLogger(str(self._data_dir))
            try:
                await audit_logger.write(actor=self._username, action=action, target=target,
                                         success=success, details=details,
                                         remote=request.client.host if request.client else "")
            except Exception as exc:
                logger.warning(f"Web panel audit write failed: {exc}")
            finally:
                await audit_logger.close()

        # ── Auth ──────────────────────────────────────────────

        @app.middleware("http")
        async def audit_mutations(request: Request, call_next):
            response = await call_next(request)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in {"/api/login"}:
                details = {k: v for k, v in request.query_params.items() if k.lower() not in {"token", "password"}}
                from mohobot.services.audit import AuditLogger
                audit_logger = AuditLogger(str(self._data_dir))
                try:
                    await audit_logger.write(
                        actor=self._username, action=f"{request.method} {request.url.path}",
                        target=request.url.path, success=response.status_code < 400,
                        details=details, remote=request.client.host if request.client else "",
                    )
                except Exception as exc:
                    logger.warning(f"Web panel audit write failed: {exc}")
                finally:
                    await audit_logger.close()
            return response

        @app.post("/api/login")
        async def login(req: LoginRequest):
            if req.username != self._username:
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            if not self._password_hash or not self._verify_password(req.password, self._password_hash):
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            token = os.urandom(32).hex()
            self._tokens[token] = time.time() + self._token_expiry
            return {"token": token, "username": self._username}

        @app.post("/api/logout")
        async def logout(request: Request):
            await _require_auth(request)
            auth = request.headers.get("Authorization", "")
            self._tokens.pop(auth[7:], None)
            return {"status": "ok"}

        # ── 1. Dashboard (数据总览) ──────────────────────────

        @app.get("/api/dashboard")
        async def dashboard(request: Request):
            await _require_auth(request)

            system = await self._get_system_stats()
            framework = await self._get_framework_stats()
            bots = await self._get_bot_list()
            usage = await self._get_llm_usage()

            return {
                "system": system,
                "framework": framework,
                "bots": bots,
                "llm_usage": usage,
            }

        # ── 2. Configuration (配置文件) ───────────────────────

        @app.get("/api/config")
        async def get_config(request: Request):
            await _require_auth(request)
            from mohobot.models.config import GlobalConfig
            cfg = GlobalConfig.load(self._config_path)
            result = cfg.to_dict()
            result["web_panel"]["password_hash"] = self._mask_secret(cfg.web_panel.password_hash)
            for section in ("llm", "anysearch"):
                for key, value in list(result.get(section, {}).items()):
                    if any(marker in key.lower() for marker in ("key", "token", "secret", "password")):
                        result[section][key] = self._mask_secret(value)
            return result

        @app.put("/api/config")
        async def update_config(request: Request, body: ConfigUpdateRequest):
            await _require_auth(request)
            from mohobot.models.config import GlobalConfig
            cfg = GlobalConfig.load(self._config_path)
            data = body.data

            # Apply nested updates safely
            # 注意: database / log_dir / data_dir / plugins_dir 属于
            # 服务端运行路径配置, 不允许在 WebUI 修改(改错会导致服务异常)。
            if "server" in data:
                for k, v in data["server"].items():
                    if hasattr(cfg.server, k):
                        setattr(cfg.server, k, v)
            if "web_panel" in data:
                for k, v in data["web_panel"].items():
                    if hasattr(cfg.web_panel, k) and k != "password_hash":
                        setattr(cfg.web_panel, k, v)
            if "interceptor" in data:
                for k, v in data["interceptor"].items():
                    if hasattr(cfg.interceptor, k):
                        setattr(cfg.interceptor, k, v)
            if "reply" in data:
                for k, v in data["reply"].items():
                    if hasattr(cfg.reply, k):
                        setattr(cfg.reply, k, v)
            if "agent" in data:
                agent_data = data["agent"] or {}
                if "enabled" in agent_data:
                    cfg.agent.enabled = bool(agent_data["enabled"])
                # llm_modules: 深合并(面板只提交 model, 保留模块级 base_url/temperature 等)
                if isinstance(agent_data.get("llm_modules"), dict):
                    merged_modules = dict(cfg.agent.llm_modules or {})
                    for mod, spec in agent_data["llm_modules"].items():
                        if isinstance(spec, dict):
                            old = merged_modules.get(mod) or {}
                            merged_modules[mod] = {**old, **spec}
                    cfg.agent.llm_modules = merged_modules
                # persona 已移除(每个 bot 的人设取自其私有配置)
                for k in ("memory", "main_chat",
                          "topic_planner", "topic_replier", "reflection_worker", "reflex",
                          "music_knowledge"):
                    if k in agent_data and isinstance(agent_data[k], dict):
                        setattr(cfg.agent, k, agent_data[k] or {})
            if "ban" in data:
                ban_data = data["ban"] or {}
                if "enabled" in ban_data:
                    cfg.ban.enabled = bool(ban_data["enabled"])
            # 顶层 admins(封禁/插件命令共用管理员)
            if "admins" in data and isinstance(data["admins"], list):
                cfg.admins = [int(a) for a in data["admins"] if str(a).isdigit()]
            elif (
                isinstance(data.get("ban"), dict)
                and isinstance(data["ban"].get("admins"), list)
            ):
                # 兼容旧页面把 admins 提交到 ban 下的情况
                cfg.admins = [
                    int(a) for a in data["ban"]["admins"] if str(a).isdigit()
                ]
            for key in ("beta_mode", "context_max_rounds"):
                if key in data:
                    setattr(cfg, key, data[key])
            for key in ("context_summary_enabled", "context_trim_at_rounds",
                        "context_trim_remove_rounds", "group_recent_msgs_count"):
                if key in data:
                    setattr(cfg, key, data[key])

            cfg.save(self._config_path)
            # 热同步上下文压缩配置(立即生效, 无需重启)
            if self._context_manager is not None:
                self._context_manager.set_trim_config(
                    enabled=cfg.context_summary_enabled,
                    at_rounds=cfg.context_trim_at_rounds,
                    remove_rounds=cfg.context_trim_remove_rounds,
                )
            # 热同步封禁拦截器(启停/管理员即时生效, 无需重启)
            if self._ban_filter is not None:
                self._ban_filter.sync_config(
                    enabled=cfg.ban.enabled, admins=cfg.admins,
                )
            # 热同步插件管理员(关系插件等命令权限)
            if self._plugin_system is not None:
                self._plugin_system.set_admin_ids(list(cfg.admins))
            logger.info(f"Web panel: global config updated ({list(data.keys())})")
            return {"status": "ok"}

        @app.get("/api/bots")
        async def list_bots(request: Request):
            await _require_auth(request)
            from mohobot.models.config import BotConfig

            # Scan data/bots/*/config.json — returns ALL configured bots
            # (online or not), with qq + nickname from their config.
            bots = []
            online_ids = set()
            if self._bot_manager:
                online_ids = {inst.bot_id for inst in self._bot_manager.all_bots}
                for cfg in self._bot_manager.list_bot_configs():
                    bots.append({
                        "bot_id": cfg.bot_id,
                        "qq": cfg.qq,
                        "nickname": cfg.nickname,
                        "online": cfg.bot_id in online_ids,
                        "enabled": cfg.enabled,
                        "bound": bool(cfg.qq),
                    })
            return {
                "bots": bots,
                "unbound": self._get_unbound_list(),
            }

        @app.post("/api/bots")
        async def create_bot(request: Request, body: BotConfigUpdateRequest):
            """面板手动创建新 bot(可选绑定 QQ)。"""
            await _require_auth(request)
            if not self._bot_manager:
                raise HTTPException(status_code=500, detail="Bot 管理器不可用")
            data = body.data or {}
            nickname = str(data.get("nickname", "") or "")
            qq = int(data.get("qq", 0) or 0)
            cfg = self._bot_manager.create_bot(nickname=nickname, qq=qq)
            logger.info(f"Web panel: bot created {cfg.bot_id}")
            return {"status": "ok", "bot_id": cfg.bot_id}

        @app.put("/api/bots/{bot_id}/bind")
        async def bind_bot_qq(bot_id: str, request: Request, body: ConfigUpdateRequest):
            """把 QQ 绑定到 bot(QQ 唯一绑定, 自动解绑其他 bot)。"""
            await _require_auth(request)
            if not self._bot_manager:
                raise HTTPException(status_code=500, detail="Bot 管理器不可用")
            bot_id = self._safe_id(bot_id, "bot_id")
            qq = int((body.data or {}).get("qq", 0) or 0)
            if not qq:
                raise HTTPException(status_code=400, detail="QQ 不能为空")
            ok = self._bot_manager.bind_qq(bot_id, qq)
            if not ok:
                raise HTTPException(status_code=404, detail=f"Bot 不存在: {bot_id}")
            logger.info(f"Web panel: {bot_id} 绑定 QQ {qq}")
            return {"status": "ok", "bot_id": bot_id, "qq": qq}

        @app.post("/api/bots/{bot_id}/unbind")
        async def unbind_bot_qq(bot_id: str, request: Request):
            bot_id = self._safe_id(bot_id, "bot_id")
            if not self._bot_manager:
                raise HTTPException(status_code=500, detail="Bot 管理器不可用")
            ok = self._bot_manager.unbind_qq(bot_id)
            if not ok:
                raise HTTPException(status_code=404, detail=f"Bot 不存在: {bot_id}")
            logger.info(f"Web panel: {bot_id} 解绑 QQ")
            return {"status": "ok", "bot_id": bot_id}

        @app.get("/api/bots/{bot_id}/config")
        async def get_bot_config(bot_id: str, request: Request):
            await _require_auth(request)
            from mohobot.models.config import BotConfig
            bot_id = self._safe_id(bot_id, "bot_id")
            config_path = self._data_dir / "bots" / bot_id / "config.json"
            cfg = BotConfig.load(config_path)
            result = cfg.to_dict()
            return result

        @app.put("/api/bots/{bot_id}/config")
        async def update_bot_config(bot_id: str, request: Request, body: BotConfigUpdateRequest):
            await _require_auth(request)
            from mohobot.models.config import BotConfig
            bot_id = self._safe_id(bot_id, "bot_id")
            config_path = self._data_dir / "bots" / bot_id / "config.json"
            cfg = BotConfig.load(config_path)
            data = body.data
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
            cfg.save(config_path)

            # Apply to live instance if connected
            if self._bot_manager:
                inst = self._bot_manager.get(bot_id)
                if inst:
                    inst.config = cfg

            logger.info(f"Web panel: bot {bot_id} config updated")
            return {"status": "ok"}

        # ── 3. Models (模型配置) ─────────────────────────────

        @app.get("/api/models")
        async def get_models(request: Request):
            await _require_auth(request)
            from mohobot.models.config import GlobalConfig
            cfg = GlobalConfig.load(self._config_path)
            return {
                "chat": {
                    "model": cfg.llm.chat_model,
                    "base_url": cfg.llm.chat_base_url,
                    "api_key": self._mask_secret(cfg.llm.chat_api_key),
                    "max_tokens": cfg.llm.chat_max_tokens,
                    "temperature": cfg.llm.chat_temperature,
                    "fallback_model": cfg.llm.fallback_model,
                },
                "vision": {
                    "model": cfg.llm.vision_model,
                    "base_url": cfg.llm.vision_base_url,
                    "api_key": self._mask_secret(cfg.llm.vision_api_key),
                    "prompt": cfg.llm.vision_prompt,
                },
                "models": list(cfg.llm.models),
            }

        @app.put("/api/models")
        async def update_models(request: Request, body: ConfigUpdateRequest):
            await _require_auth(request)
            from mohobot.models.config import GlobalConfig
            cfg = GlobalConfig.load(self._config_path)
            data = body.data

            if "chat" in data:
                for k, v in data["chat"].items():
                    if hasattr(cfg.llm, f"chat_{k}") and k != "api_key":
                        setattr(cfg.llm, f"chat_{k}", v)
                    elif k == "fallback_model":
                        cfg.llm.fallback_model = str(v or "")
                if "api_key" in data["chat"] and data["chat"]["api_key"] not in ("", self._MASK):
                    cfg.llm.chat_api_key = str(data["chat"]["api_key"])
            if "vision" in data:
                for k, v in data["vision"].items():
                    if hasattr(cfg.llm, f"vision_{k}") and k != "api_key":
                        setattr(cfg.llm, f"vision_{k}", v)
                    elif k == "prompt":
                        cfg.llm.vision_prompt = str(v or "")
                if "api_key" in data["vision"] and data["vision"]["api_key"] not in ("", self._MASK):
                    cfg.llm.vision_api_key = str(data["vision"]["api_key"])
            if "models" in data and isinstance(data["models"], list):
                cfg.llm.models = [str(m).strip() for m in data["models"] if str(m).strip()]

            cfg.save(self._config_path)
            logger.info("Web panel: LLM model config updated")
            return {"status": "ok"}

        # ── 4. Plugins (插件管理) ────────────────────────────

        @app.get("/api/plugins")
        async def list_plugins(request: Request):
            await _require_auth(request)
            if not self._plugin_system:
                return []
            return self._plugin_system.list_plugins()

        @app.post("/api/plugins/toggle")
        async def toggle_plugin(request: Request, body: PluginToggleRequest):
            await _require_auth(request)
            if not self._plugin_system:
                raise HTTPException(status_code=404, detail="插件系统未启用")
            await self._plugin_system.set_enabled(body.name, body.enabled)
            logger.info(f"Web panel: plugin {body.name} {'enabled' if body.enabled else 'disabled'} (hot)")
            return {"status": "ok", "name": body.name, "enabled": body.enabled}

        @app.post("/api/plugins/reload")
        async def reload_plugins(request: Request):
            """热重载插件: 新增/修改/删除插件文件立即生效, 无需重启。"""
            await _require_auth(request)
            if not self._plugin_system:
                raise HTTPException(status_code=500, detail="插件系统未启用")
            count = await self._plugin_system.reload_plugins()
            logger.info(f"Web panel: plugins hot-reloaded ({count} active)")
            return {"status": "ok", "count": count, "plugins": self._plugin_system.list_plugins()}

        @app.get("/api/plugins/{name}/config")
        async def get_plugin_config(name: str, request: Request):
            """读取插件配置(schema + 当前值)。"""
            await _require_auth(request)
            if not self._plugin_system:
                raise HTTPException(status_code=404, detail="插件系统未启用")
            config = self._plugin_system.get_plugin_config(name)
            schema = self._plugin_system.get_config_schema(name)
            if config is None or schema is None:
                raise HTTPException(status_code=404, detail=f"插件 {name} 无配置")
            return {"schema": schema, "config": config}

        @app.post("/api/plugins/{name}/config")
        async def save_plugin_config(name: str, request: Request, body: ConfigUpdateRequest):
            """保存插件配置(schema 校验 + 热生效)。"""
            await _require_auth(request)
            if not self._plugin_system:
                raise HTTPException(status_code=404, detail="插件系统未启用")
            ok = await self._plugin_system.save_plugin_config(name, body.data or {})
            if not ok:
                raise HTTPException(status_code=404, detail=f"插件 {name} 无配置或不存在")
            logger.info(f"Web panel: plugin {name} config updated (hot)")
            return {"status": "ok", "config": self._plugin_system.get_plugin_config(name)}

        # ── 5. Conversations (对话数据) ──────────────────────

        @app.get("/api/contexts")
        async def list_chats(request: Request):
            await _require_auth(request)
            if not self._context_manager:
                return []
            bots = []
            if self._bot_manager:
                for inst in self._bot_manager.all_bots:
                    chats = await self._context_manager.list_chats(inst.bot_id)
                    bots.append({"bot_id": inst.bot_id, "chats": chats})
            return bots

        @app.get("/api/contexts/{bot_id}/{chat_type}/{chat_id}")
        async def get_chat_sessions(
            bot_id: str, chat_type: str, chat_id: str, request: Request,
        ):
            await _require_auth(request)
            if not self._context_manager:
                return {"sessions": []}
            sessions = await self._context_manager.list_sessions(bot_id, chat_type, chat_id)
            active = await self._context_manager.get_active_session_id(bot_id, chat_type, chat_id)
            return {"sessions": sessions, "active": active}

        @app.get("/api/contexts/{bot_id}/{chat_type}/{chat_id}/session/{session_id}")
        async def get_session_detail(
            bot_id: str, chat_type: str, chat_id: str, session_id: str, request: Request,
        ):
            await _require_auth(request)
            if not self._context_manager:
                return None
            return await self._context_manager.get_session(
                bot_id, chat_type, chat_id, session_id
            )

        @app.post("/api/contexts/{bot_id}/{chat_type}/{chat_id}/session")
        async def create_session(
            bot_id: str, chat_type: str, chat_id: str, request: Request,
            body: SessionCreateRequest,
        ):
            await _require_auth(request)
            if not self._context_manager:
                raise HTTPException(status_code=500, detail="会话管理器不可用")
            sid = await self._context_manager.create_session(
                bot_id, chat_type, chat_id, body.name
            )
            return {"status": "ok", "session_id": sid}

        @app.delete("/api/contexts/{bot_id}/{chat_type}/{chat_id}/session/{session_id}")
        async def delete_session(
            bot_id: str, chat_type: str, chat_id: str, session_id: str, request: Request,
        ):
            await _require_auth(request)
            if not self._context_manager:
                raise HTTPException(status_code=500, detail="会话管理器不可用")
            ok = await self._context_manager.delete_session(
                bot_id, chat_type, chat_id, session_id
            )
            if not ok:
                raise HTTPException(status_code=400, detail="无法删除该会话")
            return {"status": "ok"}

        @app.post("/api/contexts/{bot_id}/{chat_type}/{chat_id}/session/{session_id}/reset")
        async def reset_session(
            bot_id: str, chat_type: str, chat_id: str, session_id: str, request: Request,
        ):
            await _require_auth(request)
            if not self._context_manager:
                raise HTTPException(status_code=500, detail="会话管理器不可用")
            ok = await self._context_manager.reset_session(
                bot_id, chat_type, chat_id, session_id
            )
            if not ok:
                raise HTTPException(status_code=404, detail="会话不存在")
            return {"status": "ok"}

        @app.put("/api/contexts/{bot_id}/{chat_type}/{chat_id}/session/{session_id}/message")
        async def update_message(
            bot_id: str, chat_type: str, chat_id: str, session_id: str,
            request: Request, body: MessageUpdateRequest,
        ):
            await _require_auth(request)
            if not self._context_manager:
                raise HTTPException(status_code=500, detail="会话管理器不可用")
            ok = await self._context_manager.update_message(
                bot_id, chat_type, chat_id, session_id, body.index, body.content, body.role
            )
            if not ok:
                raise HTTPException(status_code=400, detail="消息索引越界或会话不存在")
            return {"status": "ok"}

        # ── 6. Live Logs (实时日志) ──────────────────────────

        @app.post("/api/logs/ticket")
        async def create_log_ticket(request: Request):
            await _require_auth(request)
            ticket = secrets.token_urlsafe(32)
            self._sse_tickets[ticket] = time.time() + 30
            return {"ticket": ticket}

        @app.get("/api/logs/stream")
        async def log_stream(request: Request):
            # EventSource cannot set Authorization. Exchange the bearer token
            # via POST for a short-lived, one-use ticket so the session token is
            # never placed in URLs, browser history, access logs or Referer.
            ticket = request.query_params.get("ticket", "")
            expiry = self._sse_tickets.pop(ticket, 0) if ticket else 0
            if expiry <= time.time():
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

            # Multi-select levels: comma-separated, e.g. ?level=DEBUG,INFO
            raw_levels = request.query_params.get("level", "").upper()
            level_set: set[str] = set()
            if raw_levels:
                for lv in raw_levels.split(","):
                    lv = lv.strip()
                    if lv:
                        level_set.add(lv)

            queue: asyncio.Queue = asyncio.Queue()
            self._active_sse_connections.add(queue)

            async def event_generator() -> AsyncGenerator[dict, None]:
                try:
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                            if level_set and entry["level"] not in level_set:
                                continue
                            yield {"event": "log", "data": json.dumps(entry, ensure_ascii=False)}
                        except asyncio.TimeoutError:
                            yield {"event": "ping", "data": "keepalive"}
                finally:
                    self._active_sse_connections.discard(queue)

            return EventSourceResponse(event_generator())

        # ── 7. Settings (系统设置) ───────────────────────────

        @app.put("/api/settings/password")
        async def change_password(request: Request, body: PasswordChangeRequest):
            await _require_auth(request)

            if not self._verify_password(body.old_password, self._password_hash):
                raise HTTPException(status_code=400, detail="原密码错误")

            from mohobot.models.config import GlobalConfig
            cfg = GlobalConfig.load(self._config_path)
            cfg.web_panel.password_hash = self._hash_password(body.new_password)
            cfg.save(self._config_path)
            self._password_hash = cfg.web_panel.password_hash

            # Invalidate all sessions
            self._tokens.clear()

            logger.info("Web panel: admin password changed")
            return {"status": "ok"}

        @app.post("/api/settings/restart")
        async def restart_service(request: Request):
            await _require_auth(request)
            if not self._restart_callback:
                raise HTTPException(status_code=500, detail="重启回调未配置")

            async def _delayed_restart():
                await asyncio.sleep(1.0)
                try:
                    await self._restart_callback()
                except Exception as e:
                    logger.error(f"Restart failed: {e}")

            asyncio.create_task(_delayed_restart())
            return {"status": "ok", "message": "服务即将重启..."}

        # ── 8. Data management (数据管理: 备份/恢复/清理) ──────

        DATA_SCOPES = {"cache", "history", "contexts", "ban"}

        def _parse_data_scope(scope: Any) -> tuple[list[str] | None, set[str]]:
            """解析范围: 返回 (bots 列表或 None=全部, dirs 集合)。"""
            if not isinstance(scope, dict):
                raise HTTPException(status_code=400, detail="范围参数无效")
            bots_raw = scope.get("bots", "all")
            if bots_raw == "all" or bots_raw is None:
                bot_list: list[str] | None = None
            elif isinstance(bots_raw, list):
                bot_list = [self._safe_id(str(b), "bot_id") for b in bots_raw if str(b)]
                if not bot_list:
                    raise HTTPException(status_code=400, detail="未选择任何 Bot")
            else:
                raise HTTPException(status_code=400, detail="bots 参数无效")
            dirs_raw = scope.get("dirs")
            if not isinstance(dirs_raw, list):
                raise HTTPException(status_code=400, detail="dirs 参数无效")
            dir_set = {str(d) for d in dirs_raw} & DATA_SCOPES
            if not dir_set:
                raise HTTPException(status_code=400, detail="未选择任何数据范围(cache/history/contexts/ban)")
            return bot_list, dir_set

        def _collect_data_files(
            bot_list: list[str] | None, dir_set: set[str],
        ) -> list[tuple[str, Path]]:
            """收集选定范围内的文件: (zip 内相对路径, 磁盘路径)。"""
            files: list[tuple[str, Path]] = []
            root = self._data_dir

            if "cache" in dir_set:
                cache_dir = root / "cache"
                if cache_dir.exists():
                    for f in cache_dir.rglob("*"):
                        if f.is_file():
                            files.append((f"cache/{f.relative_to(cache_dir).as_posix()}", f))

            if "ban" in dir_set:
                ban_dir = root / "ban"
                if ban_dir.exists():
                    for f in ban_dir.rglob("*"):
                        if f.is_file():
                            files.append((f"ban/{f.relative_to(ban_dir).as_posix()}", f))

            for d in ("history", "contexts"):
                if d not in dir_set:
                    continue
                base = root / d
                if not base.exists():
                    continue
                if bot_list is None:
                    bot_dirs = [e for e in base.iterdir() if e.is_dir()]
                else:
                    bot_dirs = [base / b for b in bot_list]
                for bot_dir in bot_dirs:
                    if not bot_dir.exists() or not bot_dir.is_dir():
                        continue
                    for f in bot_dir.rglob("*"):
                        if f.is_file():
                            files.append((
                                f"{d}/{bot_dir.name}/{f.relative_to(bot_dir).as_posix()}",
                                f,
                            ))
            return files

        @app.post("/api/data/backup")
        async def backup_data(request: Request, body: ConfigUpdateRequest):
            """按选定范围打包下载 zip 备份。"""
            await _require_auth(request)
            bot_list, dir_set = _parse_data_scope(body.data)
            files = _collect_data_files(bot_list, dir_set)
            if not files:
                raise HTTPException(status_code=400, detail="所选范围没有数据")

            tmp_path = tempfile.NamedTemporaryFile(suffix=".zip", delete=False).name
            try:
                with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for arcname, path in files:
                        zf.write(path, arcname)
            except Exception as e:
                os.remove(tmp_path)
                raise HTTPException(status_code=500, detail=f"备份失败: {e}")

            from mohobot.utils.time_utils import format_utc8
            fname = f"mohobot_backup_{format_utc8('%Y%m%d_%H%M%S')}.zip"
            logger.info(f"Web panel: 备份完成 ({len(files)} 个文件, 范围={sorted(dir_set)})")
            return FileResponse(
                tmp_path,
                media_type="application/zip",
                filename=fname,
                background=BackgroundTask(_remove_file_background(tmp_path)),
            )

        @app.post("/api/data/restore")
        async def restore_data(request: Request):
            """上传 zip 备份并恢复到选定范围(需再次输入密码)。"""
            await _require_auth(request)
            form = await request.form()
            password = str(form.get("password", ""))
            if not self._verify_password(password, self._password_hash):
                raise HTTPException(status_code=400, detail="密码错误")

            try:
                scope = json.loads(str(form.get("scope", "{}")))
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="scope 无效")
            bot_list, dir_set = _parse_data_scope(scope)

            upload = form.get("file")
            if upload is None:
                raise HTTPException(status_code=400, detail="未上传备份文件")
            content = await upload.read()

            tmp_dir = Path(tempfile.mkdtemp(prefix="mohobot_restore_"))
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    # zip slip 防护: 拒绝包含绝对路径/.. 的成员
                    for member in zf.namelist():
                        target = (tmp_dir / member).resolve()
                        if not str(target).startswith(str(tmp_dir.resolve())) or ".." in member.split("/"):
                            raise HTTPException(status_code=400, detail="备份文件包含非法路径")
                    zf.extractall(tmp_dir)

                restored = self._restore_from(tmp_dir, bot_list, dir_set)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"恢复失败: {e}")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            # 恢复封禁数据后刷新拦截器缓存(否则 60s TTL 内用旧名单)
            if "ban" in dir_set and self._ban_store is not None:
                try:
                    await self._ban_store.clear_banned()
                except Exception as e:
                    logger.warning(f"恢复后刷新封禁缓存失败: {e}")

            logger.info(f"Web panel: 恢复完成 ({restored} 个文件, 范围={sorted(dir_set)})")
            return {"status": "ok", "restored": restored}

        @app.post("/api/data/cleanup")
        async def cleanup_data(request: Request, body: ConfigUpdateRequest):
            """清理选定范围的数据(需再次输入密码)。"""
            await _require_auth(request)
            data = body.data or {}
            password = str(data.get("password", ""))
            if not self._verify_password(password, self._password_hash):
                raise HTTPException(status_code=400, detail="密码错误")
            bot_list, dir_set = _parse_data_scope(data.get("scope", {}))
            removed = self._cleanup_data(bot_list, dir_set)
            # 清理封禁数据后同步刷新拦截器缓存
            if "ban" in dir_set and self._ban_store is not None:
                try:
                    await self._ban_store.clear_banned()
                except Exception as e:
                    logger.warning(f"清理后刷新封禁缓存失败: {e}")
            logger.info(f"Web panel: 清理完成 (移除 {removed} 个文件, 范围={sorted(dir_set)})")
            return {"status": "ok", "removed": removed}

        # ── 9. 封禁管理 (ban) ──────────────────────────────────

        @app.get("/api/ban")
        async def get_ban_list(request: Request):
            """封禁名单全量(面板可视化)。"""
            await _require_auth(request)
            from mohobot.models.config import GlobalConfig
            cfg = GlobalConfig.load(self._config_path)
            data = await self._ban_store.get_all() if self._ban_store else {}
            return {
                "enabled": cfg.ban.enabled,
                "admins": list(cfg.admins),
                "ban": data.get("ban", {}),
                "ban_all": data.get("ban_all", []),
                "pass": data.get("pass", {}),
                "pass_all": data.get("pass_all", []),
            }

        @app.post("/api/ban/operate")
        async def ban_operate(request: Request, body: ConfigUpdateRequest):
            """面板封禁操作: action=ban|ban-all|pass|pass-all|dec-ban|dec-ban-all|dec-pass|dec-pass-all|reset"""
            await _require_auth(request)
            if self._ban_store is None:
                raise HTTPException(status_code=500, detail="封禁存储不可用")
            data = body.data or {}
            action = str(data.get("action", ""))
            uid = str(data.get("uid", "")).strip()
            session_key = str(data.get("session_key", "")).strip() or None
            time_str = str(data.get("time", "") or "0").strip()
            reason = str(data.get("reason", "") or "").strip() or None

            if not uid:
                raise HTTPException(status_code=400, detail="uid 不能为空")
            if action in ("ban", "pass") and not session_key:
                raise HTTPException(status_code=400, detail="会话级操作需要 session_key")

            from mohobot.ban.time_utils import timestr_to_int
            try:
                seconds = timestr_to_int(time_str)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"时间格式错误: {time_str!r}")

            if action == "reset":
                await self._ban_store.reset_user(uid)
            elif action.startswith("dec-"):
                target = action[len("dec-"):]
                ok, err = await self._ban_store.delete(
                    target, uid, session_key=session_key, seconds=seconds, reason=reason,
                )
                if not ok:
                    raise HTTPException(status_code=400, detail=err or "未找到记录")
            elif action in ("ban", "ban-all", "pass", "pass-all"):
                ok = await self._ban_store.upsert(
                    action, uid, session_key=session_key,
                    time_val=seconds, reason=reason,
                )
                if not ok:
                    raise HTTPException(status_code=400, detail="操作失败")
            else:
                raise HTTPException(status_code=400, detail=f"未知操作: {action}")

            logger.info(f"Web panel: ban operate {action} uid={uid} session={session_key or '-'}")
            return {"status": "ok"}

        # ── Static frontend ───────────────────────────────────

        static_dir = Path(__file__).parent / "static"
        static_dir.mkdir(exist_ok=True)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            html_path = static_dir / "index.html"
            if html_path.exists():
                return HTMLResponse(html_path.read_text(encoding="utf-8"))
            return HTMLResponse("<h1>Mohobot Web Panel</h1><p>static/index.html not found</p>")

        @app.get("/api/health")
        async def health(request: Request):
            await _require_auth(request)
            from mohobot.utils.time_utils import now_utc8
            return {"status": "ok", "time": now_utc8().isoformat()}

    # ── 数据管理辅助 ──────────────────────────────────────────

    def _restore_from(
        self, tmp_dir: Path, bot_list: list[str] | None, dir_set: set[str],
    ) -> int:
        """把解压出的备份覆盖到 data 目录(先清空目标再复制)。返回文件数。"""
        restored = 0
        root = self._data_dir

        if "cache" in dir_set:
            src = tmp_dir / "cache"
            dst = root / "cache"
            if src.exists():
                if dst.exists():
                    shutil.rmtree(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dst)
                restored += sum(1 for _ in src.rglob("*") if _.is_file())

        if "ban" in dir_set:
            src = tmp_dir / "ban"
            dst = root / "ban"
            if src.exists():
                if dst.exists():
                    shutil.rmtree(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dst)
                restored += sum(1 for _ in src.rglob("*") if _.is_file())

        for d in ("history", "contexts"):
            if d not in dir_set:
                continue
            src_base = tmp_dir / d
            if not src_base.exists():
                continue
            if bot_list is None:
                src_bots = [e for e in src_base.iterdir() if e.is_dir()]
            else:
                src_bots = [src_base / b for b in bot_list]
            for src_bot in src_bots:
                if not src_bot.exists() or not src_bot.is_dir():
                    continue
                dst_bot = root / d / src_bot.name
                if dst_bot.exists():
                    shutil.rmtree(dst_bot)
                dst_bot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_bot, dst_bot)
                restored += sum(1 for _ in src_bot.rglob("*") if _.is_file())
        return restored

    def _cleanup_data(
        self, bot_list: list[str] | None, dir_set: set[str],
    ) -> int:
        """删除选定范围的数据。返回移除的文件数。"""
        removed = 0
        root = self._data_dir

        if "cache" in dir_set:
            cache_dir = root / "cache"
            if cache_dir.exists():
                for f in cache_dir.rglob("*"):
                    if f.is_file():
                        removed += 1
                shutil.rmtree(cache_dir)
                logger.info("Web panel: cache 已清理")

        if "ban" in dir_set:
            ban_dir = root / "ban"
            if ban_dir.exists():
                removed += sum(1 for _ in ban_dir.rglob("*") if _.is_file())
                shutil.rmtree(ban_dir)
                logger.info("Web panel: ban 已清理")

        for d in ("history", "contexts"):
            if d not in dir_set:
                continue
            base = root / d
            if not base.exists():
                continue
            if bot_list is None:
                bot_dirs = [e for e in base.iterdir() if e.is_dir()]
            else:
                bot_dirs = [base / b for b in bot_list]
            for bot_dir in bot_dirs:
                if not bot_dir.exists() or not bot_dir.is_dir():
                    continue
                removed += sum(1 for _ in bot_dir.rglob("*") if _.is_file())
                shutil.rmtree(bot_dir)
                logger.info(f"Web panel: 已清理 {d}/{bot_dir.name}")
        return removed

    async def _get_system_stats(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "hostname": socket.gethostname(),
            "uptime": time.time() - self._start_time,
        }
        if HAS_PSUTIL:
            # psutil.cpu_percent(interval=...) 同步阻塞, 丢线程池避免卡事件循环
            def _sys():
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage(str(Path.cwd()))
                return {
                    "cpu_percent": psutil.cpu_percent(interval=0.3),
                    "cpu_count": psutil.cpu_count(),
                    "memory": {
                        "total": mem.total,
                        "used": mem.used,
                        "percent": mem.percent,
                    },
                    "disk": {
                        "total": disk.total,
                        "used": disk.used,
                        "percent": disk.percent,
                    },
                }
            import asyncio
            result.update(await asyncio.to_thread(_sys))
        else:
            result["cpu_percent"] = None
            result["memory"] = None
            result["disk"] = None
        return result

    async def _get_framework_stats(self) -> dict[str, Any]:
        """Framework-level stats: message counts from history files.

        history 目录可能很大(数百 MB/数百文件), 同步全量遍历会阻塞事件循环
        导致 WebUI 其他请求排队 — 丢线程池执行 + 结果缓存 60s。
        """
        import asyncio
        now = time.time()
        if self._fw_stats_cache is not None and now - self._fw_stats_cache[0] < 60:
            cached = dict(self._fw_stats_cache[1])
            cached["start_time"] = self._start_time
            cached["uptime"] = now - self._start_time
            if self._bot_manager:
                cached["bot_count"] = self._bot_manager.bot_count
            return cached

        def _count_sync() -> dict[str, int]:
            result: dict[str, int] = {
                "total_messages": 0,
                "history_files": 0,
                "context_files": 0,
            }
            history_dir = self._data_dir / "history"
            if history_dir.exists():
                for bot_dir in history_dir.iterdir():
                    if not bot_dir.is_dir():
                        continue
                    for chat_dir in bot_dir.iterdir():
                        if not chat_dir.is_dir():
                            continue
                        for f in chat_dir.iterdir():
                            if f.suffix == ".jsonl":
                                result["history_files"] += 1
                                try:
                                    # 逐行迭代(内存友好), 大文件也不一次性读入
                                    with open(f, "r", encoding="utf-8") as fh:
                                        result["total_messages"] += sum(
                                            1 for line in fh if line.strip()
                                        )
                                except OSError:
                                    pass
            contexts_dir = self._data_dir / "contexts"
            if contexts_dir.exists():
                for bot_dir in contexts_dir.iterdir():
                    if bot_dir.is_dir():
                        for chat_type in bot_dir.iterdir():
                            if chat_type.is_dir():
                                for user_dir in chat_type.iterdir():
                                    if user_dir.is_dir():
                                        for f in user_dir.iterdir():
                                            if f.suffix == ".json":
                                                result["context_files"] += 1
            return result

        counts = await asyncio.to_thread(_count_sync)
        result = {
            "start_time": self._start_time,
            "uptime": now - self._start_time,
            "total_messages": counts["total_messages"],
            "history_files": counts["history_files"],
            "context_files": counts["context_files"],
            "bot_count": self._bot_manager.bot_count if self._bot_manager else 0,
        }
        self._fw_stats_cache = (now, {
            "total_messages": counts["total_messages"],
            "history_files": counts["history_files"],
            "context_files": counts["context_files"],
        })
        return result

    def _get_unbound_list(self) -> list[dict[str, Any]]:
        """未绑定 bot 的在线连接(接受但不处理消息)。"""
        if not self._bot_manager:
            return []
        return [
            {
                "qq": inst.qq,
                "nickname": inst.nickname,
                "online": True,
                "bound": False,
            }
            for inst in self._bot_manager.unbound_connections
        ]

    async def _get_bot_list(self) -> list[dict[str, Any]]:
        """List bots with online status and message counts."""
        bots = []
        if not self._bot_manager:
            return bots
        for inst in self._bot_manager.all_bots:
            bots.append({
                "bot_id": inst.bot_id,
                "qq": inst.qq,
                "nickname": inst.nickname,
                "online": True,
                "connected_at": inst.connected_at,
                "connected_for": time.time() - inst.connected_at,
                "message_count": inst.message_count,
                "enabled": inst.config.enabled,
            })
        return bots

    async def _get_llm_usage(self) -> dict[str, Any]:
        """LLM token usage stats from the llm service."""
        if self._llm_service:
            try:
                return await self._llm_service.get_usage_stats()
            except Exception as e:
                logger.error(f"Failed to get LLM usage: {e}")
        return {"totals": {"calls": 0, "total_tokens": 0},
                "per_model": {}, "today": {"calls": 0, "total_tokens": 0}}

    # ── Server lifecycle ──────────────────────────────────────

    async def start(self) -> None:
        """Start the uvicorn server."""
        import uvicorn
        logger.info("Starting web panel...")
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._server_instance = server
        # 记录承载 serve() 的任务, 供 stop() 优雅等待排空在途请求
        self._serve_task = asyncio.current_task()
        await server.serve()

    async def stop(self) -> None:
        """Stop the uvicorn server + remove the loguru sink (防重启堆积).

        两阶段关闭:
        1. 优雅阶段: should_exit=True 后等待 serve 任务自行退出——uvicorn
           会先排空(drain)在途请求再关监听, 避免硬杀浏览器在途请求
           (面板重启时曾导致 audit 中间件抛 "No response returned" 噪音);
        2. 兜底阶段: serve 任务已结束但 sockets 仍在(启动竞态: serve()
           "绑定后直接 return" 跳过 shutdown → 端口泄漏), 或优雅等待
           超时(卡死), 才强制关闭 server sockets 保证端口可重绑。
        """
        server = getattr(self, "_server_instance", None)
        if server is not None:
            server.should_exit = True
            serve_task = getattr(self, "_serve_task", None)
            drained = False
            if serve_task is not None and not serve_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(serve_task), timeout=8.0,
                    )
                    drained = True
                except asyncio.TimeoutError:
                    logger.warning(
                        "Web panel graceful shutdown timed out, forcing socket close"
                    )
                except (asyncio.CancelledError, Exception):
                    pass
            if not drained:
                for s in list(getattr(server, "servers", None) or []):
                    try:
                        s.close()
                    except Exception:
                        pass
            logger.info("Web panel stopped")
        # 移除本面板安装的 loguru sink,避免重启后重复收到日志
        sink_id = getattr(self, "_sink_id", None)
        if sink_id is not None:
            from loguru import logger as lg
            try:
                lg.remove(sink_id)
            except Exception:
                pass
            self._sink_id = None