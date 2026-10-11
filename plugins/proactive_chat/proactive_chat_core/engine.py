"""私聊主动聊天引擎: 状态持久化、随机窗口调度与主动消息生成发送全流程。

机制移植自 astrbot_plugin_proactive_chat (AGPL-3.0), 适配 mohobot 管线:
- 会话键为 "{bot_id}:{user_id}", 自动跟踪全部私聊(排除名单兜底);
- 调度用 on_tick 轮询持久化的 next_trigger_time, 不引入 APScheduler,
  重启恢复由"到期任务仍在状态文件里"自然保证;
- 生成走 llm_service.chat(合成私聊事件), 发送走 message_handler
  的 send_proactive_reply(与被动回复同形态), 上下文回写只追加 assistant 一条。
"""
from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from mohobot.file_store import json_update
from mohobot.models.onebot import PrivateMessageEvent, Sender

from .schedule import is_quiet_time, random_interval_seconds, render_prompt

# 重启恢复容忍期(秒): 到期未超过该时长的任务照常触发, 超过视为过期清理
_STALE_GRACE_SECONDS = 60
# bot 离线时到期任务的顺延(秒): 避免重连后瞬间补发过期窗口
_OFFLINE_POSTPONE_SECONDS = 300

_DEFAULT_CONFIG: dict[str, Any] = {
    "enable": True,
    "min_interval_minutes": 30,
    "max_interval_minutes": 600,
    "quiet_hours": "1-7",
    "max_unanswered_times": 4,
    "proactive_prompt": "",
    "exclude_users": [],
    "enable_auto_trigger": False,
    "auto_trigger_after_minutes": 5,
    "tick_interval_sec": 30,
}


def session_key(bot_id: str, user_id: str) -> str:
    return f"{bot_id}:{user_id}"


