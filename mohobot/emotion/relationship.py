"""关系阶段管理 — 移植自 astrbot-plugin-emotionai_pro relationship_manager.py。

4 个正向阶段(初识/深化/承诺/共生) + 3 个负向阶段(冷淡/反感/敌对)。
复合分 = favor×阶段权重 + intimacy×阶段权重; 上升阈值 > 下降阈值(滞后带防抖)。
"""

from __future__ import annotations

from typing import Any

from .models import EmotionalState, STAGE_ORDER, STAGE_NAMES

# 阶段配置: 权重/阈值/过渡缓冲/过渡期亲密度加成系数
STAGE_CONFIGS: dict[str, dict[str, Any]] = {
    "INITIAL": {
        "name": "初识期", "description": "好感驱动，建立吸引",
        "favor_weight": 0.7, "intimacy_weight": 0.3,
        "composite_threshold": 25, "transition_buffer": 3,
        "intimacy_boost_factor": 4.0,
    },
    "DEEPENING": {
        "name": "深化期", "description": "互动平衡，共同成长",
        "favor_weight": 0.5, "intimacy_weight": 0.5,
        "composite_threshold": 55, "transition_buffer": 5,
        "intimacy_boost_factor": 3.6,
    },
    "COMMITMENT": {
        "name": "承诺期", "description": "亲密主导，根基稳固",
        "favor_weight": 0.3, "intimacy_weight": 0.7,
        "composite_threshold": 80, "transition_buffer": 7,
        "intimacy_boost_factor": 3.0,
    },
    "SYMBIOSIS": {
        "name": "共生期", "description": "完全融合，不分彼此",
        "favor_weight": 0.5, "intimacy_weight": 0.5,
        "composite_threshold": 95, "transition_buffer": 10,
        "intimacy_boost_factor": 1.0,
    },
}


def _next_stage_key(stage: str) -> str | None:
    """给定阶段的下一阶段 key; 已是最高阶段返回 None。"""
    if stage in STAGE_ORDER:
        idx = STAGE_ORDER.index(stage)
        if idx + 1 < len(STAGE_ORDER):
            return STAGE_ORDER[idx + 1]
    return None


