from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.file_store import _get_lock
from mohobot.plugins._atomic_json import read_object, thread_until_done, write_object

SOURCE_SCHEMA_VERSION = 1
_UNCHECKED = object()


class SourceRecordConflict(ValueError):
    """The installed source no longer matches the transaction snapshot."""


def sources_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "plugin_sources.json"


def _read_records(path: Path) -> dict[str, dict[str, Any]]:
    data = read_object(path, missing_ok=True)
    if any(not isinstance(value, dict) for value in data.values()):
        raise ValueError(f"插件来源记录必须为对象: {path}")
    return data


async def load_sources_strict(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    path = sources_path(data_dir).absolute()
    async with _get_lock(str(path)):
        return await thread_until_done(_read_records, path)


async def load_sources(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    try:
        return await load_sources_strict(data_dir)
    except (OSError, ValueError) as exc:
        logger.error(f"读取插件来源记录失败: {exc}")
        return {}


async def get_source(data_dir: str | Path, name: str) -> dict[str, Any] | None:
    data = await load_sources(data_dir)
    value = data.get(name)
    return value if isinstance(value, dict) else None


def _replace_record(path: Path, name: str, expected: object, record: dict[str, Any] | None) -> None:
    data = _read_records(path)
    current = data.get(name)
    if expected is not _UNCHECKED and current != expected:
        raise SourceRecordConflict(f"插件 {name} 的来源记录已变更: {path}")
    if current == record:
        return
    if record is None:
        data.pop(name, None)
    else:
        data[name] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    write_object(path, data)


async def _change_source_record(
    data_dir: str | Path, name: str, expected: object, record: dict[str, Any] | None,
) -> None:
    path = sources_path(data_dir).absolute()
    async with _get_lock(str(path)):
        await thread_until_done(_replace_record, path, name, expected, record)


async def replace_source_record(
    data_dir: str | Path,
    name: str,
    *,
    expected: dict[str, Any] | None,
    record: dict[str, Any] | None,
) -> None:
    """Compare and atomically replace just one plugin's exact journal record."""
    await _change_source_record(data_dir, name, expected, record)


async def save_source(data_dir: str | Path, name: str, record: dict[str, Any]) -> None:
    await _change_source_record(data_dir, name, _UNCHECKED, {**record, "schema_version": SOURCE_SCHEMA_VERSION})


async def remove_source(data_dir: str | Path, name: str) -> None:
    await _change_source_record(data_dir, name, _UNCHECKED, None)