class ProactiveEngine:
    """主动聊天引擎。运行时服务通过 get_ref 按名惰性解析(注入时序无关)。"""

    def __init__(
        self,
        *,
        state_path: str | Path,
        config: dict[str, Any] | None = None,
        get_ref: Callable[[str], Any],
        clock: Callable[[], float] = time.time,
    ):
        self.state_path = Path(state_path)
        self._config = dict(_DEFAULT_CONFIG)
        self.update_config(config or {})
        self._get_ref = get_ref
        self._clock = clock
        self.plugin_start_time = clock()
        # 正在执行主动流程的会话(防重入; on_tick 本身串行, 此为兜底)
        self._busy: set[str] = set()
        # 配置关闭后待清理标记(tick 里做一次性清理, on_config_update 是同步钩子)
        self._pending_disable_cleanup = False

    # ── 配置 ──────────────────────────────────────────────

    def update_config(self, config: dict[str, Any]) -> None:
        self._config.update(config or {})
        if not self._config.get("enable", True):
            self._pending_disable_cleanup = True

    def _cfg(self) -> dict[str, Any]:
        return self._config

    def tick_interval(self) -> int:
        """周期轮询间隔(秒), 下限 5 秒。"""
        return max(5, int(self._config.get("tick_interval_sec", 30) or 30))

    def _ref(self, name: str) -> Any:
        return self._get_ref(name)

    def _exclude_set(self) -> set[str]:
        return {str(u) for u in (self._cfg().get("exclude_users") or [])}

    # ── 状态读写 ──────────────────────────────────────────

    async def _mutate_state(self, fn: Callable[[dict], None]) -> dict:
        """原子读改写状态文件; fn 就地修改, 返回改后全量状态。"""
        def _update(data: Any) -> dict:
            state = data if isinstance(data, dict) else {}
            state.setdefault("version", 1)
            fn(state)
            return state

        return await json_update(self.state_path, _update, default={})

    async def _read_sessions(self) -> dict[str, dict[str, Any]]:
        from mohobot.file_store import json_read

        try:
            state = await json_read(self.state_path)
        except FileNotFoundError:
            return {}
        if not isinstance(state, dict):
            return {}
        sessions = state.get("sessions")
        return sessions if isinstance(sessions, dict) else {}

    # ── 事件钩子 ──────────────────────────────────────────

    async def on_private_message(self, bot_id: str, user_id: str,
                                 sender_name: str = "") -> None:
        """用户私聊消息到达: 登记/重置计数/取消待发任务并排下一次随机窗口。"""
        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        key = session_key(bot_id, user_id)
        now = self._clock()

        if user_id in self._exclude_set():
            await self._mutate_state(lambda state: state.get("sessions", {}).pop(key, None))
            return

        interval = random_interval_seconds(
            cfg.get("min_interval_minutes", 30), cfg.get("max_interval_minutes", 600),
        )

        def _update(state: dict) -> None:
            sessions = state.setdefault("sessions", {})
            entry = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
            entry["last_message_time"] = now
            entry["unanswered_count"] = 0
            entry.pop("auto_trigger_at", None)
            if sender_name:
                entry["sender_name"] = sender_name
            entry["last_scheduled_at"] = now
            entry["next_trigger_time"] = now + interval
            sessions[key] = entry

        await self._mutate_state(_update)

    async def on_startup(self) -> None:
        """启动维护: 清理过期任务、武装冷启动自动触发。"""
        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        now = self._clock()
        exclude = self._exclude_set()
        auto_minutes = int(cfg.get("auto_trigger_after_minutes", 5) or 5)
        auto_enabled = bool(cfg.get("enable_auto_trigger", False))

        def _update(state: dict) -> None:
            sessions = state.setdefault("sessions", {})
            for key in list(sessions.keys()):
                entry = sessions.get(key)
                if not isinstance(entry, dict):
                    del sessions[key]
                    continue
                bot_id, _, user_id = key.partition(":")
                if user_id in exclude:
                    del sessions[key]
                    continue
                next_trigger = entry.get("next_trigger_time")
                if isinstance(next_trigger, (int, float)) and next_trigger < now - _STALE_GRACE_SECONDS:
                    # 停机期间过期的任务不再补发, 交给自动触发/下次消息重建
                    entry.pop("next_trigger_time", None)
                if (
                    auto_enabled
                    and "next_trigger_time" not in entry
                    and self._within_cap(entry, cfg)
                ):
                    entry["auto_trigger_at"] = now + auto_minutes * 60

        await self._mutate_state(_update)

    # ── 周期调度 ──────────────────────────────────────────

    async def tick(self) -> None:
        cfg = self._cfg()
        if not cfg.get("enable", True):
            if self._pending_disable_cleanup:
                self._pending_disable_cleanup = False
                await self._mutate_state(self._clear_all_pending)
            return
        if self._pending_disable_cleanup:
            self._pending_disable_cleanup = False

        now = self._clock()
        sessions = await self._read_sessions()
        due = sorted(
            (key for key, entry in sessions.items()
             if isinstance(entry, dict)
             and isinstance(entry.get("next_trigger_time"), (int, float))
             and entry["next_trigger_time"] <= now),
            key=lambda key: sessions[key]["next_trigger_time"],
        )
        # 自动触发到点检查(启动后始终无消息才转正)
        auto_due = sorted(
            (key for key, entry in sessions.items()
             if isinstance(entry, dict)
             and isinstance(entry.get("auto_trigger_at"), (int, float))
             and entry["auto_trigger_at"] <= now),
        )
        for key in auto_due:
            await self._handle_auto_trigger(key, cfg, now)
        for key in due:
            if key in self._busy:
                continue
            await self._run_session(key, cfg, now)

    @staticmethod
    def _clear_all_pending(state: dict) -> None:
        for entry in (state.get("sessions") or {}).values():
            if isinstance(entry, dict):
                entry.pop("next_trigger_time", None)
                entry.pop("auto_trigger_at", None)

    def _within_cap(self, entry: dict, cfg: dict) -> bool:
        max_unanswered = int(cfg.get("max_unanswered_times", 4) or 0)
        if max_unanswered <= 0:
            return True
        return int(entry.get("unanswered_count", 0) or 0) < max_unanswered

    async def _handle_auto_trigger(self, key: str, cfg: dict, now: float) -> None:
        """冷启动自动触发到点: 启动后该会话始终无消息才创建任务, 否则放弃。"""
        bot_id, _, user_id = key.partition(":")
        if user_id in self._exclude_set() or key in self._busy:
            await self._mutate_state(
                lambda state: (state.get("sessions", {}).get(key) or {}).pop("auto_trigger_at", None)
            )
            return

        def _update(state: dict) -> None:
            entry = (state.get("sessions", {}).get(key) or {})
            entry.pop("auto_trigger_at", None)
            last_message = entry.get("last_message_time", 0)
            # 启动后用户来过消息 → 观察钩子已排程, 放弃自动触发
            if last_message and last_message >= self.plugin_start_time:
                return
            if not self._within_cap(entry, cfg):
                return
            entry["last_scheduled_at"] = now
            entry["next_trigger_time"] = now + random_interval_seconds(
                cfg.get("min_interval_minutes", 30), cfg.get("max_interval_minutes", 600),
            )

        await self._mutate_state(_update)

    async def _reschedule(self, key: str, cfg: dict, now: float) -> None:
        interval = random_interval_seconds(
            cfg.get("min_interval_minutes", 30), cfg.get("max_interval_minutes", 600),
        )

        def _update(state: dict) -> None:
            entry = state.setdefault("sessions", {}).setdefault(key, {})
            entry["last_scheduled_at"] = now
            entry["next_trigger_time"] = now + interval

        await self._mutate_state(_update)

    # ── 单会话主动消息流程 ────────────────────────────────

    async def _run_session(self, key: str, cfg: dict, now: float) -> None:
        bot_id, _, user_id = key.partition(":")
        if user_id in self._exclude_set():
            return
        self._busy.add(key)
        try:
            sessions = await self._read_sessions()
            entry = sessions.get(key)
            if not isinstance(entry, dict) or "next_trigger_time" not in entry:
                return  # 生成排队期间被用户消息取消/重排

            bot_manager = self._ref("bot_manager")
            instance = bot_manager.get(bot_id) if bot_manager else None
            if instance is None:
                # bot 离线: 顺延而不是反复消耗窗口, 避免重连后过期补发
                overdue = now - entry["next_trigger_time"]
                if overdue > _OFFLINE_POSTPONE_SECONDS:
                    logger.info(f"[主动消息] {key} bot 未连接, 顺延 {_OFFLINE_POSTPONE_SECONDS}s")
                    await self._mutate_state(_set_next_trigger(key, now + _OFFLINE_POSTPONE_SECONDS))
                return

            llm = self._ref("llm_service")
            # LLMService._available 即 _chat_client is not None; 这里直接判客户端
            if llm is None or getattr(llm, "_chat_client", None) is None:
                if now - entry["next_trigger_time"] > _OFFLINE_POSTPONE_SECONDS:
                    logger.warning(f"[主动消息] {key} LLM 未就绪, 顺延 {_OFFLINE_POSTPONE_SECONDS}s")
                    await self._mutate_state(_set_next_trigger(key, now + _OFFLINE_POSTPONE_SECONDS))
                return

            if is_quiet_time(cfg.get("quiet_hours", "1-7")):
                await self._reschedule(key, cfg, now)
                return

            if not self._within_cap(entry, cfg):
                # 达到未回复上限: 暂停(用户回复时由观察钩子归零恢复)。
                # 仅当待发任务仍是读到的那一个才清, 避免覆盖用户刚回复的重排
                planned_trigger = entry["next_trigger_time"]
                await self._mutate_state(_clear_pending_if(key, planned_trigger))
                return

            context_manager = self._ref("context_manager")
            persona_service = self._ref("persona_service")
            if context_manager is None or persona_service is None:
                if now - entry["next_trigger_time"] > _OFFLINE_POSTPONE_SECONDS:
                    logger.warning(f"[主动消息] {key} 上下文/人设服务未注入, 顺延 {_OFFLINE_POSTPONE_SECONDS}s")
                    await self._mutate_state(_set_next_trigger(key, now + _OFFLINE_POSTPONE_SECONDS))
                return

            # 捕获会话一致性快照(与被动回复同机制)
            session = await context_manager.capture_session(bot_id, "private", user_id)
            context = await context_manager.load_context(
                bot_id, "private", user_id,
                session_id=session["id"], generation=session["generation"],
            )
            gen_start_message_time = entry.get("last_message_time", 0)
            unanswered_count = int(entry.get("unanswered_count", 0) or 0)

            # 消耗本次触发: 仅当待发任务仍是读到的那一个才清(CAS),
            # 用户恰在此时回复时其重排不被覆盖; 生成后由窗口检查丢弃
            planned_trigger = entry["next_trigger_time"]
            await self._mutate_state(_clear_pending_if(key, planned_trigger))

            event, prompt = self._build_synthetic_event(
                instance, user_id, entry, unanswered_count,
            )
            bot_config = deepcopy(getattr(instance, "config", None))
            persona = persona_service.resolve(bot_config, session.get("persona_id", ""))
            try:
                reply, _tool_results = await llm.chat(
                    bot_id=bot_id,
                    event=event,
                    context=context,
                    raw_event={
                        "post_type": "message", "message_type": "private",
                        "user_id": int(user_id), "self_id": int(getattr(instance, "qq", 0) or 0),
                    },
                    bot_config=bot_config,
                    persona_content=persona["content"],
                )
            except Exception as exc:
                logger.warning(f"[主动消息] {key} LLM 生成失败: {exc}")
                reply = None
            reply = (reply or "").strip()
            # 生成报错字符串(LLM 未配置等)不当作内容发送
            if not reply or reply.startswith("LLM 服务未配置"):
                await self._reschedule(key, cfg, self._clock())
                return

            # 生成窗口内用户来过新消息 → 丢弃, 观察钩子已重排
            latest = (await self._read_sessions()).get(key) or {}
            if latest.get("last_message_time", 0) > gen_start_message_time:
                logger.info(f"[主动消息] {key} 生成期间用户发来新消息, 丢弃本次主动消息")
                return

            display = await self._send(bot_id, user_id, event, reply)
            if not display:
                await self._reschedule(key, cfg, self._clock())
                return

            # 消息已发出: 上下文补记总是做; 但分段发送可耗时数秒,
            # 若用户在生成/发送期间回复, 计数与重排交给观察钩子(已归零重排)
            latest = (await self._read_sessions()).get(key) or {}
            user_replied = latest.get("last_message_time", 0) > gen_start_message_time

            context_manager = self._ref("context_manager")
            if context_manager is not None:
                try:
                    await context_manager.append_context(
                        bot_id, "private", user_id,
                        [{
                            "role": "assistant",
                            "content": display,
                            "timestamp": int(self._clock()),
                        }],
                        session_id=session["id"], generation=session["generation"],
                    )
                except Exception as exc:
                    logger.warning(f"[主动消息] {key} 上下文回写失败(继续调度): {exc}")

            if user_replied:
                logger.info(f"[主动消息] {key} 发送期间用户已回复, 不计数不重排")
                return

            now = self._clock()

            def _update(state: dict) -> None:
                entry_now = state.setdefault("sessions", {}).setdefault(key, {})
                entry_now["unanswered_count"] = unanswered_count + 1
                entry_now["last_scheduled_at"] = now
                entry_now["next_trigger_time"] = now + random_interval_seconds(
                    cfg.get("min_interval_minutes", 30), cfg.get("max_interval_minutes", 600),
                )

            await self._mutate_state(_update)
            logger.info(
                f"[主动消息] {key} 第 {unanswered_count + 1} 次主动消息已发送, "
                f"未回复计数: {unanswered_count + 1}",
            )
        except Exception as exc:
            logger.error(f"[主动消息] {key} 主动消息流程异常: {exc}")
            try:
                await self._reschedule(key, cfg, self._clock())
            except Exception as reschedule_exc:
                logger.error(f"[主动消息] {key} 异常后重调度失败: {reschedule_exc}")
        finally:
            self._busy.discard(key)

    def _build_synthetic_event(
        self, instance: Any, user_id: str, entry: dict, unanswered_count: int,
    ) -> tuple[PrivateMessageEvent, str]:
        """构造合成私聊事件: 提示词作为当轮用户输入, 不写入上下文。"""
        prompt = render_prompt(
            self._cfg().get("proactive_prompt", ""), unanswered_count,
        )
        sender_name = str(entry.get("sender_name") or f"User-{user_id}")
        event = PrivateMessageEvent(
            time=int(self._clock()),
            self_id=int(getattr(instance, "qq", 0) or 0),
            post_type="message",
            message_type="private",
            user_id=int(user_id),
            message=[{"type": "text", "data": {"text": prompt}}],
            raw_message=prompt,
            sender=Sender(user_id=int(user_id), nickname=sender_name),
        )
        return event, prompt

    async def _send(self, bot_id: str, user_id: str, event: PrivateMessageEvent,
                    reply: str) -> str:
        """发送主动消息; 优先走消息处理器(与被动回复同形态), 兜底 WS 直发。"""
        message_handler = self._ref("message_handler")
        if message_handler is not None and hasattr(message_handler, "send_proactive_reply"):
            try:
                return await message_handler.send_proactive_reply(bot_id, event, reply)
            except Exception as exc:
                logger.warning(f"[主动消息] {bot_id}:{user_id} 管线发送失败: {exc}")
                return ""
        ws_server = self._ref("ws_server")
        if ws_server is None:
            return ""
        await ws_server.send_private_msg(bot_id, user_id, reply, source="llm")
        return reply


def _set_next_trigger(key: str, when: float) -> Callable[[dict], None]:
    def _update(state: dict) -> None:
        entry = state.setdefault("sessions", {}).setdefault(key, {})
        entry["next_trigger_time"] = when
    return _update


def _clear_pending_if(key: str, planned_trigger: float) -> Callable[[dict], None]:
    """CAS 清除待发任务: 仅当 next_trigger_time 仍是计划值时清除。

    用户恰在消耗瞬间回复时, 观察钩子刚写入的新触发时间不会被覆盖。
    """
    def _update(state: dict) -> None:
        entry = (state.get("sessions", {}).get(key) or {})
        if entry.get("next_trigger_time") == planned_trigger:
            entry.pop("next_trigger_time", None)
            entry.pop("auto_trigger_at", None)
    return _update
