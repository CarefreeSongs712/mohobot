"""Small transaction records; atomic replacement, not power-loss durability."""

from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


def _unique_keys(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"重复的 JSON 键: {key}")
        result[key] = value
    return result


def _finite_number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("JSON 数字必须有限")
    return value


def read_object(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise
    if not stat.S_ISREG(mode):
        raise ValueError(f"不是普通 JSON 文件: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys,
            parse_float=_finite_number, parse_constant=_finite_number,
        )
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"无法解析 JSON 文件 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 文件必须为对象: {path}")
    return value


def write_object(path: Path, value: dict[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def finish_task(work):
    """Keep ownership of a task until it settles, even on repeated cancellation."""
    worker = asyncio.create_task(work)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


async def thread_until_done(func, /, *args):
    return await finish_task(asyncio.to_thread(func, *args))
