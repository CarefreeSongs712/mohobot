from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from loguru import logger

from mohobot.utils.status_card import (
    AMBER,
    BG,
    DIM,
    FG,
    GREEN,
    RED,
    SEP,
    TRACK,
    format_duration,
    usage_color,
)

_CST = timezone(timedelta(hours=8))

_DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": "http://127.0.0.1:8090",
    "email": "",
    "password": "",
    "timeout_secs": 8,
    "max_systems": 12,
}

_STATUS_META: dict[str, tuple[str, tuple[int, int, int]]] = {
    "up": ("运行中", GREEN),
    "down": ("离线", RED),
    "paused": ("已暂停", DIM),
    "pending": ("等待中", AMBER),
}

_RECORDS_PATH = "/api/collections/systems/records"
_AUTH_PATH = "/api/collections/users/auth-with-password"


class _ConfigSnapshot:
    """One request's immutable Hub credentials and endpoint generation."""

    __slots__ = ("base_url", "email", "password", "timeout_secs", "generation")

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        timeout_secs: float,
        generation: int,
    ) -> None:
        self.base_url = base_url
        self.email = email
        self.password = password
        self.timeout_secs = timeout_secs
        self.generation = generation


class Plugin:
    global_triggers = {"/servers", "/服务器"}
    help_category = "工具"
    info = {
        "commands": [
            {
                "name": "servers",
                "desc": "查看 Beszel 监控的全部服务器状态",
                "usage": "/servers",
                "aliases": ["/服务器"],
            }
        ]
    }

    _ws_server = None

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    def __init__(self) -> None:
        self.plugin_config: dict[str, Any] = dict(_DEFAULT_CONFIG)
        self._clients: dict[tuple[str, float], httpx.AsyncClient] = {}
        self._client_refs: dict[tuple[str, float], int] = {}
        self._active_client_key: tuple[str, float] | None = None
        self._client_lock = asyncio.Lock()
        self._closing = False
        self._token = ""
        self._token_exp = 0.0
        self._token_generation = -1
        self._config_generation = 0
        self._auth_lock = asyncio.Lock()

    def on_config_update(self, config: dict[str, Any]) -> None:
        # Keep in-flight old clients alive; new requests use a new generation
        # and can never reuse the old token. Idle old clients are retired on
        # the next request release (or shutdown) so pools do not grow.
        self.plugin_config = dict(config or {})
        self._config_generation += 1
        self._token = ""
        self._token_exp = 0.0
        self._token_generation = -1
        self._active_client_key = None

    async def on_shutdown(self) -> None:
        # Set the barrier before touching the pool: a query racing shutdown
        # must not create a client after the swap.
        async with self._client_lock:
            self._closing = True
            clients = list(self._clients.values())
            self._clients.clear()
            self._client_refs.clear()
            self._active_client_key = None
        for client in clients:
            try:
                await client.aclose()
            except Exception as e:  # noqa: BLE001 - 关闭失败不影响退出
                logger.warning(f"beszel_servers 关闭 HTTP 客户端失败: {e}")

    async def on_message(self, bot_id: str, event, raw: dict) -> tuple[bool, str | None]:
        text = _extract_text(event)
        if text not in self.global_triggers:
            return False, None

        base_url = str(self.plugin_config.get("base_url") or "").strip()
        email = str(self.plugin_config.get("email") or "").strip()
        password = str(self.plugin_config.get("password") or "")
        if not base_url or not email or not password:
            return True, "Beszel 未配置：请在插件设置里填写 Hub 地址、邮箱与密码"

        try:
            snapshot = self._snapshot()
        except BeszelConfigError as e:
            return True, f"Beszel 配置错误：{e}"

        try:
            systems = await self._fetch_systems(snapshot)
        except Exception as e:  # noqa: BLE001 - 对外返回可读错误
            logger.warning(f"beszel_servers 查询失败: {_safe_error_text(e)}")
            return True, f"Beszel 查询失败：{_friendly_error(e)}"

        if not systems:
            return True, "Beszel 里还没有已接入的服务器"

        limit = min(50, max(1, int(self.plugin_config.get("max_systems") or 12)))
        view = build_view(systems, limit)
        chat_type, chat_id = _event_chat(event)
        ws = self._ws_server
        if ws is not None and chat_type and chat_id:
            image_path = None
            try:
                image_path = await asyncio.to_thread(render_card, view)
                if image_path:
                    await ws.send_image(bot_id, chat_type, chat_id, image_path)
                    return True, None
            except Exception as e:  # noqa: BLE001 - 渲染/发送失败降级文本
                logger.warning(f"beszel_servers 发送图卡失败，改用文本: {e}")
            finally:
                if image_path:
                    try:
                        os.remove(image_path)
                    except OSError:
                        pass
        return True, build_text(view)

    def _snapshot(self) -> _ConfigSnapshot:
        config = self.plugin_config
        try:
            timeout = max(1.0, float(config.get("timeout_secs") or 8))
        except (TypeError, ValueError):
            timeout = 8.0
        return _ConfigSnapshot(
            base_url=_normalize_base_url(str(config.get("base_url") or "")),
            email=str(config.get("email") or "").strip(),
            password=str(config.get("password") or ""),
            timeout_secs=timeout,
            generation=self._config_generation,
        )

    async def _fetch_systems(self, snapshot: _ConfigSnapshot | None = None) -> list[dict[str, Any]]:
        snapshot = snapshot or self._snapshot()
        key, client = await self._acquire_client(snapshot)
        try:
            token = await self._ensure_token(client, snapshot)
            params = {"perPage": "200", "sort": "name", "skipTotal": "1"}
            url = f"{snapshot.base_url}{_RECORDS_PATH}"
            resp = await client.get(url, params=params, headers={"Authorization": token})
            if resp.status_code in (401, 403):
                # Pass the stale token so concurrent 401/403 responses share
                # one refresh instead of each logging in again.
                token = await self._ensure_token(
                    client, snapshot, force=True, stale_token=token
                )
                resp = await client.get(url, params=params, headers={"Authorization": token})
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items") if isinstance(data, dict) else None
            return [item for item in (items or []) if isinstance(item, dict)]
        finally:
            await self._release_client(key)

    async def _acquire_client(
        self, snapshot: _ConfigSnapshot
    ) -> tuple[tuple[str, float], httpx.AsyncClient]:
        key = (snapshot.base_url, snapshot.timeout_secs)
        async with self._client_lock:
            if self._closing:
                raise BeszelUnavailableError("插件正在关闭")
            client = self._clients.get(key)
            if client is None:
                client = httpx.AsyncClient(timeout=snapshot.timeout_secs)
                self._clients[key] = client
                self._client_refs[key] = 0
            self._client_refs[key] = self._client_refs.get(key, 0) + 1
            self._active_client_key = key
            return key, client

    async def _release_client(self, key: tuple[str, float]) -> None:
        to_close: list[httpx.AsyncClient] = []
        async with self._client_lock:
            refs = max(0, self._client_refs.get(key, 0) - 1)
            self._client_refs[key] = refs
            # Retire every idle client except the active one; this also
            # bounds the pool after repeated (base_url, timeout) changes.
            for other, client in list(self._clients.items()):
                if other == self._active_client_key or self._client_refs.get(other, 0) > 0:
                    continue
                self._clients.pop(other, None)
                self._client_refs.pop(other, None)
                to_close.append(client)
        for client in to_close:
            try:
                await client.aclose()
            except Exception as e:  # noqa: BLE001 - 退役连接关闭失败只记日志
                logger.warning(f"beszel_servers 关闭退役 HTTP 客户端失败: {e}")

    async def _ensure_token(
        self,
        client: httpx.AsyncClient,
        snapshot: _ConfigSnapshot,
        *,
        force: bool = False,
        stale_token: str | None = None,
    ) -> str:
        if (
            not force
            and self._token
            and self._token_generation == snapshot.generation
            and time.time() < self._token_exp - 60
        ):
            return self._token
        async with self._auth_lock:
            if not force and self._token and self._token_generation == snapshot.generation:
                # Another concurrent request already refreshed for the same
                # generation: reuse it even if its JWT exp is unknown/zero.
                if self._token_exp <= 0 or time.time() < self._token_exp - 60:
                    return self._token
            if (
                force
                and stale_token is not None
                and self._token
                and self._token_generation == snapshot.generation
                and self._token != stale_token
                and (self._token_exp <= 0 or time.time() < self._token_exp - 60)
            ):
                # A concurrent 401/403 already refreshed for this generation.
                return self._token
            return await self._login(client, snapshot)

    async def _login(self, client: httpx.AsyncClient, snapshot: _ConfigSnapshot) -> str:
        payload = {"identity": snapshot.email, "password": snapshot.password}
        resp = await client.post(f"{snapshot.base_url}{_AUTH_PATH}", json=payload)
        if resp.status_code >= 400:
            raise BeszelAuthError(_response_message(resp))
        data = resp.json()
        token = str(data.get("token") or "")
        if not token:
            raise BeszelAuthError("登录响应缺少 token")
        exp = _jwt_exp(token)
        # Only publish to the shared cache when the configuration is still the
        # same. The in-flight request may still use its local return value, but
        # a later request cannot reuse an old-generation token.
        if self._config_generation == snapshot.generation:
            self._token = token
            self._token_exp = exp if exp > 0 else time.time() + 3600
            self._token_generation = snapshot.generation
        return token


