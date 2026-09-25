"""Shared group archives; private history remains isolated by bot.

Group writers are shared by incoming and outgoing paths. Identity is scoped to
one group: message_id first, otherwise timestamp + sender + message content.
Legacy files remain read-only and are included by history readers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import weakref
from pathlib import Path

from mohobot.file_store import JSONLWriter

MERGED_BOT_ID = "_merged"
_writers: weakref.WeakValueDictionary[str, GroupHistoryWriter] = weakref.WeakValueDictionary()


def history_path(data_dir, bot_id, chat_type, chat_id) -> Path:
    owner = MERGED_BOT_ID if chat_type == "group" else bot_id
    return Path(data_dir) / "history" / owner / chat_type / f"{chat_id}.jsonl"


def group_history_files(data_dir, group_id) -> list[Path]:
    return sorted((Path(data_dir) / "history").glob(f"*/group/{group_id}.jsonl"))


def event_identity(event: dict) -> bytes:
    mid = str(event.get("message_id") or "").strip()
    if mid:
        value = ["mid", mid]
    else:
        sender = event.get("sender") or {}
        value = ["content", event.get("time"),
                 str(event.get("user_id") or sender.get("user_id") or ""),
                 event.get("message")]
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).digest()


def archive_event(raw: dict, bot_id: str, bot_manager=None) -> dict:
    """Add provenance without mutating the protocol event used by handlers."""
    bots = {}
    if bot_manager is not None:
        for bot in bot_manager.all_bots:
            if bot.qq:
                bots[str(bot.qq)] = bot.bot_id
    if raw.get("self_id"):
        bots[str(raw["self_id"])] = bot_id
    return {**raw, "archive_bot_id": bot_id, "archive_bots": bots}


class GroupHistoryWriter(JSONLWriter):
    def __init__(self, path):
        super().__init__(path)
        self._seen: set[bytes] | None = None

    def _load_seen(self) -> set[bytes]:
        seen = set()
        if self._path.exists():
            with self._path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        seen.add(event_identity(event))
        return seen

    async def append(self, data: dict) -> None:
        # The lock covers both deduplication and the flushed append. Reloading
        # identities on first use also suppresses replays after a restart.
        async with self._lock:
            if self._seen is None:
                self._seen = await asyncio.to_thread(self._load_seen)
            identity = event_identity(data)
            if identity in self._seen:
                return
            await self._ensure_open()
            await self._file.write(json.dumps(data, ensure_ascii=False) + "\n")
            await self._file.flush()
            self._seen.add(identity)

    async def close(self) -> None:
        async with self._lock:
            await super().close()
            self._seen = None


def group_writer(path) -> GroupHistoryWriter:
    key = str(Path(path).resolve())
    writer = _writers.get(key)
    if writer is None:
        writer = GroupHistoryWriter(key)
        _writers[key] = writer
    return writer
