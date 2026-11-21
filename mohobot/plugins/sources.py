from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.file_store import json_read, json_update

SOURCE_SCHEMA_VERSION = 1


def sources_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "plugin_sources.json"


async def load_sources(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    try:
        data = await json_read(sources_path(data_dir))
    except (OSError, ValueError) as e:
        logger.error(f"读取插件来源记录失败: {e}")
        return {}
    return data if isinstance(data, dict) else {}


async def get_source(data_dir: str | Path, name: str) -> dict[str, Any] | None:
    data = await load_sources(data_dir)
    value = data.get(name)
    return value if isinstance(value, dict) else None


async def save_source(
    data_dir: str | Path,
    name: str,
    record: dict[str, Any],
) -> None:
    record = {**record, "schema_version": SOURCE_SCHEMA_VERSION}
    path = sources_path(data_dir)

    def merge(current: Any) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        return {**data, name: record}

    await json_update(path, merge, default={name: record})


async def remove_source(data_dir: str | Path, name: str) -> None:
    path = sources_path(data_dir)

    def merge(current: Any) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        data.pop(name, None)
        return data

    await json_update(path, merge, default={})
