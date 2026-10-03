"""可选的检索改写：让小模型把会话模型写的描述改写成入库描述的风格再做向量检索。"""

from __future__ import annotations

import re
from typing import Any

from ...host import logger

FALLBACK_QUERY_REWRITE_PROMPT = (
    "将下面的表情包描述改写成标注检索风格：图上可能的文字 + 画面主体与动作和语气 + "
    "适合回应的一句话。40 字以内，只输出改写结果。\n"
    "用户消息：{user_message}\n"
    "描述：{description}"
)
MAX_INPUT_CHARS = 300
MAX_OUTPUT_CHARS = 120


def _render(template: str, **values: str) -> str:
    """只替换已知占位符，保留其他花括号。"""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return values[key] if key in values else match.group(0)

    return re.sub(r"\{([a-z_]+)\}", _replace, template)


class MemeQueryRewriter:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def enabled(self) -> bool:
        return bool(getattr(self.plugin.plugin_config, "meme_query_rewrite", False))

    def _template(self) -> str:
        custom = str(getattr(self.plugin.plugin_config, "meme_query_rewrite_prompt", "") or "")
        if custom.strip():
            return custom.strip()
        return str(
            getattr(self.plugin, "MEME_QUERY_REWRITE_PROMPT", "") or FALLBACK_QUERY_REWRITE_PROMPT
        )

    def _models(self) -> Any:
        return getattr(self.plugin, "models", None)

    async def rewrite(self, event: Any, description: str, *, user_message: str = "") -> str:
        """返回改写后的描述；关闭、失败或输出为空时原样返回。"""
        description = str(description or "").strip()
        if not description or not self.enabled():
            return description
        models = self._models()
        if models is None or not models.describe("rewrite"):
            return description
        prompt = _render(
            self._template(),
            description=description[:MAX_INPUT_CHARS],
            user_message=str(user_message or "").strip()[:MAX_INPUT_CHARS],
        )
        try:
            text = str(await models.complete(prompt, role="rewrite", max_tokens=150) or "").strip()
        except Exception as e:
            logger.warning(f"[检索改写] 小模型调用失败，使用原描述: {e}")
            return description
        text = text.strip("`\"' \n")[:MAX_OUTPUT_CHARS]
        if text:
            logger.debug(f"[检索改写] {description[:40]} -> {text[:40]}")
        return text or description
