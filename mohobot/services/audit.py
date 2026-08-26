"""Persistent, redacted Web administration audit log."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mohobot.file_store import JSONLWriter

_SECRET_MARKERS = ("password", "api_key", "token", "cookie", "secret", "authorization")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "***" if any(marker in str(key).lower() for marker in _SECRET_MARKERS) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class AuditLogger:
    def __init__(self, data_dir: str = "./data") -> None:
        self._writer = JSONLWriter(Path(data_dir) / "audit" / "web_admin.jsonl")

    async def write(
        self,
        *,
        actor: str,
        action: str,
        target: str = "",
        success: bool,
        details: Any = None,
        remote: str = "",
    ) -> None:
        await self._writer.append({
            "time": time.time(),
            "actor": actor,
            "action": action,
            "target": target,
            "success": success,
            "remote": remote,
            "details": redact(details),
        })

    async def close(self) -> None:
        await self._writer.close()
