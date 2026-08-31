"""Unified asynchronous token usage recording."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.file_store import JSONLWriter


class UsageRecorder:
    """Writes one compatible JSONL record for each provider request."""

    def __init__(self, data_dir: str = "./data") -> None:
        self._writer = JSONLWriter(Path(data_dir) / "stats" / "llm_usage.jsonl")

    async def record(
        self,
        usage: Any,
        *,
        model: str,
        bot_id: str = "",
        module: str = "chat",
        kind: str = "chat",
        request_id: str | None = None,
        provider: str = "openai-compatible",
        chat_type: str = "",
        chat_id: str = "",
        user_id: str = "",
    ) -> None:
        if usage is None:
            return
        try:
            # 缓存命中 token: OpenAI 风格 prompt_tokens_details.cached_tokens,
            # DeepSeek 风格 prompt_cache_hit_tokens; 两者都没有则记 0
            details = getattr(usage, "prompt_tokens_details", None)
            cached = int(getattr(details, "cached_tokens", 0) or 0)
            if not cached:
                cached = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
            record = {
                "time": time.time(),
                "request_id": request_id or uuid.uuid4().hex,
                "bot_id": bot_id,
                "module": module,
                "kind": kind,
                "provider": provider,
                "model": model,
                "chat_type": chat_type,
                "chat_id": chat_id,
                "user_id": user_id,
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
                "cached_tokens": cached,
            }
            await self._writer.append(record)
        except Exception as exc:
            logger.debug("Failed to record LLM usage: {}", exc)

    async def close(self) -> None:
        await self._writer.close()
