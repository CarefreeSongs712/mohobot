"""表情包发送引擎：门控 → 选图提示 → 会话模型调用工具选图 → 回复后发送。

会话模型自己决定发不发、发哪张：通过门控（开关、名单、冷却、概率）的轮次，
插件经 mohobot 的环境感知钩子返回一段选图提示，mohobot 只把它并入本轮请求，
不写入会话上下文。模型调用 send_meme 后图片先排队，等本条消息的回复全部发出后
再发送，保持“回复在前、表情在后”。
"""

import asyncio
import os
import random
from typing import Any

from ...host import StealerEvent, logger

from ..util.normalization import bounded_int
from .event_context import get_event_bot_session_key

FALLBACK_MEME_HINT_PROMPT = (
    "[表情包] 这轮你可以配一张表情包，不是必须。\n"
    "觉得合适时：调用 {search_tool}，用一两句话描述想要的画面——主体与动作、图上可能的文字、"
    "想表达的语气；从返回的最多 {candidate_count} 个候选里挑一张调用 {send_tool}，都不贴切就不发。\n"
    "严肃话题、道歉、报错、技术解答不配图。表情包在文字回复后自动发出，正文不用提及。"
)


class _MemeTurnState:
    """封装单次会话中的表情包发送状态。"""

    def __init__(self) -> None:
        self._active_sent = False
        self._candidates: list[dict] = []
        self._auto_decided = False
        self._auto_allowed = False
        self._auto_reason = ""
        self._hint_injected = False
        self._queued: dict | None = None

    def mark_active_sent(self) -> None:
        """标记当前回合已发送过表情包。"""
        self._active_sent = True
        if hasattr(self, "_event"):
            self._event.set_extra("stealer_active_sent", True)

    def is_active_sent(self) -> bool:
        return self._active_sent

    def set_candidates(self, candidates: list[dict]) -> None:
        self._candidates = candidates

    def get_candidates(self) -> list[dict]:
        return self._candidates

    def is_auto_decided(self) -> bool:
        return self._auto_decided

    def set_auto_decision(self, allowed: bool, reason: str = "") -> None:
        self._auto_decided = True
        self._auto_allowed = allowed
        self._auto_reason = reason

    def get_auto_allowed(self) -> bool:
        return self._auto_allowed

    def get_auto_reason(self) -> str:
        return self._auto_reason

    def mark_hint_injected(self) -> None:
        self._hint_injected = True

    def is_hint_injected(self) -> bool:
        return self._hint_injected

    def queue(self, candidate: dict) -> None:
        """排队一张待发送的表情；同一轮多次选择时以最后一次为准。"""
        self._queued = dict(candidate)

    def pop_queued(self) -> dict | None:
        queued, self._queued = self._queued, None
        return queued

    def reset_for_new_turn(self) -> None:
        self._active_sent = False
        self._candidates = []
        self._auto_decided = False
        self._auto_allowed = False
        self._auto_reason = ""
        self._hint_injected = False
        self._queued = None


