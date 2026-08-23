"""Mohobot 点赞插件 — 发送"赞我"/"/赞我"/"zanwo"/"/zanwo"时给用户点赞。

OneBot v11 API: send_like (user_id, times) — 每个好友每天最多 10 次。
点赞总数/每次点数/回复消息模板均可在 WebUI 插件配置中调整(_conf_schema.json)。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

PRAISE_TRIGGERS = {"赞我", "/赞我", "zanwo", "/zanwo"}


class Plugin:
    """Responds to praise requests by sending likes via send_like API."""

    info = {
        "commands": [
            {"name": "赞我", "desc": "给自己点赞(数量可在插件配置中调整)"},
        ],
    }

    # 无前缀触发: 群聊不 @ 直接发"赞我/zanwo"也可触发(框架观察钩子精确匹配)
    no_prefix_triggers = {"赞我", "zanwo"}

    # WS server injected by main.py via inject_ws_server() classmethod
    _ws_server = None

    # 当日点赞上限缓存: f"{bot_id}:{user_id}" -> "YYYY-MM-DD"
    # 同一好友当天达到上限后不再调用 API(QQ 每日最多 10 赞/好友)
    _like_limit_cache: dict[str, str] = {}

    # 判定"已达上限"的错误关键词(平台返回)
    _LIMIT_HINTS = ("频繁", "上限", "过快", "too many", "频繁操作", "操作过快")

    # 默认配置(_conf_schema.json 注入覆盖; 无 schema 时的兜底)
    _DEFAULTS = {
        "like_total": 20,
        "like_times_per_call": 10,
        "success_msg": "✅ 已给你点了 {count} 个赞,去名片看看吧~",
        "fail_msg": "❌ 点赞失败: {detail}",
        "limit_msg": "😴 今天已经给你点过很多赞啦,QQ 每日点赞有上限,明天再来吧~",
    }

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        """Inject the WS server for API calls (called from main.py)."""
        cls._ws_server = ws_server

    @staticmethod
    def _mark_like_limit(cache_key: str, today: str) -> None:
        """记录当日已达上限(跨天自动清理旧条目)。"""
        for k in [k for k, v in Plugin._like_limit_cache.items() if v != today]:
            del Plugin._like_limit_cache[k]
        Plugin._like_limit_cache[cache_key] = today

    def _cfg(self, key: str, default):
        """读取插件配置(WebUI 保存后热生效), 缺失/类型异常时回退默认值。"""
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None and value != "" else default

    def _render(self, template: str, **kwargs) -> str:
        """模板占位符替换(仅 {key} 形式, 避免 format 对花括号敏感)。"""
        text = template
        for key, val in kwargs.items():
            text = text.replace("{" + key + "}", str(val))
        return text

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Check for praise trigger and send likes."""
        # Extract plain text
        text = ""
        if isinstance(event.message, str):
            text = event.message.strip()
        elif isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
            text = text.strip()

        if text not in PRAISE_TRIGGERS:
            return (False, None)

        user_id = event.user_id
        logger.info(f"Praise request from {user_id} (bot {bot_id})")

        ws_server = self._ws_server
        if ws_server is None:
            return (True, "点赞服务未配置,无法发送点赞。")

        like_total = max(1, int(self._cfg("like_total", 20)))
        times_per_call = max(1, min(10, int(self._cfg("like_times_per_call", 10))))
        limit_msg = self._cfg("limit_msg", self._DEFAULTS["limit_msg"])
        success_msg = self._cfg("success_msg", self._DEFAULTS["success_msg"])
        fail_msg = self._cfg("fail_msg", self._DEFAULTS["fail_msg"])

        from mohobot.utils.time_utils import format_utc8
        today = format_utc8("%Y-%m-%d")
        cache_key = f"{bot_id}:{user_id}"
        # 当日已达上限 → 直接提示, 不再调用 API
        if self._like_limit_cache.get(cache_key) == today:
            logger.debug(f"Praise daily limit cached: {user_id}")
            return (True, limit_msg)

        # 按配置的每次点数分批调用(QQ 单次上限 10)。
        # Wait for each response and report the REAL result — do not claim
        # success when the platform rejects it (e.g. daily like limit reached).
        batches = max(1, -(-like_total // times_per_call))  # ceil
        errors: list[str] = []
        ok_count = 0
        try:
            for i in range(batches):
                resp = await ws_server.send_to_bot(
                    bot_id, "send_like",
                    {"user_id": int(user_id), "times": times_per_call},
                    wait_response=True,
                    timeout=5.0,
                )
                if resp is None:
                    errors.append(f"第 {i + 1} 次无响应(超时)")
                elif resp.get("status") != "ok" or resp.get("retcode") != 0:
                    wording = resp.get("wording") or resp.get("message") or "未知错误"
                    errors.append(f"第 {i + 1} 次失败: {wording}")
                    # First failure already tells the story (e.g. daily limit) —
                    # no point sending the next batch.
                    break
                else:
                    ok_count += 1
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"send_like failed: {e}")
            return (True, f"❌ 点赞失败: {e}")

        if errors:
            detail = "；".join(errors)
            logger.warning(f"Praise failed for {user_id}: {detail}")
            # 平台拒绝(如已达上限) → 缓存当天, 之后不再重复调用 API
            if any(hint in detail for hint in self._LIMIT_HINTS):
                self._mark_like_limit(cache_key, today)
            return (True, self._render(fail_msg, detail=detail))
        total_sent = ok_count * times_per_call
        return (True, self._render(success_msg, count=total_sent))
