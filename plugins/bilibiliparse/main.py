"""Mohobot 哔哩哔哩视频解析插件 — 迁移自 astrbot_plugin_bilibiliParse (7Hello12)。

自动检测消息中的 B 站视频链接(www.bilibili.com/video/BVxxx 或 av123):
- 群聊不 @ 也解析(观察钩子, gate 前); 私聊直接解析
- 多 bot 群内: 仅 bot_id 最小者解析回复, 其余 bot 静默消费含链接消息
  (不回复也不落 LLM, 保证只有一个 bot 处理)
- 回复: PIL 深色信息卡片图片(标题/链接/清晰度/大小), 渲染或发送失败降级为文本
- 解析 API: 配置项 api_url(默认 http://114.134.188.188:3003), accept 清晰度可配
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from loguru import logger

try:
    import aiohttp
except ImportError:
    aiohttp = None

BILI_VIDEO_PATTERN = r"(https?:\/\/)?www\.bilibili\.com\/video\/(BV\w+|av\d+)\/?"

# 清晰度参数映射(accept): 1080P=80 / 720P=64 / 480P=32 / 360P=16
_QUALITY_HINTS = {"80": "1080P", "64": "720P", "32": "480P", "16": "360P"}


class Plugin:
    """B 站视频链接自动解析: 发链接 → 信息卡片图片。"""

    info = {
        "commands": [
            {"name": "(链接自动识别)", "desc": "消息中含 B 站视频链接(BV/av)时自动解析并返回信息卡片"},
        ],
    }

    # WS server injected by main.py via inject_ws_server() classmethod
    _ws_server = None

    _DEFAULTS = {
        "api_url": "http://114.134.188.188:3003",
        "accept_quality": 80,
    }

    def __init__(self):
        # 插件配置由框架注入(_conf_schema.json), 缺失时回退默认
        self.plugin_config: dict = dict(self._DEFAULTS)
        self._http_session: Any = None

    # ── 框架注入 ─────────────────────────────────────────────

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    # ── 内部工具 ─────────────────────────────────────────────

    def _cfg(self, key: str, default):
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None and value != "" else default

    def _http(self) -> Any:
        if aiohttp is None:
            raise RuntimeError("aiohttp 未安装, 无法访问解析 API")
        if self._http_session is None or getattr(self._http_session, "closed", False):
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._http_session

    @staticmethod
    def _extract_text(event: Any) -> str:
        if isinstance(event.message, str):
            return event.message.strip()
        text = ""
        if isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
        return text.strip()

    @staticmethod
    def _chat_of(event: Any) -> tuple[str, str]:
        from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent
        if isinstance(event, GroupMessageEvent):
            return ("group", str(event.group_id))
        return ("private", str(event.user_id))

    @staticmethod
    def _fmt_size(size) -> str:
        """字节 → 可读大小。"""
        try:
            size = float(size)
        except (TypeError, ValueError):
            return str(size)
        units = ["B", "KB", "MB", "GB", "TB"]
        index = 0
        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1
        return f"{size:.2f} {units[index]}"

    # ── 解析 API ─────────────────────────────────────────────

    async def _parse(self, bvid: str, accept: int) -> dict:
        """调用解析 API, 返回 {code, msg, title, video_url, video_size, quality}。"""
        api_url = self._cfg("api_url", "http://114.134.188.188:3003").rstrip("/")
        url = f"{api_url}/api?bvid={urllib.parse.quote(bvid)}&accept={int(accept)}"
        try:
            async with self._http().get(url) as r:
                r.raise_for_status()
                data = await r.json()
        except Exception as e:
            logger.error(f"B 站解析 API 请求失败: {e}")
            return {"code": "-1", "msg": "请求解析服务失败,请稍后再试"}

        if not isinstance(data, dict) or data.get("code") != 0:
            return {"code": "-1", "msg": "解析失败,视频可能不存在或链接不正确"}
        item = (data.get("data") or [{}])[0]
        return {
            "code": 0,
            "msg": "视频解析成功",
            "title": data.get("title", ""),
            "video_url": item.get("video_url", ""),
            "video_size": item.get("video_size", ""),
            "quality": item.get("accept_format", ""),
        }

    # ── 观察钩子(gate 前, 群聊不 @ 也触发) ─────────────────

    async def on_message_observed(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """检测消息中的 B 站视频链接并解析。"""
        text = self._extract_text(event)
        match = re.search(BILI_VIDEO_PATTERN, text)
        if not match:
            return (False, None)

        # 群聊多 bot: 仅最小 bot 解析回复; 其余 bot 静默消费(不回复, 不落 LLM)
        from mohobot.models.onebot import GroupMessageEvent
        if isinstance(event, GroupMessageEvent):
            ws = self._ws_server
            if ws is not None and getattr(ws, "_bot_manager", None) is not None:
                min_bot = ws._bot_manager.min_bot_for_group(str(event.group_id))
                if min_bot is not None and min_bot != bot_id:
                    return (True, None)

        bvid = match.group(2)
        info = await self._parse(bvid, int(self._cfg("accept_quality", 80)))
        if info.get("code") != 0:
            return (True, info.get("msg", "解析失败"))

        # 渲染信息卡片图片; 失败降级为文本
        card_path = self._render_card(info)
        if card_path is not None:
            try:
                chat_type, chat_id = self._chat_of(event)
                await self._ws_server.send_image(bot_id, chat_type, chat_id, card_path)
                import os
                os.remove(card_path)
                return (True, None)
            except Exception as e:
                logger.warning(f"发送 B 站信息卡片失败, 降级为文本: {e}")
                try:
                    import os
                    os.remove(card_path)
                except OSError:
                    pass

        lines = [
            f"🎬 标题: {info.get('title', '')}",
            f"🔗 视频链接: {info.get('video_url', '')}",
            f"📖 视频大小: {self._fmt_size(info.get('video_size', ''))}",
            f"👓 清晰度: {info.get('quality', '')}",
        ]
        return (True, "\n".join(lines))

    # ── 信息卡片渲染 ─────────────────────────────────────────

    def _render_card(self, info: dict) -> str | None:
        """把解析结果渲染成深色信息卡片; 无 PIL/字体时返回 None。"""
        from mohobot.utils.image_card import render_info_card

        quality = str(info.get("quality", ""))
        accept = str(self._cfg("accept_quality", 80))
        # API 返回的 accept_format 通常是 "1080P 高清" 之类, 直接展示
        fields = [
            ("标题", info.get("title", "")),
            ("链接", info.get("video_url", "")),
            ("清晰度", quality or _QUALITY_HINTS.get(accept, accept)),
            ("大小", self._fmt_size(info.get("video_size", ""))),
        ]
        return render_info_card("B站视频解析", fields)
