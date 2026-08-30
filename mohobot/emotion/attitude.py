"""态度/语气指导 — 移植自 astrbot-plugin-emotionai_pro attitude_manager.py。

纯文本映射: 复合分 → 行为风格; 主导情绪 → 语气指令(注入 LLM 提示用)。
"""

from __future__ import annotations

from .models import EmotionalState, EMOTION_FIELDS

BEHAVIOR_STYLES = {
    "hostile": "极其简短、尖锐，可能拒绝回应",
    "cold": "冷淡、简洁、不耐烦",
    "neutral": "客观、保持距离、标准化回应",
    "friendly": "积极、乐于协助、带有正面情绪",
    "intimate": "热情、主动、富有情感，可使用亲昵称呼",
}

TONE_INSTRUCTIONS = {
    "joy": "你的语气应该是愉快、充满热情和活力的。多使用积极的词汇和表情符号。",
    "trust": "你的语气应该是平和、真诚且令人安心的。展现出你的可靠和支持。",
    "fear": "你的语气应该显得有些紧张、谨慎或不安。表现出犹豫或退缩。",
    "surprise": "你的语气应该是震惊、难以置信或充满好奇的。多使用感叹号和反问。",
    "sadness": "你的语气应该是低落、消沉和遗憾的。句子要简短，带有一种无力感。",
    "disgust": "你的语气应该是厌烦、抗拒甚至带有生理性不适的。表现出想回避的态度。",
    "anger": "你的语气应该是愤怒、急躁和具有攻击性的。使用简短有力的句子，表现出不耐烦。",
    "anticipation": "你的语气应该是期待、急切和向往的。关注未来的可能性。",
}


def get_behavior_guidance(favor: int, intimacy: int) -> str:
    """复合分(0.6/0.4 加权) → 行为风格句。"""
    composite = favor * 0.6 + intimacy * 0.4
    if composite >= 75:
        return BEHAVIOR_STYLES["intimate"]
    if composite >= 40:
        return BEHAVIOR_STYLES["friendly"]
    if composite >= -10:
        return BEHAVIOR_STYLES["neutral"]
    if composite >= -50:
        return BEHAVIOR_STYLES["cold"]
    return BEHAVIOR_STYLES["hostile"]


def get_tone_instruction(state: EmotionalState) -> str:
    """主导情绪(>30) → 对应语气句; 否则按好感度给兜底语气。"""
    dominant_name, dominant_value = max(
        ((f, getattr(state.emotions, f)) for f in EMOTION_FIELDS),
        key=lambda x: x[1],
    )
    if dominant_value > 30:
        return TONE_INSTRUCTIONS.get(dominant_name, "保持自然友好的语气。")
    if state.favor >= 40:
        return "保持友好积极的语气。"
    if state.favor >= -10:
        return "保持中立客观的语气。"
    return "保持简洁冷淡的语气。"
