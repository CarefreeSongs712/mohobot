"""情感分析专家(二次 LLM) — 移植自 astrbot-plugin-emotionai_pro emotion_expert.py。

llm_call 由 LLMService.analyze_emotion 注入(独立情感模型, 缺省回退 chat 模型)。
LLM 连续失败 3 次后自动停用, 改走关键词降级(smart fallback)。
原版的 md5 结果缓存省略 — 更新决策已按轮去重, 同一轮不会重复分析。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

from loguru import logger

from .models import (
    EmotionalState, EMOTION_FIELDS,
    FAVOR_DELTA_LIMIT, EMOTION_DELTA_LIMIT,
    ATTITUDE_TEXT_MAX, RELATIONSHIP_TEXT_MAX,
)

# 连续失败 N 次后停用 LLM, 全部走关键词降级
LLM_MAX_CONSECUTIVE_FAILURES = 3


class EmotionExpert:
    """调用二次 LLM 分析一轮对话的情感增量, 返回带 source 标记的更新字典。"""

    def __init__(
        self,
        llm_call: Callable[[str], Awaitable[str | None]],
        timeout: float = 30.0,
        retries: int = 2,
        retry_delay: float = 1.0,
    ) -> None:
        self._llm_call = llm_call
        self._timeout = timeout
        self._retries = max(1, retries)
        self._retry_delay = retry_delay
        self._llm_available = True
        self._llm_failures = 0

    async def analyze(
        self, user_msg: str, bot_reply: str, state: EmotionalState, bot_name: str = "AI"
    ) -> dict[str, Any]:
        """分析入口: 返回 {favor, intimacy, 8 情绪, relationship_text, attitude_text, source}。"""
        try:
            if self._llm_available:
                text = await self._call_llm_with_retry(user_msg, bot_reply, state, bot_name)
                if text:
                    updates = self._parse(text)
                    updates["source"] = "llm_analysis"
                    self._llm_failures = 0
                    return self._ensure_completeness(updates, state)
                # LLM 不可用或返回空
                self._record_failure()
            else:
                self._record_failure_quiet()
        except Exception as e:
            logger.warning(f"情感分析异常, 使用降级方案: {e}")
            self._record_failure()

        updates = self._smart_fallback(user_msg, bot_reply)
        updates["source"] = "smart_fallback"
        return self._ensure_completeness(updates, state)

    def reset_llm_availability(self) -> None:
        self._llm_available = True
        self._llm_failures = 0

    # ── LLM 调用 ─────────────────────────────────────────────

    async def _call_llm_with_retry(
        self, user_msg: str, bot_reply: str, state: EmotionalState, bot_name: str
    ) -> str | None:
        from .prompts import build_expert_prompt
        prompt = build_expert_prompt(user_msg, bot_reply, state, bot_name or "AI")
        for attempt in range(self._retries):
            try:
                result = await asyncio.wait_for(
                    self._llm_call(prompt), timeout=self._timeout
                )
                if result and len(result.strip()) > 10:
                    return result.strip()
            except asyncio.TimeoutError:
                logger.warning(f"情感分析 LLM 超时 (尝试 {attempt + 1}/{self._retries})")
            except Exception as e:
                logger.warning(f"情感分析 LLM 异常 (尝试 {attempt + 1}/{self._retries}): {e}")
            if attempt < self._retries - 1:
                await asyncio.sleep(self._retry_delay * (attempt + 1))
        return None

    def _record_failure(self) -> None:
        self._llm_failures += 1
        if self._llm_failures >= LLM_MAX_CONSECUTIVE_FAILURES:
            self._llm_available = False
            logger.warning("情感分析 LLM 连续失败, 已停用(改用关键词降级)")

    def _record_failure_quiet(self) -> None:
        self._llm_failures += 1

    # ── 解析 ─────────────────────────────────────────────────

    def _parse(self, analysis_text: str) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        cleaned = self._clean_json(analysis_text)
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            fixed = self._fix_json_errors(match.group())
            data = json.loads(fixed)
            if "emotion_updates" not in data:
                raise ValueError("缺少 emotion_updates 字段")
            raw = data["emotion_updates"]
            for name in ("favor", "intimacy", *EMOTION_FIELDS):
                value = raw.get(name, 0)
                if isinstance(value, (int, float)):
                    limit = FAVOR_DELTA_LIMIT if name in ("favor", "intimacy") else EMOTION_DELTA_LIMIT
                    updates[name] = max(-limit, min(limit, int(value)))
                else:
                    updates[name] = 0
            updates["relationship_text"] = str(
                data.get("relationship") or "正常关系"
            ).strip()[:RELATIONSHIP_TEXT_MAX]
            updates["attitude_text"] = str(
                data.get("attitude") or "友好交流"
            ).strip()[:ATTITUDE_TEXT_MAX]
            return updates
        raise ValueError("响应中未找到 JSON")

    @staticmethod
    def _clean_json(text: str) -> str:
        text = re.sub(r"```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*", "", text)
        return text.strip()

    @staticmethod
    def _fix_json_errors(json_str: str) -> str:
        # 未加引号的键 → 补引号; 单引号 → 双引号; 去尾逗号; 补齐大括号
        json_str = re.sub(r"([{,]\s*)(\w+)(\s*:)", r'\1"\2"\3', json_str)
        json_str = json_str.replace("'", '"')
        json_str = re.sub(r",\s*}", "}", json_str)
        json_str = re.sub(r",\s*]", "]", json_str)
        opens, closes = json_str.count("{"), json_str.count("}")
        if opens > closes:
            json_str += "}" * (opens - closes)
        return json_str

    # ── 降级方案 ─────────────────────────────────────────────

    @staticmethod
    def _smart_fallback(user_msg: str, bot_reply: str) -> dict[str, Any]:
        """关键词加权的数值降级(不产生描述文本覆盖)。"""
        user_lower = (user_msg or "").lower()
        reply_lower = (bot_reply or "").lower()

        positive = ["好", "开心", "高兴", "谢谢", "感谢", "喜欢", "爱",
                    "不错", "棒", "可爱", "漂亮", "美丽", "相信"]
        negative = ["讨厌", "生气", "愤怒", "烦", "恨", "滚", "傻", "笨", "蠢", "垃圾", "不愿意"]
        intimate = ["想你", "想念", "关心", "担心", "在乎", "重要",
                    "宝贝", "亲爱的", "搞好关系"]

        pos = sum(3 for w in positive if w in user_lower) + sum(1 for w in positive if w in reply_lower)
        neg = sum(3 for w in negative if w in user_lower) + sum(1 for w in negative if w in reply_lower)
        intimate_weight = sum(2 for w in intimate if w in user_lower) + sum(1 for w in intimate if w in reply_lower)

        if neg > pos and neg > 0:
            strength = min(3, neg)
            return {
                "favor": -strength, "intimacy": -1,
                "sadness": 2, "anger": 1, "disgust": 1,
            }
        if pos > neg and pos > 0:
            strength = min(3, pos)
            return {
                "favor": strength, "intimacy": 2 if intimate_weight > 0 else 1,
                "joy": 2, "trust": 1, "anticipation": 1,
            }
        if intimate_weight > 0:
            return {"favor": 1, "intimacy": 3, "joy": 2, "trust": 2, "anticipation": 1}
        return {"favor": 0, "intimacy": 0, "anticipation": 1}

    @staticmethod
    def _ensure_completeness(updates: dict[str, Any], state: EmotionalState) -> dict[str, Any]:
        for name in ("favor", "intimacy", *EMOTION_FIELDS):
            updates.setdefault(name, 0)
        updates.setdefault("relationship_text", state.descriptions.relationship)
        updates.setdefault("attitude_text", state.descriptions.attitude)
        updates.setdefault("source", "unknown")
        return updates
