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
    ) -> None:
        if usage is None:
            return
        try:
            record = {
                "time": time.time(),
                "request_id": request_id or uuid.uuid4().hex,
                "bot_id": bot_id,
                "module": module,
                "kind": kind,
                "provider": provider,
                "model": model,
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            }
            await self._writer.append(record)
        except Exception as exc:
            logger.debug("Failed to record LLM usage: {}", exc)

    async def close(self) -> None:
        await self._writer.close()
