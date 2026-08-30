"""智能更新决策 — 移植自 astrbot-plugin-emotionai_pro managers.py SmartUpdateManager。

三维判断(原版的主 LLM `[需要情感评估]` 标记维度因流式发送不兼容而移除):
1. 对话内容关键词/语气/表情启发式
2. 距上次情感更新的时间间隔
3. 每 N 轮强制更新
"""

from __future__ import annotations

import re
import time
from typing import Any

from .models import EmotionalState, ONE_DAY, THIRTY_MINUTES

MAJOR_CHANGE = 8  # 情绪极差达到该值视为"情感强度重大变化"


class SmartUpdateManager:
    """判断一轮对话是否需要调用情感分析 LLM。"""

    EMOTIONAL_KEYWORDS: dict[str, list[str]] = {
        "positive": ["喜欢", "爱", "开心", "高兴", "谢谢", "感谢", "感动",
                     "温暖", "棒", "好", "不错", "可爱", "漂亮", "美丽"],
        "negative": ["讨厌", "恨", "生气", "愤怒", "伤心", "难过", "失望",
                     "烦", "滚", "傻", "笨", "蠢", "垃圾", "不愿意"],
        "intimate": ["想你", "想念", "关心", "担心", "在乎", "重要",
                     "宝贝", "亲爱的", "搞好关系", "拥抱", "吻"],
        "conflict": ["吵架", "争执", "不满", "抱怨", "批评", "指责", "反对", "不同意"],
    }

    INTENSITY_PATTERNS = {
        "strong_positive": re.compile(r"(非常|特别|极其|太|真的)好|喜欢|爱|开心"),
        "strong_negative": re.compile(r"(非常|特别|极其|太|真的)讨厌|恨|生气|烦"),
        "question": re.compile(r"[？?]"),
        "exclamation": re.compile(r"[！!]"),
        "emoticon_positive": re.compile(r"[:：][)）]|😊|😄|😍|🥰|🤗"),
        "emoticon_negative": re.compile(r"[:：][(（]|😠|😡|😢|😭|😤"),
    }

    def should_update(
        self, state: EmotionalState, user_message: str, ai_response: str,
        force_interval: int,
    ) -> tuple[bool, str]:
        """返回 (是否更新, 原因)。"""
        reasons: list[str] = []

        # 1. 情感强度(8 维极差)
        emotions = [getattr(state.emotions, f) for f in state.emotions.to_dict()]
        if max(emotions) - min(emotions) >= MAJOR_CHANGE:
            reasons.append("情感强度重大变化")

        # 2. 关键词/语气分析
        keyword = self._analyze_keywords(user_message, ai_response)
        if keyword["should_update"]:
            reasons.append(keyword["reason"])

        # 3. 长时间未更新
        if self._is_stale(state):
            reasons.append("长时间未更新")

        # 4. 强制更新(每 N 轮 / 距上次超过 30 分钟)
        if state.should_force_update(force_interval):
            reasons.append("强制更新机制")

        # 5. 久别重逢
        if state.stats.total_count > 0 and state.stats.days_since_last > 7:
            reasons.append("久别重逢")

        return (True, " | ".join(reasons)) if reasons else (False, "无明显情感变化")

    def _analyze_keywords(self, user_message: str, ai_response: str) -> dict[str, Any]:
        result: dict[str, Any] = {"should_update": False, "reason": ""}
        user_lower = (user_message or "").lower()
        reply_lower = (ai_response or "").lower()

        intensity = 0.0
        detected: set[str] = set()
        for category, keywords in self.EMOTIONAL_KEYWORDS.items():
            for kw in keywords:
                if kw in user_lower:
                    detected.add(category)
                    intensity += {"positive": 2, "negative": 3, "intimate": 2, "conflict": 3}[category]
                if kw in reply_lower:
                    detected.add(category)
                    intensity += 1

        for name, pattern in self.INTENSITY_PATTERNS.items():
            if pattern.search(user_message or "") or pattern.search(ai_response or ""):
                if "strong" in name:
                    intensity += 2
                elif "emoticon" in name:
                    intensity += 1
                elif name == "question":
                    intensity += 0.5
                elif name == "exclamation":
                    intensity += 1

        if intensity >= 2:
            result["should_update"] = True
            if "negative" in detected and "conflict" in detected:
                result["reason"] = "用户表达强烈负面情感和冲突"
            elif "negative" in detected:
                result["reason"] = "用户表达负面情感"
            elif "positive" in detected and "intimate" in detected:
                result["reason"] = "用户表达积极亲密情感"
            elif "positive" in detected:
                result["reason"] = "用户表达积极情感"
            elif "intimate" in detected:
                result["reason"] = "用户表达亲密情感"
            else:
                result["reason"] = "对话包含情感关键词"
        return result

    @staticmethod
    def _is_stale(state: EmotionalState) -> bool:
        now = time.time()
        if now - state.descriptions.last_attitude_update > ONE_DAY:
            return True
        if now - state.descriptions.last_relationship_update > ONE_DAY:
            return True
        return now - state.last_force_update > THIRTY_MINUTES * 2
