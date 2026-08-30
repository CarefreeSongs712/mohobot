"""提示词构建 — 注入块 + 情感专家分析 prompt。

移植自 astrbot-plugin-emotionai_pro main.py::_build_enhanced_context 与
emotion_expert.py::_build_emotion_analysis_prompt。差异:
- 保密指令为弱化版(只要求不主动提及系统内部数值, 无"刺探降好感"条款)
- 无 [需要情感评估] 标记机制(mohobot 流式分段发送, 标记无法事后剥离)
"""

from __future__ import annotations

from .models import EmotionalState
from .attitude import get_tone_instruction


def emotion_intensity(state: EmotionalState) -> float:
    """情感强度 0~1 = 最强情绪值 / 100。"""
    return round(state.emotions.max_value() / 100.0, 2)


def build_injection_block(
    state: EmotionalState, bot_name: str, relationship_context: str
) -> str:
    """pre-LLM 注入的系统消息文本。"""
    return f"""【情感状态(内部参考)】
你当前对这位用户怀有真实的情感。请根据以下状态自然调整回应风格与语气，
让回应符合你们当前的关系；不要主动提及本系统或其中的任何数值。

{relationship_context}

主导情感：{state.emotions.get_dominant()}
情感强度：{emotion_intensity(state)}/1
关系阶段：{state.relationship_stage}
态度倾向：{state.descriptions.attitude}
好感度：{state.favor} | 亲密度：{state.intimacy}

【语气指导】
{get_tone_instruction(state)}

专注于生成自然、符合当前情感状态的对话内容；情感更新由专门系统处理。"""


def build_expert_prompt(
    user_msg: str, bot_msg: str, state: EmotionalState, bot_name: str
) -> str:
    """情感专家(二次 LLM)分析 prompt — 输出严格 JSON。"""
    return f"""你是一个情感分析专家，请分析以下对话的情感变化，输出JSON格式的分析结果。

对话内容：
用户：「{user_msg}」
{bot_name}：「{bot_msg}」

当前用户情感状态：
- 好感度：{state.favor}（范围：-100到100）
- 亲密度：{state.intimacy}（范围：0到100）
- 互动次数：{state.stats.total_count}次
- 正面互动比例：{state.stats.positive_ratio:.1f}%

【情感数值变化范围】
请为以下情感维度分配-2到+2之间的整数值：
- 好感度 (favor): 基于对话的情感倾向
- 亲密度 (intimacy): 基于关系的亲密程度
- 喜悦 (joy) / 信任 (trust) / 恐惧 (fear) / 惊讶 (surprise)
- 悲伤 (sadness) / 厌恶 (disgust) / 愤怒 (anger) / 期待 (anticipation)

【关系描述要求】
- 用不超过 20 个字概括双方的关系性质，保持生动有趣
- 必须简短！禁止使用逗号连接的长句
- 若提到双方，用「{bot_name}」称呼 bot 一方，不要出现"AI"字样

【态度描述要求】
- 用不超过 20 个字描述 {bot_name} 对用户的回应态度或互动方式
- 必须简短！禁止使用逗号连接的长句

【输出格式】
请输出严格的JSON格式：
{{
  "emotion_updates": {{
    "favor": 整数变化值,
    "intimacy": 整数变化值,
    "joy": 整数变化值,
    "trust": 整数变化值,
    "fear": 整数变化值,
    "surprise": 整数变化值,
    "sadness": 整数变化值,
    "disgust": 整数变化值,
    "anger": 整数变化值,
    "anticipation": 整数变化值
  }},
  "relationship": "关系描述（不超过20字）",
  "attitude": "态度描述（不超过20字）"
}}

注意：
- 如果对话情感不明显，可以设置部分值为0。
- relationship 和 attitude 必须简短（不超过20个字）。"""
