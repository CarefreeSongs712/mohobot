"""Mohobot 点赞插件 — 发送"赞我"/"/赞我"/"zanwo"/"/zanwo"时给用户点赞。

OneBot v11 API: send_like (user_id, times) — 每个好友每天最多 10 次。
点赞总数/每次点数/回复消息模板均可在 WebUI 插件配置中调整(_conf_schema.json)。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from loguru import logger

from mohobot.history_dedup import message_signature

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

    # 一分钟全局节流: 用户 QQ -> 上次触发时间戳(类级 = 跨 bot/群/私聊全局)。
    # 触发即占用(无论点赞成败), 60s 内再次触发直接提示, 不调 API。
    _THROTTLE_WINDOW = 60.0
    _throttle_cache: dict[int, float] = {}

    # 同一消息事件的提示去重: 跨账号稳定事件键 -> 占用时间戳。
    # 每条消息上提示语只出一次(由先到的 bot 出, 其余静默); 点赞本身不做
    # 主权声明 —— 每个 bot 都以自己的身份点赞并汇报各自结果。
    _event_owner_cache: dict[Any, float] = {}

    @staticmethod
    def _event_key(raw_event: dict) -> Any:
        """跨账号稳定的事件身份(用于提示去重与兄弟 bot 判定)。

        NapCat 等实现的 ``message_id`` 按账号分配: 同一条群消息经不同 bot
        推送时 id 不同, 不能只靠它去重。优先用发送者/时间/消息内容指纹;
        拿不到消息段时退回 message_id(非标准事件), 都没有则返回 None。
        """
        try:
            message = raw_event.get("message")
        except AttributeError:
            return None
        if message is not None:
            return (
                str(raw_event.get("group_id") or raw_event.get("user_id") or ""),
                str(raw_event.get("user_id") or ""),
                str(raw_event.get("time") or ""),
                message_signature(message, strict=True),
            )
        try:
            mid = str(raw_event.get("message_id") or "")
        except AttributeError:
            return None
        return ("mid", mid) if mid else None

    @classmethod
    def _claim_event(cls, raw_event: dict) -> bool:
        """声明本条消息事件提示类的唯一发言权; 已被占用的事件键返回 False。"""
        key = cls._event_key(raw_event)
        if key is None:
            return True  # 拿不到稳定身份时保持旧行为
        now = time.time()
        stale = [k for k, ts in cls._event_owner_cache.items() if now - ts > 120]
        for k in stale:
            del cls._event_owner_cache[k]
        if key in cls._event_owner_cache:
            return False
        cls._event_owner_cache[key] = now
        return True

    @staticmethod
    def _set_throttle(user_id: int, event_key: Any) -> None:
        """写入节流窗口(带跨账号事件键, 用于区分"同一条消息的兄弟 bot")。"""
        Plugin._throttle_cache[int(user_id)] = {
            "ts": time.time(), "id": event_key,
        }

    # 默认配置(_conf_schema.json 注入覆盖; 无 schema 时的兜底)
    _DEFAULTS = {
        "like_total": 20,
        "like_times_per_call": 10,
        "pending_msg": "正在点赞中，一次点赞就是一天的量，请勿重复点赞",
        "success_msg": "✅ 已给你点了 {count} 个赞~",
        "fail_msg": "❌ 点赞失败: {detail}",
        "limit_msg": "😴 今天已经给你点过很多赞啦,QQ 每日点赞有上限,明天再来吧~",
        "throttle_msg": "bot一次点赞就会把所有的赞点完，不需要发送多次。反复点赞可能导致bot风控",
        # 每日定时点赞
        "daily_like_enabled": True,
        "daily_like_time": "08:00",
        "daily_admin_qq": 3831097597,
        "daily_like_users": [3831097597, 2011372693, 858120206, 1341586492,
                             1489388902, 2640535972],
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

    async def _send_pending_notice(self, bot_id: str, event: Any, raw_event: dict) -> None:
        """在真正调用点赞 API 前立刻发一句提示(同一条消息只由一个 bot 发)。

        直接走 WSServer 发送而不是返回给框架, 因为后续点赞要等 API 回包,
        返回式回复只能等全部批次结束后才发出。发送失败不影响点赞流程。
        """
        cfg = getattr(self, "plugin_config", None) or {}
        # 这里不走 _cfg: 它把空串当作"未配置"回退默认值, 而本项需要支持留空关闭。
        text = str(cfg.get("pending_msg", self._DEFAULTS["pending_msg"]) or "").strip()
        ws = self._ws_server
        if not text or ws is None:
            return
        if not self._claim_event(raw_event):
            return
        # 同一条消息只提示一次: 即使本 bot 的 send_like 全部失败, 其它 bot 仍会
        # 各自点赞并汇报结果; 失败后释放 claim 只会让兄弟 bot 重复刷提示。
        try:
            if getattr(event, "message_type", "") == "group":
                sender = getattr(ws, "send_group_msg", None)
                group_id = getattr(event, "group_id", None)
                if sender is not None and group_id is not None:
                    await sender(bot_id, group_id, text)
            else:
                sender = getattr(ws, "send_private_msg", None)
                user_id = getattr(event, "user_id", None)
                if sender is not None and user_id is not None:
                    await sender(bot_id, user_id, text)
        except Exception as e:  # noqa: BLE001 - 提示失败不应阻断点赞
            logger.debug(f"Praise pending notice failed: {e}")

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

        # 目标组合: 每个 bot 点管理员(非自己) + 其它 bot(自己不点自己)
        #           + 配置的额外名单(非自己; 存档经 schema 强转可能是字符串)
        extra: list[tuple[int, str]] = []
        for u in (self._cfg("daily_like_users", []) or []):
            try:
                u_qq = int(str(u).strip())
            except (TypeError, ValueError):
                continue
            if u_qq:
                extra.append((u_qq, f"名单:{u_qq}"))

        pairs: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int]] = set()
        for bot_id, bot_qq in bots:
            targets = [(admin_qq, "管理员")] if admin_qq else []
            targets += [(q, f"bot:{oid}") for oid, q in bots if q != bot_qq]
            targets += [(q, label) for q, label in extra if q != bot_qq]
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

        # 一分钟全局节流(触发即占用窗口, 成败与否):
        # - 窗口内的新消息 → 提示语每条消息只由一个 bot 出一次, 其余静默;
        # - 窗口内的同一条消息(兄弟 bot) → 继续执行点赞, 每个 bot 都要赞;
        # - 无窗口/已过期 → 点赞。
        throttle_msg = self._cfg("throttle_msg", self._DEFAULTS["throttle_msg"])
        now_ts = time.time()
        event_key = self._event_key(raw_event)
        entry = Plugin._throttle_cache.get(int(user_id))
        if entry is not None and now_ts - entry["ts"] < Plugin._THROTTLE_WINDOW:
            if entry.get("id") != event_key:
                if self._claim_event(raw_event):
                    logger.debug(f"Praise throttled(new msg): {user_id} (retry in "
                                 f"{Plugin._THROTTLE_WINDOW - (now_ts - entry['ts']):.0f}s)")
                    return (True, throttle_msg)
                logger.debug(f"Praise throttled silent: {user_id} (hint claimed)")
                return (True, None)
            # 同一条消息: 兄弟 bot 放行去点赞
        else:
            # 顺手清理过期条目, 防长驻进程下缓存膨胀
            expired = [k for k, v in Plugin._throttle_cache.items()
                       if time.time() - v["ts"] > 3600]
            for k in expired:
                del Plugin._throttle_cache[k]

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
            self._set_throttle(int(user_id), event_key)
            return (True, limit_msg)

        # 真正开始点赞前先立刻提示一次(只由本条消息的首个 bot 发);
        # 随后的成功/失败汇报保持原有返回式回复, 不改变既有形态。
        await self._send_pending_notice(bot_id, event, raw_event)

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
            self._set_throttle(int(user_id), event_key)
            return (True, f"❌ 点赞失败: {e}")

        if errors:
            detail = "；".join(errors)
            logger.warning(f"Praise failed for {user_id}: {detail}")
            # 平台拒绝(如已达上限) → 缓存当天, 之后不再重复调用 API
            if any(hint in detail for hint in self._LIMIT_HINTS):
                self._mark_like_limit(cache_key, today)
            self._set_throttle(int(user_id), event_key)
            return (True, self._render(fail_msg, detail=detail))
        total_sent = ok_count * times_per_call
        self._set_throttle(int(user_id), event_key)
        return (True, self._render(success_msg, count=total_sent))