def _normalize_base_url(raw: str) -> str:
    """Validate and normalize the Hub base URL; reject credential/query injection."""
    text = str(raw or "").strip()
    if not text:
        raise BeszelConfigError("Hub 地址为空")
    try:
        url = httpx.URL(text)
    except Exception as e:  # noqa: BLE001 - 非法 URL 统一转为可读配置错误
        raise BeszelConfigError("Hub 地址格式无效") from e
    if url.scheme not in ("http", "https") or not url.host:
        raise BeszelConfigError("Hub 地址必须是 http(s)://主机[:端口]")
    if url.username or url.password:
        raise BeszelConfigError("Hub 地址不能包含用户名或密码")
    if url.query or url.fragment:
        raise BeszelConfigError("Hub 地址不能包含查询串或片段")
    path = url.path.rstrip("/")
    return str(httpx.URL(scheme=url.scheme, host=url.host, port=url.port, path=path))


def _safe_error_text(error: Exception) -> str:
    """Log/display error without leaking URL userinfo or full request URLs."""
    if isinstance(error, httpx.HTTPStatusError):
        url = error.request.url
        return f"HTTP {error.response.status_code} from {url.scheme}://{url.host}"
    text = _friendly_error(error)
    return re.sub(r"(https?://)[^/\s@]+@", r"\1***@", text)


