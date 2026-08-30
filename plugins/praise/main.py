"""Mohobot 点赞插件 — 发送"赞我"/"/赞我"/"zanwo"/"/zanwo"时给用户点赞。

OneBot v11 API: send_like (user_id, times) — 每个好友每天最多 10 次。
点赞总数/每次点数/回复消息模板均可在 WebUI 插件配置中调整(_conf_schema.json)。
"""

from __future__ import annotations

import asyncio
import random
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
    # TaskSupervisor injected by PluginSystem(每日点赞后台任务统一管理)
    _task_supervisor = None

    # 每日定时点赞: 周期任务(60s)检查, 到点后由后台任务执行(带随机延时防风控)
    interval_sec = 60
    _daily_done_date: str = ""   # "YYYY-MM-DD", 每天(每次进程启动后)只执行一次

    # 当日点赞上限缓存: f"{bot_id}:{user_id}" -> "YYYY-MM-DD"
    # 同一好友当天达到上限后不再调用 API(QQ 每日最多 10 赞/好友)
    _like_limit_cache: dict[str, str] = {}

    # 判定"已达上限"的错误关键词(平台返回)
    _LIMIT_HINTS = ("频繁", "上限", "过快", "too many", "频繁操作", "操作过快")

    # 默认配置(_conf_schema.json 注入覆盖; 无 schema 时的兜底)
    _DEFAULTS = {
        "like_total": 20,
        "like_times_per_call": 10,
        "success_msg": "✅ 已给你点了 {count} 个赞~",
        "fail_msg": "❌ 点赞失败: {detail}",
        "limit_msg": "😴 今天已经给你点过很多赞啦,QQ 每日点赞有上限,明天再来吧~",
        # 每日定时点赞
        "daily_like_enabled": True,
        "daily_like_time": "08:00",
        "daily_admin_qq": 3831097597,
        "daily_like_times": 10,
        "daily_min_delay": 10,
        "daily_max_delay": 30,
    }

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        """Inject the WS server for API calls (called from main.py)."""
        cls._ws_server = ws_server

    @classmethod
    def inject_task_supervisor(cls, supervisor) -> None:
        cls._task_supervisor = supervisor

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

    # ── 每日定时点赞 ──────────────────────────────────────────

    async def on_tick(self) -> None:
        """周期任务(60s): 到达配置时间(默认 08:00)后, 当天首次触发每日点赞。"""
        from mohobot.utils.time_utils import format_utc8
        await self._maybe_daily(format_utc8("%Y-%m-%d"), format_utc8("%H:%M"))

    async def _maybe_daily(self, today: str, hhmm: str) -> None:
        """到点触发(当天仅一次); 立即标记防 tick 期间重复, 后台执行。"""
        if not bool(self._cfg("daily_like_enabled", True)):
            return
        if Plugin._daily_done_date == today:
            return
        trigger_time = str(self._cfg("daily_like_time", "08:00"))
        if hhmm < trigger_time:
            return
        Plugin._daily_done_date = today
        logger.info(f"每日定时点赞触发({today} {hhmm}), 后台执行中...")
        coro = self._run_daily_likes()
        if self._task_supervisor is not None:
            self._daily_task = self._task_supervisor.create_task(
                coro, name="praise-daily-likes", owner="plugins"
            )
        else:
            self._daily_task = asyncio.create_task(coro)

    async def _run_daily_likes(self) -> None:
        """全体 bot 点赞: 管理员 + bot 互相点赞(自己不点自己), 随机延时防风控。"""
        ws = self._ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        if ws is None or bm is None:
            logger.warning("每日点赞跳过: ws/bot_manager 未注入")
            return
        bots = [
            (b.bot_id, int(b.config.qq))
            for b in bm.all_bots
            if b.bound and b.bot_id and b.config.qq
        ]
        if not bots:
            logger.warning("每日点赞跳过: 无在线 bot")
            return
        admin_qq = int(self._cfg("daily_admin_qq", 0) or 0)
        times = max(1, min(10, int(self._cfg("daily_like_times", 10))))
        lo = max(0.0, float(self._cfg("daily_min_delay", 10)))
        hi = max(lo, float(self._cfg("daily_max_delay", 30)))

        # 目标组合: 每个 bot 点管理员(非自己) + bot 互赞(自己不点自己)
        pairs: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int]] = set()
        for bot_id, bot_qq in bots:
            targets = [(admin_qq, "管理员")] if admin_qq else []
            targets += [(q, f"bot:{oid}") for oid, q in bots if q != bot_qq]
            for target_qq, label in targets:
                key = (bot_id, target_qq)
                if target_qq and key not in seen:
                    seen.add(key)
                    pairs.append((bot_id, target_qq, label))

        logger.info(f"每日点赞开始: {len(bots)} bot, 共 {len(pairs)} 组点赞")
        ok = failed = 0
        for i, (bot_id, target_qq, label) in enumerate(pairs):
            try:
                resp = await ws.send_to_bot(
                    bot_id, "send_like",
                    {"user_id": int(target_qq), "times": times},
                    wait_response=True, timeout=10.0,
                )
                if resp and resp.get("status") == "ok" and resp.get("retcode") == 0:
                    ok += 1
                else:
                    failed += 1
                    wording = (resp or {}).get("wording") or (resp or {}).get("message") or "无响应/未知错误"
                    logger.warning(f"每日点赞失败({bot_id} → {label} {target_qq}): {wording}")
            except Exception as e:
                failed += 1
                logger.warning(f"每日点赞异常({bot_id} → {label} {target_qq}): {e}")
            if i < len(pairs) - 1:
                await asyncio.sleep(random.uniform(lo, hi))
        logger.info(f"每日点赞完成: 成功 {ok}, 失败 {failed}, 共 {len(pairs)} 组")

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
