"""Keyword interceptor — matches incoming messages against configured keyword-reply pairs.

If a keyword is found in the message, the configured reply is sent directly,
bypassing the LLM pipeline.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from mohobot.interceptors.base import Interceptor
from mohobot.models.onebot import MessageEvent, PrivateMessageEvent, GroupMessageEvent
from mohobot.utils.cq_code import extract_plain_text


class KeywordFilter(Interceptor):
    """Matches message text against a keyword-reply dictionary."""

    def __init__(self, keyword_replies: dict[str, str] | None = None):
        self._keywords: dict[str, str] = keyword_replies or {}

    def update_keywords(self, keywords: dict[str, str]) -> None:
        """Replace the keyword map (called when bot config changes)."""
        self._keywords = keywords
        logger.debug(f"KeywordFilter updated with {len(keywords)} keywords")

    async def intercept(
        self,
        bot_id: str,
        event: MessageEvent,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | list[dict[str, Any]] | None]:
        if not self._keywords:
            return (False, None)

        text = extract_plain_text(event.message).lower()

        for keyword, reply in self._keywords.items():
            if keyword.lower() in text:
                logger.info(f"Keyword match: '{keyword}' in message from {event.user_id}")
                return (True, reply)

        return (False, None)