class StageManager:
    """关系阶段计算(滞后判定 + 过渡期亲密度加成)。"""

    @classmethod
    def _get_stage_by_score(cls, composite: float, state: EmotionalState) -> str:
        """滞后版阶段判定: 升阶段用上升阈值, 降阶段用阈值−5 的滞后带。"""
        prev_stage = state.prev_stage_key or "INITIAL"
        if composite >= STAGE_CONFIGS["SYMBIOSIS"]["composite_threshold"]:
            raw_target = "SYMBIOSIS"
        elif composite >= STAGE_CONFIGS["COMMITMENT"]["composite_threshold"]:
            raw_target = "COMMITMENT"
        elif composite >= STAGE_CONFIGS["DEEPENING"]["composite_threshold"]:
            raw_target = "DEEPENING"
        else:
            raw_target = "INITIAL"

        up_threshold = STAGE_CONFIGS[raw_target]["composite_threshold"]
        down_threshold = up_threshold - 5
        use_threshold = (
            up_threshold
            if STAGE_ORDER.index(raw_target) > STAGE_ORDER.index(prev_stage)
            else down_threshold
        )
        if composite < use_threshold:
            return prev_stage
        return raw_target

    @classmethod
    def calculate_stage(cls, state: EmotionalState) -> tuple[str, dict[str, Any]]:
        """返回 (目标阶段, 过渡信息)。"""
        current_composite = cls._calculate_raw_composite(state)
        previous_stage = state.prev_stage_key or "INITIAL"
        previous_composite = state.prev_composite
        target_stage = cls._get_stage_by_score(current_composite, state)

        transition = {
            "is_transitioning": False,
            "from_stage": previous_stage,
            "to_stage": target_stage,
            "protected_composite": current_composite,
            "intimacy_boost_active": False,
            "needed_intimacy_boost": 0,
        }
        if previous_stage != target_stage:
            transition["is_transitioning"] = True
            protected = max(current_composite, previous_composite)
            transition["protected_composite"] = protected
            needed = cls._calculate_needed_intimacy(
                state, STAGE_CONFIGS[target_stage], protected
            )
            transition["needed_intimacy_boost"] = needed
            transition["intimacy_boost_active"] = needed > 0
        return target_stage, transition

    @classmethod
    def _calculate_raw_composite(cls, state: EmotionalState) -> float:
        """先按简单加权(0.6/0.4)定位裸阶段, 再按该阶段权重算复合分。"""
        rough = state.favor * 0.6 + state.intimacy * 0.4
        stage = cls._get_stage_by_score(rough, state)
        cfg = STAGE_CONFIGS[stage]
        return state.favor * cfg["favor_weight"] + state.intimacy * cfg["intimacy_weight"]

    @classmethod
    def _calculate_needed_intimacy(
        cls, state: EmotionalState, target_cfg: dict[str, Any], protected: float
    ) -> int:
        int_weight = target_cfg["intimacy_weight"]
        if int_weight == 0:
            return 0
        needed = (protected - state.favor * target_cfg["favor_weight"]) / int_weight
        needed = int(max(0, min(100, needed)))
        return max(0, needed - state.intimacy)

    @classmethod
    def get_stage_info(cls, state: EmotionalState) -> dict[str, Any]:
        """完整阶段信息(负好感走独立的负向阶段)。"""
        if state.favor < 0:
            return cls._negative_stage_info(state)

        target_stage, transition = cls.calculate_stage(state)
        cfg = STAGE_CONFIGS[target_stage]
        composite = transition["protected_composite"]

        progress = (composite / cfg["composite_threshold"]) * 100
        next_key = _next_stage_key(target_stage)
        if next_key:
            next_threshold: int | None = STAGE_CONFIGS[next_key]["composite_threshold"]
            next_name: str = STAGE_CONFIGS[next_key]["name"]
        else:
            next_threshold = None
            next_name = "已达最高阶段"

        # 记录本次判定, 供下一次滞后带使用
        state.prev_stage_key = target_stage
        state.prev_composite = composite
        state.relationship_stage = cfg["name"]
        state.stage_composite_score = composite
        state.stage_progress = max(0.0, min(100.0, progress))

        return {
            "stage": target_stage,
            "stage_name": cfg["name"],
            "description": cfg["description"],
            "composite_score": composite,
            "current_stage_threshold": cfg["composite_threshold"],
            "next_stage_threshold": next_threshold,
            "next_stage_name": next_name,
            "is_max_stage": target_stage == "SYMBIOSIS",
            "progress_to_next": max(0.0, min(100.0, progress)),
            "is_transitioning": transition["is_transitioning"],
            "intimacy_boost_active": transition["intimacy_boost_active"],
            "needed_intimacy_boost": transition["needed_intimacy_boost"],
        }

    @classmethod
    def _negative_stage_info(cls, state: EmotionalState) -> dict[str, Any]:
        composite = float(state.favor)
        if state.favor >= -30:
            stage_name, description = "冷淡期", "关系冷淡，需要修复"
            progress = max(0.0, (state.favor + 30) / 30 * 100)
        elif state.favor >= -70:
            stage_name, description = "反感期", "存在反感情绪"
            progress = max(0.0, (state.favor + 70) / 40 * 100)
        else:
            stage_name, description = "敌对期", "关系敌对"
            progress = 0.0

        state.prev_stage_key = "INITIAL"
        state.prev_composite = composite
        state.relationship_stage = stage_name
        state.stage_composite_score = composite
        state.stage_progress = max(0.0, min(100.0, progress))

        return {
            "stage": None,
            "stage_name": stage_name,
            "description": description,
            "composite_score": composite,
            "current_stage_threshold": 0,
            "next_stage_threshold": None,
            "next_stage_name": "恢复正常关系",
            "is_max_stage": False,
            "progress_to_next": max(0.0, min(100.0, progress)),
            "is_transitioning": False,
            "intimacy_boost_active": False,
            "needed_intimacy_boost": 0,
        }

    @classmethod
    def apply_transition_benefits(
        cls, state: EmotionalState, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """过渡期增益: 亲密度增量按阶段系数放大; 正向情绪自动补亲密度。"""
        _, transition = cls.calculate_stage(state)
        if not transition["intimacy_boost_active"]:
            return updates
        cfg = STAGE_CONFIGS[transition["to_stage"]]
        boost = cfg["intimacy_boost_factor"]

        if "intimacy" in updates:
            updates["intimacy"] = int(updates["intimacy"] * boost)
        elif any(k in updates for k in ("joy", "trust", "anticipation")):
            updates["intimacy"] = updates.get("intimacy", 0) + max(1, int(2 * boost))
        return updates

    @classmethod
    def get_stage_advice(cls, state: EmotionalState) -> str:
        """阶段进阶建议。"""
        if state.favor < 0:
            if state.favor >= -30:
                return "冷淡期：需要真诚道歉和积极行动来修复关系，避免进一步恶化。"
            if state.favor >= -70:
                return "反感期：需要时间和耐心来缓解负面情绪，避免直接冲突。"
            return "敌对期：关系极度紧张，需要保持距离或寻求第三方调解。"

        info = cls.get_stage_info(state)
        if info["is_transitioning"] and info["intimacy_boost_active"]:
            return (
                f"【阶段过渡中】{info['stage_name']}\n"
                f"当前需要提升亲密度 {info['needed_intimacy_boost']} 点来适应新阶段\n"
                f"建议: 多进行深度交流，分享个人经历和情感"
            )

        advice = {
            "INITIAL": "初识期：多展示个人魅力，建立良好第一印象。通过有趣的话题和积极的互动提升好感度。",
            "DEEPENING": "深化期：分享更多个人经历和情感，建立信任基础。共同经历和深度交流是关键。",
            "COMMITMENT": "承诺期：巩固信任和默契，在困难时刻相互支持。关系的深度比广度更重要。",
            "SYMBIOSIS": "共生期：维持情感的深度连接，共同成长和创造美好回忆。",
        }
        return advice.get(info["stage"], "继续培养这段关系吧！")
