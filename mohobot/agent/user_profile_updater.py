"""用户画像更新 — 移植自 Agent-LuoTianyi (src/subconscious/memory/user_profile_updater.py)。

根据最近上下文判断是否更新用户画像(users.description)。
返回空串 = 不更新;非空 = 新的完整画像文本。
"""

from __future__ import annotations

import re
from typing import Any, Dict

from loguru import logger

from mohobot.agent.llm_module import LLMModule


class UserProfileUpdater:
    def __init__(self, config: Dict[str, Any], llm_module: LLMModule | None):
        self.config = config or {}
        self.llm = llm_module

    async def update_profile(
        self,
        history: Dict[str, Any],
        current_profile: str,
        character_name: str = "",
    ) -> str:
        """返回值: 空串=不修改;非空=新的完整画像。"""
        if self.llm is None:
            return ""
        try:
            history_str = (
                "更早对话总结" + (history.get("summary") or "")
                + "\n最近对话：\n"
                + "\n".join(history.get("recent_conversation", []))
            )
            response = await self.llm.generate_response(
                history=history_str or "无",
                current_profile=current_profile or "",
                character_name=character_name,
            )
        except Exception as e:
            logger.warning(f"User profile update LLM call failed: {e}")
            return ""

        normalized = self._normalize_response(response)
        if not normalized:
            return ""
        if normalized == (current_profile or "").strip():
            return ""
        return normalized

    def _normalize_response(self, response: str) -> str:
        text = (response or "").strip()
        if not text:
            return ""

        lowered = text.lower()
        no_update_tokens = {
            "no_update", "none", "null", "无需更新", "不需要更新",
            "无需修改", "不需要修改", "无", "空", "保持不变",
        }
        if lowered in no_update_tokens:
            logger.debug(f"User profile update: no update needed ('{response[:30]}')")
            return ""

        if text.startswith("```"):
            text = text.strip("`")
            text = re.sub(r"^(text|markdown)\s*", "", text, flags=re.IGNORECASE).strip()

        return "\n".join(line.rstrip() for line in text.splitlines()).strip()
