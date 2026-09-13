"""QQ 空间登录态管理 — mohobot 多 bot 适配版。

与原版(astrbot_plugin_qzone_lite)差异:
- 每个 bot 一份登录态(bot_id 隔离), 发/删说说必须用各 bot 自己的身份。
- Cookies 完全自动获取: 通过该 bot 的 OneBot 连接调 get_cookies(domain) API,
  并持久化缓存到 data/plugins_data/qzone/cookies_{bot_id}.json(协议端不在线时复用)。
- 不再提供手动 cookies_str 配置。
"""

from __future__ import annotations

import asyncio
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from mohobot.file_store import json_read, json_write

from .model import QzoneContext


class QzoneSession:
    DOMAIN = "user.qzone.qq.com"

    def __init__(
        self,
        *,
        bot_id: str,
        ws_server: Any,
        data_dir: str,
        cfg_get: Callable[[str, Any], Any],
    ):
        self.bot_id = bot_id
        self._ws = ws_server
        self._data_dir = Path(data_dir)
        self._cfg_get = cfg_get
        self._ctx: QzoneContext | None = None
        self._lock = asyncio.Lock()

    # ── Cookies 持久化 ────────────────────────────────────────

    def _cookies_path(self) -> Path:
        return self._data_dir / "plugins_data" / "qzone" / f"cookies_{self.bot_id}.json"

    async def _load_cached_cookies(self) -> str:
        data = await json_read(self._cookies_path())
        if isinstance(data, dict):
            return str(data.get("cookies") or "")
        return ""

    async def _save_cookies(self, cookies_str: str) -> None:
        await json_write(
            self._cookies_path(),
            {"cookies": cookies_str, "saved_at": int(time.time())},
        )

    async def _clear_cookies(self) -> None:
        try:
            self._cookies_path().unlink()
        except OSError:
            pass

    # ── 登录 ──────────────────────────────────────────────────

    async def _fetch_cookies_from_bot(self) -> str:
        """通过该 bot 的 OneBot 连接获取 QQ 空间域 Cookies。"""
        if self._ws is None:
            raise RuntimeError("WS 服务未注入, 无法获取 Cookies")
        resp = await self._ws.send_to_bot(
            self.bot_id, "get_cookies", {"domain": self.DOMAIN},
            wait_response=True, timeout=10.0,
        )
        data = (resp or {}).get("data") or {}
        cookies = data.get("cookies") or ""
        if isinstance(cookies, dict):
            cookies = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if not cookies:
            raise RuntimeError("协议端未返回 Cookies(get_cookies 失败)")
        return cookies

    @staticmethod
    def _build_ctx(cookies_str: str) -> QzoneContext:
        try:
            c = {k: v.value for k, v in SimpleCookie(cookies_str).items()}
        except Exception as e:
            raise RuntimeError(f"Cookies 解析失败: {e}") from e
        uin_raw = str(c.get("uin", "") or "").lstrip("oO")
        try:
            uin = int(uin_raw)
        except ValueError:
            uin = 0
        if not uin:
            raise RuntimeError("Cookie 中缺少合法 uin")
        return QzoneContext(
            uin=uin,
            skey=c.get("skey", ""),
            p_skey=c.get("p_skey", ""),
            raw_cookies=c,
        )

    async def get_ctx(self) -> QzoneContext:
        """获取登录态: 内存 → 磁盘缓存 → 协议端自动获取(逐级回退)。"""
        async with self._lock:
            if not self._ctx:
                cookies_str = await self._load_cached_cookies()
                if not cookies_str:
                    logger.info(f"[{self.bot_id}] 正在通过协议端登录 QQ 空间")
                    cookies_str = await self._fetch_cookies_from_bot()
                    await self._save_cookies(cookies_str)
                self._ctx = self._build_ctx(cookies_str)
                logger.info(f"[{self.bot_id}] QQ 空间登录成功, uin={self._ctx.uin}")
            return self._ctx

    async def get_uin(self) -> int:
        ctx = await self.get_ctx()
        return ctx.uin

    async def get_nickname(self) -> str:
        """bot 昵称(取 bot 配置, 取不到回退 uin)。"""
        bm = getattr(self._ws, "_bot_manager", None) if self._ws is not None else None
        inst = bm.get(self.bot_id) if bm is not None else None
        if inst is not None:
            nickname = (getattr(inst.config, "nickname", "") or "").strip()
            if nickname:
                return nickname
        try:
            return str(await self.get_uin())
        except Exception:
            return self.bot_id

    async def invalidate(self) -> None:
        async with self._lock:
            self._ctx = None

    async def reset_login_state(self, *, clear_cookies: bool = False) -> None:
        """清空登录态(可选同时清除磁盘 Cookies 缓存)。"""
        async with self._lock:
            self._ctx = None
            if clear_cookies:
                await self._clear_cookies()

    async def relogin(self) -> QzoneContext:
        """登录失效后重置并重新获取(由 HttpClient 调用)。"""
        await self.reset_login_state(
            clear_cookies=bool(self._cfg_get("auto_reset_on_login_expired", True)),
        )
        return await self.get_ctx()
