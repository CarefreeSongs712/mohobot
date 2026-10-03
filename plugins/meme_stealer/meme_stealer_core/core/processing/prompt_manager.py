"""提示词管理器：负责加载、缓存和渲染 VLM 审核+标注提示词模板。"""

from typing import Any


class PromptManager:
    """管理表情包审核+标注所需的 VLM 提示词。

    审核与标注合并为一次调用，不再做情绪分类。模板支持两个可选占位符：
    - ``{chat_context}``：图片发出前的聊天记录；缺省时追加在模板末尾。
    - ``{emotion_list}``：兼容旧自定义提示词，替换为当前分类列表。
    """

    _CONTEXT_PLACEHOLDER = "{chat_context}"
    _LEGACY_CATEGORY_PLACEHOLDER = "{emotion_list}"
    _NO_CONTEXT_TEXT = "（无聊天记录）"

    _FALLBACK_PROMPT = (
        "先审核这张表情包：含裸露色情、暴力血腥、政治敏感或广告营销内容时，"
        '只输出 {"approved": false, "reason": "审核不通过"}。'
        "否则输出 JSON："
        '{"approved": true, "overlay_text": "图上文字", "description": "画面描述", '
        '"tags": [], "scenes": []}。'
        "聊天记录只用于理解图片含义，不要把其中的人名或具体事件写进任何字段。\n"
        "{chat_context}"
    )

    def __init__(self, plugin_instance: Any) -> None:
        self.plugin = plugin_instance
        self.plugin_config = getattr(plugin_instance, "plugin_config", None)

        self.emoji_classification_prompt = getattr(
            plugin_instance, "EMOJI_CLASSIFICATION_PROMPT", self._FALLBACK_PROMPT
        )
        self.categories = list(self.plugin_config.categories or []) if self.plugin_config else []

    def update_config(self, categories=None, emoji_classification_prompt=None) -> None:
        if categories is not None:
            self.categories = categories
        if emoji_classification_prompt is not None:
            self.emoji_classification_prompt = emoji_classification_prompt

    def build_classification_prompt(self, *, chat_context: str = "") -> str:
        """渲染完整的 VLM 审核+标注提示词。"""
        template = self.emoji_classification_prompt or self._FALLBACK_PROMPT
        if self._LEGACY_CATEGORY_PLACEHOLDER in template:
            template = template.replace(
                self._LEGACY_CATEGORY_PLACEHOLDER, self._build_emotion_list_str()
            )
        context_text = str(chat_context or "").strip()
        if self._CONTEXT_PLACEHOLDER in template:
            return template.replace(
                self._CONTEXT_PLACEHOLDER, context_text or self._NO_CONTEXT_TEXT
            )
        if not context_text:
            return template
        return f"{template.rstrip()}\n\n{context_text}"

    def _build_emotion_list_str(self) -> str:
        categories = [c for c in (self.categories or []) if isinstance(c, str) and c.strip()]
        info_map = getattr(self.plugin_config, "category_info", None) or {}

        lines = []
        for raw_key in categories:
            key = raw_key.strip()
            info = info_map.get(key)
            name = str(info.get("name", "")).strip() if isinstance(info, dict) else ""
            desc = str(info.get("desc", "")).strip() if isinstance(info, dict) else ""
            label = f"{key} - {name}" if name and name != key else key
            lines.append(f"{label}：{desc}" if desc else label)
        return "\n".join(lines)
