"""Async file I/O with per-file asyncio.Lock for thread-safe JSONL/JSON access.

All file operations are async (aiofiles) with per-path locks to prevent
concurrent write corruption while allowing parallel access to different files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import aiofiles
import aiofiles.os

# ── Lock Registry ─────────────────────────────────────────────

_file_locks: dict[str, "asyncio.Lock"] = {}
"""Registry of per-file locks. Keyed by absolute file path."""


def _get_lock(file_path: str) -> "asyncio.Lock":
    """Get or create an asyncio.Lock for the given file path."""
    import asyncio

    if file_path not in _file_locks:
        _file_locks[file_path] = asyncio.Lock()
    return _file_locks[file_path]


# ─── Path helpers ──────────────────────────────────────────────


def data_dir(base: str = "./data") -> Path:
    return Path(base)


def bot_dir(base: str, bot_id: int | str) -> Path:
    return data_dir(base) / "bots" / str(bot_id)


def history_dir(base: str, bot_id: int | str) -> Path:
    return data_dir(base) / "history" / str(bot_id)


def contexts_dir(base: str, bot_id: int | str) -> Path:
    return data_dir(base) / "contexts" / str(bot_id)


def cache_dir(base: str) -> Path:
    return data_dir(base) / "cache"


def images_dir(base: str) -> Path:
    return cache_dir(base) / "images"


# ── JSONL Writer (append-only, lock-protected) ────────────────


class JSONLWriter:
    """Append-only JSONL writer with per-file async lock.

    Usage:
        writer = JSONLWriter("./data/history/123456/private/789.jsonl")
        await writer.append({"time": ..., "message": ...})
        await writer.close()
    """

    def __init__(self, file_path: str | Path):
        self._path = Path(file_path)
        self._lock = _get_lock(str(self._path.absolute()))
        self._file = None  # Lazy-open for append

    async def _ensure_open(self):
        if self._file is None or self._file.closed:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = await aiofiles.open(self._path, mode="a", encoding="utf-8")

    async def append(self, data: dict[str, Any]) -> None:
        """Append one JSON line to the file (thread-safe)."""
        async with self._lock:
            await self._ensure_open()
            line = json.dumps(data, ensure_ascii=False) + "\n"
            await self._file.write(line)
            await self._file.flush()

    async def append_batch(self, batch: list[dict[str, Any]]) -> None:
        """Append multiple lines atomically."""
        async with self._lock:
            await self._ensure_open()
            lines = "\n".join(json.dumps(d, ensure_ascii=False) for d in batch) + "\n"
            await self._file.write(lines)
            await self._file.flush()

    async def close(self) -> None:
        if self._file and not self._file.closed:
            await self._file.close()

    @property
    def path(self) -> Path:
        return self._path


# ── JSON Reader / Writer (full-file, lock-protected) ──────────


async def json_read(file_path: str | Path) -> Any:
    """Read and parse a JSON file with lock protection."""
    path = Path(file_path)
    lock = _get_lock(str(path.absolute()))
    async with lock:
        if not await aiofiles.os.path.exists(path):
            return None
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            content = await f.read()
        if not content.strip():
            return None
        return json.loads(content)


async def json_write(file_path: str | Path, data: Any, pretty: bool = True) -> None:
    """Write data as JSON to a file with lock protection."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _get_lock(str(path.absolute()))
    async with lock:
        kwargs = {"ensure_ascii": False}
        if pretty:
            kwargs["indent"] = 2
        content = json.dumps(data, **kwargs)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(content)


# ── JSONL Reader (read-only, lock-protected) ──────────────────


async def jsonl_read_all(file_path: str | Path) -> list[dict[str, Any]]:
    """Read all lines from a JSONL file with lock protection."""
    path = Path(file_path)
    lock = _get_lock(str(path.absolute()))
    async with lock:
        if not await aiofiles.os.path.exists(path):
            return []
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            content = await f.read()
    lines = [line.strip() for line in content.split("\n") if line.strip()]
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


async def jsonl_read_tail(file_path: str | Path, n: int = 10) -> list[dict[str, Any]]:
    """Read the last N lines from a JSONL file efficiently."""
    path = Path(file_path)
    lock = _get_lock(str(path.absolute()))
    async with lock:
        if not await aiofiles.os.path.exists(path):
            return []

        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            # Read from end using seek for efficiency
            content = await f.read()

    lines = [line.strip() for line in content.split("\n") if line.strip()]
    tail = lines[-n:]
    result: list[dict[str, Any]] = []
    for line in tail:
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


# ── File Listing & Utilities ──────────────────────────────────


async def list_json_files(directory: str | Path) -> list[Path]:
    """List all .json files in a directory (non-recursive)."""
    path = Path(directory)
    if not await aiofiles.os.path.exists(path):
        return []
    entries = await aiofiles.os.listdir(path)
    return [path / e for e in entries if e.endswith(".json")]


async def list_jsonl_files(directory: str | Path) -> list[Path]:
    """List all .jsonl files in a directory (non-recursive)."""
    path = Path(directory)
    if not await aiofiles.os.path.exists(path):
        return []
    entries = await aiofiles.os.listdir(path)
    return [path / e for e in entries if e.endswith(".jsonl")]


async def file_size(file_path: str | Path) -> int:
    """Get file size in bytes."""
    path = Path(file_path)
    if not await aiofiles.os.path.exists(path):
        return 0
    stat = await aiofiles.os.stat(path)
    return stat.st_size


async def ensure_dir(path: str | Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p