class MemeSenderEngine:
    """负责表情包发送门控、提示注入和排队发送。"""

    AUTO_EMOJI_COOLDOWN_SECONDS = 20  # 同一会话自动发表情的最短间隔

    def __init__(self, plugin_instance: Any) -> None:
        self.plugin = plugin_instance
        self._auto_emoji_cooldowns: dict[str, float] = {}
        self._auto_emoji_cooldowns_lock = asyncio.Lock()
        self._pending_auto_emoji_tasks: dict[str, asyncio.Task] = {}

    # --- 状态管理 ---

    def emoji_turn_state(self, event: StealerEvent) -> _MemeTurnState:
        """获取或创建当前会话的回合状态（挂在本轮事件对象上）。"""
        key = self.get_auto_emoji_session_key(event)
        if not hasattr(event, "_emoji_turn_state"):
            event._emoji_turn_state = {}  # type: ignore[attr-defined]
        turn_states = event._emoji_turn_state  # type: ignore[attr-defined]
        if key not in turn_states:
            turn_states[key] = _MemeTurnState()
            turn_states[key]._event = event
        return turn_states[key]

    def get_auto_emoji_session_key(self, event: StealerEvent) -> str:
        # 同群多个 bot 的冷却与待发表情各自独立，互不取消
        return get_event_bot_session_key(event)

    def reset_turn_state(self, event: StealerEvent) -> None:
        """重置表情包回合状态及事件 extras，为新的一轮对话做准备。"""
        self.emoji_turn_state(event).reset_for_new_turn()
        for key in (
            "stealer_active_sent",
            "stealer_auto_emoji_turn_decided",
            "stealer_auto_emoji_turn_allowed",
        ):
            try:
                event.set_extra(key, False)
            except Exception:
                pass

    def cancel_pending_auto_emoji(self, event: StealerEvent, reason: str = "new_message") -> bool:
        """取消当前会话尚未发出的表情任务。"""
        key = self.get_auto_emoji_session_key(event)
        task = self._pending_auto_emoji_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            logger.debug(f"[MemeSenderEngine] 取消待发送表情: session={key}, reason={reason}")
            return True
        return False

    def schedule_auto_emoji_task(
        self,
        event: StealerEvent,
        task: asyncio.Task | None,
    ) -> asyncio.Task | None:
        """记录当前会话的待发送任务，并替换掉旧任务。"""
        if task is None:
            return None
        key = self.get_auto_emoji_session_key(event)
        old_task = self._pending_auto_emoji_tasks.get(key)
        if old_task and old_task is not task and not old_task.done():
            old_task.cancel()
        self._pending_auto_emoji_tasks[key] = task

        def _clear(done_task: asyncio.Task) -> None:
            if self._pending_auto_emoji_tasks.get(key) is done_task:
                self._pending_auto_emoji_tasks.pop(key, None)

        task.add_done_callback(_clear)
        return task

    # --- 门控 ---

    async def is_auto_emoji_cooldown_ready(self, event: StealerEvent) -> bool:
        key = self.get_auto_emoji_session_key(event)
        now = asyncio.get_event_loop().time()
        async with self._auto_emoji_cooldowns_lock:
            last = self._auto_emoji_cooldowns.get(key, 0)
            return now - last >= self.AUTO_EMOJI_COOLDOWN_SECONDS

    def normalize_auto_meme_chance(self) -> float:
        try:
            chance = float(getattr(self.plugin, "meme_chance", 0.2))
        except (TypeError, ValueError):
            chance = 0.2
        return max(0.0, min(1.0, chance))

    async def resolve_auto_emoji_turn_permission(self, event: StealerEvent) -> bool:
        """本轮是否允许提示会话模型配表情：开关 → 名单 → 冷却 → 概率。"""
        turn_state = self.emoji_turn_state(event)
        if turn_state.is_auto_decided():
            return turn_state.get_auto_allowed()

        def decide(allowed: bool, reason: str) -> bool:
            event.set_extra("stealer_auto_emoji_turn_decided", True)
            event.set_extra("stealer_auto_emoji_turn_allowed", allowed)
            event.set_extra("stealer_auto_emoji_turn_reason", reason)
            turn_state.set_auto_decision(allowed, reason)
            logger.debug(f"[Stealer] 本轮配表情判定: allowed={allowed}, reason={reason}")
            return allowed

        if not getattr(self.plugin, "auto_send_meme", False):
            return decide(False, "auto_send_meme_disabled")
        if not self.plugin.is_send_enabled_for_event(event):
            return decide(False, "send_disabled")
        if not await self.is_auto_emoji_cooldown_ready(event):
            return decide(False, "cooldown")
        chance = self.normalize_auto_meme_chance()
        if chance <= 0:
            return decide(False, "chance_zero")
        if chance >= 1 or random.random() < chance:
            return decide(True, "chance_hit")
        return decide(False, "chance_miss")

    def prune_auto_emoji_cooldowns(self, now: float) -> None:
        cutoff = now - self.AUTO_EMOJI_COOLDOWN_SECONDS * 2
        for k in [k for k, v in self._auto_emoji_cooldowns.items() if v < cutoff]:
            del self._auto_emoji_cooldowns[k]

    async def mark_auto_emoji_sent(self, event: StealerEvent) -> None:
        key = self.get_auto_emoji_session_key(event)
        now = asyncio.get_event_loop().time()
        async with self._auto_emoji_cooldowns_lock:
            self.prune_auto_emoji_cooldowns(now)
            self._auto_emoji_cooldowns[key] = now

    # --- 提示注入 ---

    def _tools_available(self) -> bool:
        checker = getattr(self.plugin, "meme_tools_registered", None)
        if not callable(checker):
            return True
        try:
            return bool(checker())
        except Exception:
            return False

    def _library_has_memes(self) -> bool:
        db = getattr(self.plugin, "db_service", None)
        try:
            return db is not None and db.count_total() > 0
        except Exception:
            return False

    def render_hint(self) -> str:
        cfg = self.plugin.plugin_config
        template = str(getattr(cfg, "meme_hint_prompt", "") or "").strip()
        if not template:
            template = str(
                getattr(self.plugin, "MEME_HINT_PROMPT", "") or FALLBACK_MEME_HINT_PROMPT
            )
        values = {
            "{search_tool}": self.plugin.SEARCH_MEME_TOOL_NAME,
            "{send_tool}": self.plugin.SEND_MEME_TOOL_NAME,
            "{candidate_count}": str(self.candidate_count()),
        }
        for placeholder, value in values.items():
            template = template.replace(placeholder, value)
        return template

    def candidate_count(self) -> int:
        cfg = self.plugin.plugin_config
        return bounded_int(getattr(cfg, "meme_candidate_count", 5), 5, 1, 20)

    async def build_meme_hint(self, event: StealerEvent) -> str:
        """通过门控时返回本轮的选图提示，否则返回空串。

        提示由 mohobot 并入本轮 LLM 请求（环境感知段），不写入会话上下文。
        """
        if not self._tools_available():
            logger.debug("[Stealer] 表情包工具未注册，跳过选图提示")
            return ""
        if not self._library_has_memes():
            return ""
        if not await self.resolve_auto_emoji_turn_permission(event):
            return ""
        self.emoji_turn_state(event).mark_hint_injected()
        logger.debug("[Stealer] 本轮附带选图提示")
        return self.render_hint()

    # --- 排队与发送 ---

    def queue_meme(self, event: StealerEvent, candidate: dict) -> None:
        self.emoji_turn_state(event).queue(candidate)

    def get_meme_send_delay(self, text: str = "", task_start: float = 0.0) -> float:
        """回复发出后等待多久再发表情（秒）。"""
        char_delay = getattr(self.plugin, "meme_send_char_delay", 0.0)
        if char_delay > 0 and text:
            desired = len(text) * char_delay
            if task_start > 0:
                elapsed = asyncio.get_event_loop().time() - task_start
                return max(0.0, desired - elapsed)
            return desired
        try:
            base = float(getattr(self.plugin, "meme_send_delay", 0.5))
        except (TypeError, ValueError):
            base = 0.5
        if bool(getattr(self.plugin, "meme_send_delay_random", False)):
            try:
                rand_max = float(getattr(self.plugin, "meme_send_delay_max", 8.0))
            except (TypeError, ValueError):
                rand_max = 8.0
            if rand_max > 0:
                return base + random.random() * rand_max
        return base

    def dispatch_queued_meme(self, event: StealerEvent, reply_text: str = "") -> bool:
        """最终回复生成后调用：把本轮排队的表情安排在回复之后发出。"""
        queued = self.emoji_turn_state(event).pop_queued()
        if not queued:
            return False
        task = self.plugin._safe_create_task(
            self._send_queued(event, queued, reply_text), name="emoji_send_queued"
        )
        self.schedule_auto_emoji_task(event, task)
        return True

    async def _send_queued(self, event: StealerEvent, queued: dict, reply_text: str) -> bool:
        try:
            task_start = asyncio.get_event_loop().time()
            delay = self.get_meme_send_delay(reply_text, task_start)
            if delay > 0:
                await asyncio.sleep(delay)

            path = str(queued.get("path") or "")
            selector = getattr(self.plugin, "meme_selector", None)
            if selector is None or not path or not os.path.isfile(path):
                logger.debug(f"[MemeSenderEngine] 排队的表情已不可用: {path}")
                return False
            if not self.plugin.is_send_enabled_for_event(event):
                return False
            if not selector.is_path_allowed_for_event(path, event):
                return False

            send_mode = await selector.send_emoji_message(event, path)
            if not send_mode:
                logger.warning(f"[MemeSenderEngine] 表情发送失败: {path}")
                return False
            await selector.record_emoji_usage(path, trigger="llm_tool")
            selector.mark_recently_sent(path, queued.get("meta"))
            await self.mark_auto_emoji_sent(event)
            self.emoji_turn_state(event).mark_active_sent()
            logger.info(f"[MemeSenderEngine] 已在回复后发送表情 ({send_mode}): {path}")
            return True
        except asyncio.CancelledError:
            logger.debug("[MemeSenderEngine] 待发送表情已取消")
            raise
        except Exception as e:
            logger.warning(f"[MemeSenderEngine] 发送排队表情失败: {e}")
            return False