def _extract_text(event: Any) -> str:
    message = getattr(event, "message", "")
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        return "".join(
            seg.get("data", {}).get("text", "")
            for seg in message
            if isinstance(seg, dict) and seg.get("type") == "text"
        ).strip()
    return ""


def _event_chat(event: Any) -> tuple[str, str]:
    from mohobot.models.onebot import GroupMessageEvent

    if isinstance(event, GroupMessageEvent):
        return "group", str(event.group_id)
    return "private", str(getattr(event, "user_id", "") or "")


def _jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return float(data.get("exp") or 0)
    except (IndexError, ValueError, TypeError, binascii.Error):
        return 0.0


def _response_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        message = str(data.get("message") or "")
        if "Failed to authenticate" in message:
            return "邮箱或密码错误"
        if message:
            return message
    return f"HTTP {resp.status_code}"


def _friendly_error(error: Exception) -> str:
    if isinstance(error, BeszelConfigError):
        return str(error)
    if isinstance(error, BeszelUnavailableError):
        return str(error)
    if isinstance(error, BeszelAuthError):
        return f"登录失败（{error}）"
    if isinstance(error, httpx.TimeoutException):
        return "连接超时"
    if isinstance(error, httpx.HTTPStatusError):
        return _response_message(error.response)
    if isinstance(error, httpx.HTTPError):
        return "无法连接 Hub"
    return str(error) or error.__class__.__name__


def _as_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _parse_updated(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_CST)


