"""Interceptor protocol base class.

Interceptors sit in the message pipeline between event classification and LLM inference.
Each can optionally handle a message, returning (handled, response).
If handled=True, the pipeline stops and sends the response directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Interceptor(ABC):
    """Base class for message interceptors."""

    @abstractmethod
    async def intercept(
        self,
        bot_id: str,
        event: Any,  # MessageEvent
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | list[dict[str, Any]] | None]:
        """Process an incoming message.

        Returns:
            (handled, response):
                handled=True  → pipeline stops, response sent to user
                handled=False → pipeline continues to next interceptor / LLM
                response: the reply message (string or array format), or None
        """
        ...