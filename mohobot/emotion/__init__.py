"""情感系统 — 移植自 astrbot-plugin-emotionai_pro(核心子集)。

功能: 每用户好感度/亲密度、8 维情绪、关系阶段、态度语气提示注入、
长期互动记忆、二次 LLM 情感分析(独立模型可配)。
由全局配置 emotion.enabled 开关(启动时读取, 修改后重启生效)。
"""

from .manager import EmotionManager

__all__ = ["EmotionManager"]