def build_view(systems: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    ordered = sorted(systems, key=lambda item: str(item.get("name") or ""))
    rows: list[dict[str, Any]] = []
    latest: datetime | None = None
    for item in ordered[:limit]:
        info = item.get("info") if isinstance(item.get("info"), dict) else {}
        load = info.get("la") if isinstance(info.get("la"), list) else []
        updated = _parse_updated(item.get("updated"))
        if updated and (latest is None or updated > latest):
            latest = updated
        status = str(item.get("status") or "unknown")
        label, color = _STATUS_META.get(status, (status or "未知", DIM))
        online = status == "up"
        rows.append(
            {
                "name": str(item.get("name") or item.get("id") or "未命名"),
                "status": status,
                "status_label": label,
                "status_color": color,
                "cpu": _as_float(info.get("cpu")) if online else None,
                "mem": _as_float(info.get("mp")) if online else None,
                "disk": _as_float(info.get("dp")) if online else None,
                "load": [_as_float(v) for v in load[:3]] if online else [],
                "uptime": _as_float(info.get("u")) if online else None,
                "version": str(info.get("v") or "") if online else "",
                "updated": updated,
            }
        )
    return {
        "rows": rows,
        "total": len(systems),
        "hidden": max(0, len(systems) - len(rows)),
        "updated_text": latest.strftime("%m-%d %H:%M") if latest else "",
    }


def build_text(view: dict[str, Any]) -> str:
    rows = view["rows"]
    lines = [f"服务器状态（Beszel · {view['total']} 台）"]
    for row in rows:
        label = row["status_label"]
        if row["status"] != "up":
            lines.append(f"{row['name']}  {label}")
            continue
        parts = [f"CPU {_pct(row['cpu'])}", f"内存 {_pct(row['mem'])}", f"磁盘 {_pct(row['disk'])}"]
        load = next((v for v in row["load"] if v is not None), None)
        if load is not None:
            parts.append(f"负载 {load:.2f}")
        if row["uptime"]:
            parts.append(f"运行 {format_duration(row['uptime'])}")
        if row["version"]:
            parts.append(f"agent {row['version']}")
        lines.append(f"{row['name']}  {label}  " + " · ".join(parts))
    if view["hidden"]:
        lines.append(f"另有 {view['hidden']} 台未显示")
    if view["updated_text"]:
        lines.append(f"数据更新 {view['updated_text']}（UTC+8）")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "--" if value is None else f"{value:.0f}%"


def render_card(view: dict[str, Any]) -> str | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    from mohobot.utils.image_card import find_cjk_font

    font_path = find_cjk_font()
    if not font_path:
        return None

    width = 900
    pad = 28
    header = 96
    row_h = 92
    footer = 34

    def font(size: int) -> Any:
        return ImageFont.truetype(font_path, size)

    f_title = font(28)
    f_sub = font(13)
    f_name = font(21)
    f_status = font(14)
    f_small = font(12)
    f_tiny = font(11)

    rows = view["rows"]
    extra = 24 if view["hidden"] else 0
    height = header + row_h * len(rows) + extra + footer
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    draw.text((pad, 22), "服务器状态", font=f_title, fill=FG)
    count_text = f"Beszel · {view['total']} 台"
    draw.text((width - pad - draw.textlength(count_text, font=f_sub), 34), count_text, font=f_sub, fill=DIM)
    subtitle = f"数据更新 {view['updated_text']}（UTC+8）" if view["updated_text"] else "等待 Hub 上报数据"
    draw.text((pad, 62), subtitle, font=f_sub, fill=DIM)

    for index, row in enumerate(rows):
        top = header + index * row_h
        if index:
            draw.line((pad, top, width - pad, top), fill=SEP)

        dot_color = row["status_color"]
        draw.ellipse((pad + 2, top + 22, pad + 12, top + 32), fill=dot_color)
        draw.text((pad + 20, top + 10), _fit(row["name"], draw, f_name, 420), font=f_name, fill=FG)

        status_text = row["status_label"]
        draw.text(
            (width - pad - draw.textlength(status_text, font=f_status), top + 16),
            status_text,
            font=f_status,
            fill=dot_color,
        )

        for slot, (label, value) in enumerate(
            (("CPU", row["cpu"]), ("MEM", row["mem"]), ("DISK", row["disk"]))
        ):
            bx = pad + 20 + slot * 210
            value_text = _pct(value)
            color = usage_color(value) if value is not None else TRACK
            draw.text((bx, top + 42), f"{label} {value_text}", font=f_small, fill=DIM)
            bar_x = bx + 62
            bar_y = top + 48
            draw.rounded_rectangle((bar_x, bar_y, bar_x + 120, bar_y + 6), radius=3, fill=TRACK)
            if value is not None:
                fill_w = int(120 * max(0.0, min(100.0, value)) / 100)
                if fill_w > 0:
                    draw.rounded_rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + 6), radius=3, fill=color)

        meta_parts = []
        load = next((v for v in row["load"] if v is not None), None)
        if row["status"] == "up":
            if load is not None:
                meta_parts.append(f"负载 {load:.2f}")
            if row["uptime"]:
                meta_parts.append(f"运行 {format_duration(row['uptime'])}")
            if row["version"]:
                meta_parts.append(f"agent {row['version']}")
        else:
            if row["updated"]:
                meta_parts.append(f"最后上报 {row['updated'].strftime('%m-%d %H:%M')}")
            if row["version"]:
                meta_parts.append(f"agent {row['version']}")
        if meta_parts:
            draw.text(
                (pad + 20, top + 66),
                _fit(" · ".join(meta_parts), draw, f_small, width - 2 * pad - 20),
                font=f_small,
                fill=DIM,
            )

    if view["hidden"]:
        draw.text((pad, header + row_h * len(rows) + 4), f"另有 {view['hidden']} 台未显示", font=f_small, fill=DIM)

    footer_text = "来源：Beszel Hub systems 集合"
    draw.text((pad, height - footer + 8), footer_text, font=f_tiny, fill=(110, 118, 132))

    fd, path = tempfile.mkstemp(prefix="beszel-servers-", suffix=".png")
    os.close(fd)
    try:
        image.save(path, format="PNG")
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return path


def _fit(text: str, draw: Any, font: Any, max_width: float) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and draw.textlength(trimmed + ellipsis, font=font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ellipsis


class BeszelAuthError(RuntimeError):
    pass


class BeszelConfigError(RuntimeError):
    pass


class BeszelUnavailableError(RuntimeError):
    pass
