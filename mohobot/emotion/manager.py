"""EmotionManager — 情感系统编排器。

流水线接入:
- pre-LLM:  build_context_block() 生成注入的系统消息(不写入 context 文件)
- post-LLM: schedule_turn() 后台执行 process_turn(智能决策 → 二次 LLM 分析
            → 应用增量 → 写长期记忆), 不阻塞回复发送

数据: data/emotion/{bot_id}/user_states.json + memory.json (原子 JSON)。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from loguru import logger

from .attitude import get_tone_instruction  # noqa: F401  (commands.py 复用导出)
from .expert import EmotionExpert
from .memory import MemorySystem
from .models import EmotionalState, ATTITUDE_TEXT_MAX
from .prompts import build_injection_block
from .relationship import StageManager
from .smart import SmartUpdateManager
from .store import EmotionStore

AUTOSAVE_INTERVAL = 60.0


class EmotionManager:
    """情感系统入口: 状态/记忆存储 + 注入 + 后台分析 + 命令数据访问。"""

    def __init__(
        self,
        data_dir: str,
        config,  # mohobot.models.config.EmotionConfig
        llm_service,
        task_supervisor=None,
        admins: list | None = None,
        bot_name_provider: Callable[[str], str] | None = None,
    ) -> None:
        self._cfg = config
        self._llm = llm_service
        self._supervisor = task_supervisor
        self._bot_name_provider = bot_name_provider or (lambda bot_id: "AI")
        self._memory = MemorySystem()
        self._store = EmotionStore(f"{data_dir}/emotion", self._memory)
        self._smart = SmartUpdateManager()
        self._expert = EmotionExpert(
            llm_call=self._analyze_llm,
            timeout=max(5, int(getattr(config, "analysis_timeout_sec", 90))),
        )
        self._admins: set[int] = {int(a) for a in (admins or [])}
        self._save_task: asyncio.Task | None = None
        # 单并发队列: 同一时刻只允许一次情感分析 LLM 调用, 其余排队等待
        self._analysis_lock = asyncio.Lock()
        self._pending_analysis = 0      # 已进入分析段(排队+进行中)的任务数
        self._burst_left = 0            # 快速模型 burst 剩余次数
        self._active_model: str | None = None   # 本次分析用的模型(None=配置的正常模型)

    # ── 生命周期 ─────────────────────────────────────────────

    async def startup(self) -> None:
        if self._supervisor is not None:
            self._save_task = self._supervisor.create_task(
                self._save_loop(), name="emotion-autosave", owner="emotion"
            )
        else:
            self._save_task = asyncio.create_task(self._save_loop())
        logger.info("情感系统已启动(EmotionManager)")

    async def shutdown(self) -> None:
        if self._save_task is not None:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
            self._save_task = None
        await self._store.flush()
        logger.info("情感系统已关闭(数据已落盘)")

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(AUTOSAVE_INTERVAL)
            try:
                await self._store.flush()
            except Exception as e:
                logger.debug(f"情感数据周期落盘失败: {e}")

    def _analyze_llm(self, prompt: str):
        """情感分析 LLM 调用(burst 期间按次指定快速模型)。"""
        if self._active_model:
            return self._llm.analyze_emotion(prompt, model=self._active_model)
        return self._llm.analyze_emotion(prompt)

    # ── 配置热同步 / 管理员 ──────────────────────────────────

    def sync_config(self, cfg) -> None:
        """WebUI 保存后热同步(对象替换为最新加载的 EmotionConfig)。"""
        self._cfg = cfg
        try:
            self._expert._timeout = max(5, int(getattr(cfg, "analysis_timeout_sec", 90)))
        except Exception:
            pass

    def set_admin_ids(self, ids: list) -> None:
        self._admins = {int(a) for a in (ids or [])}

    def is_admin(self, user_id) -> bool:
        try:
            return int(user_id) in self._admins
        except (TypeError, ValueError):
            return False

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    # ── pre-LLM 注入 ─────────────────────────────────────────

    async def build_context_block(self, bot_id: str, event) -> str:
        """生成情感注入块; 禁用/无内容返回 ""。"""
        if not self.enabled:
            return ""
        user_key = str(getattr(event, "user_id", "") or "")
        if not user_key:
            return ""
        await self._store.ensure_loaded(bot_id)
        state = self._get_or_create_state(bot_id, user_key)
        relationship_context = self._memory.build_relationship_context(bot_id, user_key)
        return build_injection_block(state, self._bot_name_provider(bot_id), relationship_context)

    # ── post-LLM 后台分析 ────────────────────────────────────

    def schedule_turn(self, bot_id: str, event, user_text: str, ai_reply: str) -> None:
        """回复完成后的后台分析调度(fire-and-forget, 不阻塞发送)。"""
        if not self.enabled:
            return
        if self._is_error_placeholder(ai_reply):
            return
        user_id = str(getattr(event, "user_id", "") or "")
        if not user_id:
            return
        coro = self._safe_process_turn(bot_id, user_id, user_text, ai_reply)
        if self._supervisor is not None:
            self._supervisor.create_task(coro, name="emotion-turn", owner="emotion")
        else:
            asyncio.create_task(coro)

    @staticmethod
    def _is_error_placeholder(text: str) -> bool:
        head = (text or "")[:30]
        return head.startswith("[") and ("失败" in head or "未返回" in head or "错误" in head)

    async def _safe_process_turn(
        self, bot_id: str, user_id: str, user_text: str, ai_reply: str
    ) -> None:
        try:
            await self.process_turn(bot_id, user_id, user_text, ai_reply)
        except Exception as e:
            logger.warning(f"情感分析处理失败({bot_id}/{user_id}): {e}")

    async def process_turn(
        self, bot_id: str, user_id: str, user_text: str, ai_reply: str
    ) -> None:
        """一轮对话的情感更新(在后台任务中执行)。"""
        if not self.enabled:
            return
        await self._store.ensure_loaded(bot_id)
        user_key = str(user_id)
        state = self._get_or_create_state(bot_id, user_key)
        state.force_update_counter += 1

        # 冷却门控(热生效, 频率硬上限; last_force_update==0 表示从未分析过 → 不适用):
        if state.last_force_update > 0:
            import time as _time
            now = _time.time()
            if state.force_update_counter < max(0, int(self._cfg.analysis_round_cooldown)):
                # 距上次分析不足 N 轮 → 跳过(计数继续累积, 供强制更新判断)
                self._store.set_state(bot_id, user_key, state)
                return
            if now - state.last_force_update < max(0, int(self._cfg.min_interval_sec)):
                # 距上次分析不足最小间隔(秒) → 跳过
                self._store.set_state(bot_id, user_key, state)
                return

        if self._cfg.smart_update:
            needs, reason = self._smart.should_update(
                state, user_text, ai_reply, self._cfg.force_update_interval,
                keyword_threshold=int(getattr(self._cfg, "keyword_threshold", 3)),
            )
        else:
            needs, reason = True, "每轮分析(smart_update 关闭)"

        if not needs:
            self._store.set_state(bot_id, user_key, state)
            return

        logger.debug(f"情感更新触发({bot_id}/{user_key}): {reason}")
        bot_name = self._bot_name_provider(bot_id)
        # 单并发队列: 情感分析串行执行(同时只有一次 LLM 调用), 其余排队等待。
        # 多用户并发重 prompt 会把网关打满引发超时连锁; 串行化降低峰值压力。
        self._pending_analysis += 1
        try:
            async with self._analysis_lock:
                # 此刻仍在等待的任务数(自己已持锁) = 排队积压深度。
                # 积压超过阈值 → 用快速模型 burst 若干次, 尽快消化队列。
                self._pick_analysis_model(self._pending_analysis - 1)
                updates = await self._expert.analyze(user_text, ai_reply, state, bot_name)
                self.apply_expert_updates(state, updates)

                significance = self._calculate_significance(updates)
                written = self._memory.add_interaction(
                    bot_id, user_key, user_text, ai_reply,
                    significance, updates, threshold=int(self._cfg.significance_threshold),
                )
                if written:
                    self._store.touch_memory_dirty(bot_id)

                state.reset_force_update_counter()
                StageManager.get_stage_info(state)  # 刷新阶段/复合分/进度
                self._store.set_state(bot_id, user_key, state)
        finally:
            self._pending_analysis -= 1
            self._active_model = None
        logger.debug(
            f"情感更新完成({bot_id}/{user_key}): "
            f"favor={state.favor} intimacy={state.intimacy} source={updates.get('source')}"
        )

    def _pick_analysis_model(self, queued: int) -> None:
        """选择本次分析用的模型: 队列积压超阈值时启用快速模型 burst。

        burst 进行中不重复触发(一次触发固定连续 count 次);
        burst_model 为空表示不启用(始终用配置的正常模型)。
        """
        burst_model = str(getattr(self._cfg, "burst_model", "") or "").strip()
        if not burst_model:
            self._active_model = None
            return
        threshold = max(0, int(getattr(self._cfg, "queue_burst_threshold", 5)))
        count = max(1, int(getattr(self._cfg, "queue_burst_count", 3)))
        if queued > threshold and self._burst_left <= 0:
            self._burst_left = count
            logger.info(
                f"情感分析队列积压 {queued} > {threshold}, 启用快速模型 "
                f"{burst_model} 连续 {count} 次"
            )
        if self._burst_left > 0:
            self._burst_left -= 1
            self._active_model = burst_model
        else:
            self._active_model = None

    # ── 更新应用(移植 _apply_expert_updates) ─────────────────

    def apply_expert_updates(self, state: EmotionalState, updates: dict[str, Any]) -> None:
        updates = StageManager.apply_transition_benefits(state, updates)

        emotion_updates = {
            k: v for k, v in updates.items()
            if k in state.emotions.to_dict() and isinstance(v, (int, float))
        }
        state.emotions.apply_update(emotion_updates)

        for attr, delta in (("favor", updates.get("favor", 0)),
                            ("intimacy", updates.get("intimacy", 0))):
            if not isinstance(delta, (int, float)):
                continue
            if attr == "favor":
                low, high = int(self._cfg.favour_min), int(self._cfg.favour_max)
            else:
                low, high = int(self._cfg.intimacy_min), int(self._cfg.intimacy_max)
            current = getattr(state, attr)
            setattr(state, attr, max(low, min(high, current + int(delta))))

        # 互动正负分类(正负增量求和对比)
        numeric = [v for k, v in updates.items()
                   if isinstance(v, (int, float)) and k in ("favor", "intimacy", *state.emotions.to_dict())]
        total_positive = sum(v for v in numeric if v > 0)
        total_negative = sum(-v for v in numeric if v < 0)
        state.stats.record_interaction(is_positive=total_positive >= total_negative)

        # AI 生成的描述文本仅在真实 LLM 分析时覆盖
        if updates.get("source") == "llm_analysis":
            attitude = str(updates.get("attitude_text") or "").strip()[:ATTITUDE_TEXT_MAX]
            if attitude:
                state.descriptions.update_attitude(attitude)
            relationship = str(updates.get("relationship_text") or "").strip()
            if relationship:
                state.descriptions.update_relationship(relationship)

    @staticmethod
    def _calculate_significance(updates: dict[str, Any]) -> int:
        """情感意义分数: 总变化量 8+/5+/2+ → 8/5/3, 否则 1。"""
        numeric = [abs(v) for k, v in updates.items()
                   if isinstance(v, (int, float)) and k not in ("source",)]
        total = sum(numeric)
        if total >= 8:
            return 8
        if total >= 5:
            return 5
        if total >= 2:
            return 3
        return 1

    # ── 状态访问(命令用) ─────────────────────────────────────

    async def get_state(self, bot_id: str, user_id) -> EmotionalState:
        await self._store.ensure_loaded(bot_id)
        return self._get_or_create_state(bot_id, str(user_id))

    def _get_or_create_state(self, bot_id: str, user_key: str) -> EmotionalState:
        state = self._store.get_state(bot_id, user_key)
        if state is None:
            state = EmotionalState(user_key=user_key)
        return state

    async def all_states(self, bot_id: str) -> dict[str, EmotionalState]:
        await self._store.ensure_loaded(bot_id)
        return self._store.all_states(bot_id)

    async def ranking(self, bot_id: str, limit: int = 10, reverse: bool = True):
        """加权平均分排行: (favor*0.6 + intimacy*0.4) × (1 + 互动频率加成 ≤10%)。"""
        entries = []
        for user_key, state in (await self.all_states(bot_id)).items():
            avg = state.favor * 0.6 + state.intimacy * 0.4
            if state.stats.total_count > 0:
                weight = min(1.0, state.stats.total_count / 100.0)
                avg *= 1 + weight * 0.1
            entries.append((user_key, avg, state.favor, state.intimacy, state))
        entries.sort(key=lambda x: x[1], reverse=reverse)
        return entries[:max(1, min(20, limit))]

    async def set_favor(self, bot_id: str, user_id, value: int) -> EmotionalState:
        state = await self.get_state(bot_id, user_id)
        state.favor = max(int(self._cfg.favour_min),
                          min(int(self._cfg.favour_max), int(value)))
        StageManager.get_stage_info(state)
        self._store.set_state(bot_id, state.user_key, state)
        return state

    async def set_intimacy(self, bot_id: str, user_id, value: int) -> EmotionalState:
        state = await self.get_state(bot_id, user_id)
        state.intimacy = max(int(self._cfg.intimacy_min),
                             min(int(self._cfg.intimacy_max), int(value)))
        StageManager.get_stage_info(state)
        self._store.set_state(bot_id, state.user_key, state)
        return state

    async def set_attitude(self, bot_id: str, user_id, text: str) -> tuple[EmotionalState, bool]:
        state = await self.get_state(bot_id, user_id)
        ok = state.descriptions.update_attitude(text[:ATTITUDE_TEXT_MAX])
        if ok:
            self._store.set_state(bot_id, state.user_key, state)
        return state, ok

    async def reset_user(self, bot_id: str, user_id) -> EmotionalState:
        state = EmotionalState(user_key=str(user_id))
        StageManager.get_stage_info(state)
        self._store.set_state(bot_id, state.user_key, state)
        return state

    async def clear_bot(self, bot_id: str) -> None:
        self._store.clear_bot(bot_id)
        await self._store.save(bot_id)

    async def flush(self) -> None:
        await self._store.flush()

    def runtime_status(self) -> dict[str, Any]:
        """面板监控快照: 启停/分析队列/burst/熔断/存储统计。"""
        return {
            "enabled": self.enabled,
            "smart_update": bool(self._cfg.smart_update),
            "analysis_timeout_sec": int(getattr(self._cfg, "analysis_timeout_sec", 90)),
            "burst_model": str(getattr(self._cfg, "burst_model", "") or ""),
            "queue_pending": self._pending_analysis,
            "burst_left": self._burst_left,
            "active_model": self._active_model,
            "expert": self._expert.status(),
            "store": self._store.stats(),
            "memory": self._memory.stats(),
        }

    def user_memory(self, bot_id: str, user_key: str) -> list[dict[str, Any]]:
        """某用户的长期记忆记录(新→旧, 只读)。"""
        return self._memory.user_records(bot_id, str(user_key))

    def stats(self) -> dict[str, Any]:
        return {"store": self._store.stats(), "memory": self._memory.stats()}
