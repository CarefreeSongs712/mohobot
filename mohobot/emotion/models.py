"""情感系统数据模型 — 移植自 astrbot-plugin-emotionai_pro models.py。

per-user 情感状态: 好感度/亲密度 + 8 维情绪 + 互动统计 + AI 生成的
态度/关系描述文本。数值超范围时静默 clamp(不抛异常)。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, ClassVar

# ── 数值边界 ──────────────────────────────────────────────────
MIN_FAVOR, MAX_FAVOR = -100, 100
MIN_INTIMACY, MAX_INTIMACY = 0, 100
MIN_EMOTION, MAX_EMOTION = 0, 100

# 专家单次增量上限(原插件硬编码 ±5/±3, 这里收敛为常量)
FAVOR_DELTA_LIMIT = 5
EMOTION_DELTA_LIMIT = 3

# 描述文本长度上限(专家 prompt 也按此约束)
ATTITUDE_TEXT_MAX = 20
RELATIONSHIP_TEXT_MAX = 20

# ── 时间常量(秒) ──────────────────────────────────────────────
ONE_MINUTE = 60
THIRTY_MINUTES = 1800
ONE_HOUR = 3600
ONE_DAY = 86400

EMOTION_FIELDS = (
    "joy", "trust", "fear", "surprise",
    "sadness", "disgust", "anger", "anticipation",
)
EMOTION_NAMES = {
    "joy": "喜悦", "trust": "信任", "fear": "恐惧", "surprise": "惊讶",
    "sadness": "悲伤", "disgust": "厌恶", "anger": "愤怒", "anticipation": "期待",
}

STAGE_ORDER = ("INITIAL", "DEEPENING", "COMMITMENT", "SYMBIOSIS")
STAGE_NAMES = {
    "INITIAL": "初识期", "DEEPENING": "深化期",
    "COMMITMENT": "承诺期", "SYMBIOSIS": "共生期",
    "COLD": "冷淡期", "DISLIKE": "反感期", "HOSTILE": "敌对期",
}
VALID_STAGES = list(STAGE_NAMES.values())


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass
class EmotionalMetrics:
    """8 维情感指标。"""
    joy: int = 0
    trust: int = 0
    fear: int = 0
    surprise: int = 0
    sadness: int = 0
    disgust: int = 0
    anger: int = 0
    anticipation: int = 0

    def apply_update(self, updates: dict[str, int]) -> None:
        """应用情绪增量(逐维 clamp)。"""
        for name, change in updates.items():
            if name not in EMOTION_FIELDS:
                continue
            current = getattr(self, name)
            setattr(self, name, _clamp(current + int(change), MIN_EMOTION, MAX_EMOTION))

    def get_dominant(self) -> str:
        """主导情感(并列时返回复合描述)。"""
        values = {EMOTION_NAMES[f]: getattr(self, f) for f in EMOTION_FIELDS}
        max_value = max(values.values())
        if max_value == 0:
            return "中立"
        dominant = [name for name, v in values.items() if v == max_value]
        if len(dominant) == 1:
            return dominant[0]
        return f"复合({'+'.join(dominant)})"

    def max_value(self) -> int:
        return max(getattr(self, f) for f in EMOTION_FIELDS)

    def to_dict(self) -> dict[str, int]:
        return {f: getattr(self, f) for f in EMOTION_FIELDS}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EmotionalMetrics":
        data = data or {}
        return cls(**{
            f: _clamp(int(data.get(f, 0) or 0), MIN_EMOTION, MAX_EMOTION)
            for f in EMOTION_FIELDS
        })


@dataclass
class InteractionStats:
    """互动统计。"""
    total_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    last_interaction_time: float = 0.0

    def record_interaction(self, is_positive: bool) -> None:
        self.total_count += 1
        if is_positive:
            self.positive_count += 1
        else:
            self.negative_count += 1
        self.last_interaction_time = time.time()

    @property
    def positive_ratio(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.positive_count / self.total_count * 100

    @property
    def days_since_last(self) -> float:
        if self.last_interaction_time == 0:
            return float("inf")
        return (time.time() - self.last_interaction_time) / 86400

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "InteractionStats":
        data = data or {}
        total = max(0, int(data.get("total_count", 0) or 0))
        positive = max(0, int(data.get("positive_count", 0) or 0))
        negative = max(0, int(data.get("negative_count", 0) or 0))
        if positive + negative > total:
            total = positive + negative
        return cls(
            total_count=total,
            positive_count=positive,
            negative_count=negative,
            last_interaction_time=max(0.0, float(data.get("last_interaction_time", 0) or 0)),
        )


@dataclass
class TextDescriptions:
    """AI 生成的态度/关系描述文本(仅 llm_analysis 来源会覆盖)。"""
    attitude: str = "中立"
    relationship: str = "陌生人"
    last_attitude_update: float = 0.0
    last_relationship_update: float = 0.0

    # 汉字/字母数字/空格/常见中文标点; 不放行逗号长句会被误拒, 必须放行
    VALID_PATTERN: ClassVar[str] = (
        r'^[\w\-\s\u4e00-\u9fa5\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a'
        r'\u201c\u201d\u2018\u2019\u300a\u300b\u00b7]{1,50}$'
    )

    def _valid(self, text: str) -> bool:
        return bool(text) and bool(re.match(self.VALID_PATTERN, text))

    def update_attitude(self, text: str) -> bool:
        if not self._valid(text):
            return False
        self.attitude = text
        self.last_attitude_update = time.time()
        return True

    def update_relationship(self, text: str) -> bool:
        if not self._valid(text):
            return False
        self.relationship = text
        self.last_relationship_update = time.time()
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TextDescriptions":
        data = data or {}
        desc = cls(
            attitude=str(data.get("attitude", "中立") or "中立"),
            relationship=str(data.get("relationship", "陌生人") or "陌生人"),
            last_attitude_update=float(data.get("last_attitude_update", 0) or 0),
            last_relationship_update=float(data.get("last_relationship_update", 0) or 0),
        )
        if not desc._valid(desc.attitude):
            desc.attitude = "中立"
        if not desc._valid(desc.relationship):
            desc.relationship = "陌生人"
        return desc


@dataclass
class EmotionalState:
    """单个用户(bot 维度隔离)的情感状态。"""
    user_key: str = ""
    favor: int = 0
    intimacy: int = 0
    emotions: EmotionalMetrics = field(default_factory=EmotionalMetrics)
    stats: InteractionStats = field(default_factory=InteractionStats)
    descriptions: TextDescriptions = field(default_factory=TextDescriptions)

    relationship_stage: str = "初识期"
    stage_composite_score: float = 0.0
    stage_progress: float = 0.0

    force_update_counter: int = 0
    last_force_update: float = 0.0

    # 阶段滞后判定用的运行时状态(不序列化, 重启后从初识期重新判定)
    prev_stage_key: str = ""
    prev_composite: float = 0.0

    def __post_init__(self):
        self.favor = _clamp(int(self.favor), MIN_FAVOR, MAX_FAVOR)
        self.intimacy = _clamp(int(self.intimacy), MIN_INTIMACY, MAX_INTIMACY)
        self.force_update_counter = max(0, int(self.force_update_counter))
        if self.relationship_stage not in VALID_STAGES:
            self.relationship_stage = "敌对期" if self.favor < -70 else (
                "反感期" if self.favor < -30 else ("冷淡期" if self.favor < 0 else "初识期")
            )

    def should_force_update(self, force_interval: int) -> bool:
        """强制更新检查: 计数器满 N 轮, 或距上次强制更新超过 30 分钟。"""
        if self.force_update_counter >= max(1, int(force_interval)):
            return True
        return time.time() - self.last_force_update > THIRTY_MINUTES

    def reset_force_update_counter(self) -> None:
        self.force_update_counter = 0
        self.last_force_update = time.time()

    def is_initial(self) -> bool:
        return (
            self.favor == 0 and self.intimacy == 0
            and self.stats.total_count == 0
            and self.relationship_stage == "初识期"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_key": self.user_key,
            "favor": self.favor,
            "intimacy": self.intimacy,
            "emotions": self.emotions.to_dict(),
            "stats": self.stats.to_dict(),
            "descriptions": self.descriptions.to_dict(),
            "relationship_stage": self.relationship_stage,
            "stage_composite_score": self.stage_composite_score,
            "stage_progress": self.stage_progress,
            "force_update_counter": self.force_update_counter,
            "last_force_update": self.last_force_update,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmotionalState":
        try:
            return cls(
                user_key=str(data.get("user_key", "") or ""),
                favor=int(data.get("favor", 0) or 0),
                intimacy=int(data.get("intimacy", 0) or 0),
                emotions=EmotionalMetrics.from_dict(data.get("emotions")),
                stats=InteractionStats.from_dict(data.get("stats")),
                descriptions=TextDescriptions.from_dict(data.get("descriptions")),
                relationship_stage=str(data.get("relationship_stage", "初识期") or "初识期"),
                stage_composite_score=float(data.get("stage_composite_score", 0) or 0),
                stage_progress=float(data.get("stage_progress", 0) or 0),
                force_update_counter=int(data.get("force_update_counter", 0) or 0),
                last_force_update=float(data.get("last_force_update", 0) or 0),
            )
        except (TypeError, ValueError):
            return cls(user_key=str(data.get("user_key", "") or ""))
