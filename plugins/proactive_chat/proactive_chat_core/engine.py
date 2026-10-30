"""私聊主动聊天引擎: 状态持久化、随机窗口调度与主动消息生成发送全流程。

机制移植自 astrbot_plugin_proactive_chat (AGPL-3.0), 适配 mohobot 管线:
- 会话键为 "{bot_id}:{user_id}", 自动跟踪全部私聊(排除名单兜底);
- 调度用 on_tick 轮询持久化的 next_trigger_time, 不引入 APScheduler,
  重启恢复由"到期任务仍在状态文件里"自然保证;
- 生成走 llm_service.chat(合成私聊事件), 发送走 message_handler
  的 send_proactive_reply(与被动回复同形态), 上下文回写只追加 assistant 一条。
"""
from __future__ import annotations

import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from mohobot.file_store import json_update
from mohobot.models.onebot import PrivateMessageEvent, Sender
from mohobot.utils.llm_diagnostic import LLMDiagnostic

from .schedule import is_quiet_time, random_interval_seconds, render_prompt

# 重启恢复容忍期(秒): 到期未超过该时长的任务照常触发, 超过视为过期清理
_STALE_GRACE_SECONDS = 60
# bot 离线时到期任务的顺延(秒): 避免重连后瞬间补发过期窗口
_OFFLINE_POSTPONE_SECONDS = 300

# LLM 自主决策协议: 模型可在生成阶段选择本次不发([SKIP])或暂停该会话([PAUSE])。
# 指令作为服务端固定段附加(不进可编辑提示词), 避免用户配置覆盖导致协议丢失;
# 标记必须独占整条回复, 解析端容忍括号/空白/大小写变体。
_DECISION_GUIDANCE = (
    "\n\n【发送前自主判断】\n"
    "你并不一定要发送这条主动消息。先结合最近的聊天上下文、当前时间与未回复状态"
    "(当前连续未回复次数: {{unanswered_count}}), 判断现在主动开口是否合适:\n"
    "- 若只是此刻不合适(对方可能在忙、情绪不适合、话题没到开口的时候), "
    "整条回复只输出 [SKIP]: 本轮不发, 稍后会有新机会;\n"
    "- 若上下文已经清楚表明对方不希望你主动打扰(比如连续多次没回复、上次不欢而散、"
    "对方明确说过别再发), 整条回复只输出 [PAUSE]: 主动消息将暂停, 直到对方再回复你;\n"
    "- 标记必须独占整条回复; 若你决定主动开口, 就按前述要求正常生成开场白, 不输出标记。"
)

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
    "enable_llm_choice": True,
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
        # 封禁检查(issue #9 第 2 点): 用户可能在排队/冷却期间被封禁,
        # 到期后不得再生成或发送主动消息(否则绕过封禁策略)。
        if await self._is_banned_key(key):
            await self._mutate_state(lambda st: st.get("sessions", {}).pop(key, None))
            logger.debug(f"[主动消息] 跳过已封禁会话: {key}")
            return
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
            # 诊断文案(未配置/调用失败/空响应)不是主动消息正文: 不发送、不计数、正常重排。
            # 必须在 strip() 之前判断, 字符串运算会丢掉 LLMDiagnostic 标记。
            if isinstance(reply, LLMDiagnostic):
                logger.warning(f"[主动消息] {key} LLM 未产出有效内容, 本次不发送: {reply[:80]}")
                await self._reschedule(key, cfg, self._clock())
                return
            reply = (reply or "").strip()
            if not reply:
                await self._reschedule(key, cfg, self._clock())
                return

            # 生成窗口内用户来过新消息 → 丢弃, 观察钩子已重排
            latest = (await self._read_sessions()).get(key) or {}
            if latest.get("last_message_time", 0) > gen_start_message_time:
                logger.info(f"[主动消息] {key} 生成期间用户发来新消息, 丢弃本次主动消息")
                return

            # LLM 自主决策(可关): [SKIP]=本次不发(不计数, 正常重排);
            # [PAUSE]=暂停该会话(待发任务已消耗, 不再重排即暂停, 对方回复后自动恢复);
            # 标记附带正文时剥标记按正文发送, 不视为拒绝。
            reply_after_marker = reply
            if self._cfg().get("enable_llm_choice", True):
                choice, reply_after_marker = self._parse_choice(reply)
                if choice and not reply_after_marker:
                    if choice == "skip":
                        logger.info(f"[主动消息] {key} 模型判断此刻不适合主动开口, 本次跳过")
                        await self._reschedule(key, cfg, self._clock())
                    else:
                        logger.info(f"[主动消息] {key} 模型判断对方不希望被打扰, 暂停主动消息(对方回复后恢复)")
                    return
                reply = reply_after_marker or reply

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
        if self._cfg().get("enable_llm_choice", True):
            # 自主决策协议是服务端固定段: 渲染占位符后整体附加
            prompt += render_prompt(_DECISION_GUIDANCE, unanswered_count)
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

    _CHOICE_RE = re.compile(r"[\[【(（]\s*(skip|pause)\s*[\]】)）]", re.IGNORECASE)

    def _parse_choice(self, reply: str) -> tuple[str | None, str]:
        """解析 LLM 的自主决策标记。

        返回 (choice, 剥除标记后的剩余文本):
        - choice='skip'/'pause': 剩余文本为空 → 按决策处理;
        - choice=None: 无标记, 剩余文本即原回复;
        标记附带正文时返回剩余正文, 由调用方按普通消息发送。
        """
        markers = self._CHOICE_RE.findall(reply)
        if not markers:
            # 容忍无括号的裸标记(去空白/标点后精确等于 skip/pause)
            normalized = re.sub(r"[\s\[\]【】()（）。，,!！?？:：;；]+", "", reply).lower()
            if normalized == "skip":
                return "skip", ""
            if normalized == "pause":
                return "pause", ""
            return None, reply
        remaining = self._CHOICE_RE.sub("", reply).strip()
        kind = markers[-1].lower()
        if remaining:
            return kind, remaining  # 标记附带正文: 按正文发送
        return kind, ""

    async def _is_banned_key(self, key: str) -> bool:
        """该会话键是否对应已被全局封禁的用户/群(未注入 BanStore 时返回 False)。"""
        store = self._ref("ban_store")
        if store is None:
            return False
        parts = str(key).split(":")
        if len(parts) < 2:
            return False
        if len(parts) >= 3 and parts[-2] in ("private", "group"):
            # bot:private:123 / bot:group:456
            chat_type, target = parts[-2], parts[-1]
            scope = "private" if chat_type == "private" else "group"
        else:
            # 主动聊天会话键为 ``bot_id:user_id``(session_key), 属私聊
            scope, target = "private", parts[-1]
        try:
            banned, _reason = await store.is_banned(f"{scope}:{target}", str(target))
        except Exception as exc:  # noqa: BLE001 - 查询失败不阻断发送
            logger.debug(f"[主动消息] 封禁检查失败({key}): {exc}")
            return False
        return bool(banned)

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
        # 已知缺口: 兜底 WS 直发不经过 MessageHandler 的 OOC 三防。仅当
        # message_handler 未注入或没有 send_proactive_reply 时走到这里;
        # 正常路径已由 handler 治理。此处不在本 PR 范围内。
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
