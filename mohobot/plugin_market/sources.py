from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from mohobot.file_store import json_read, json_update

DEFAULT_SOURCE_ID = "official"
DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/CarefreeSongs712/mohobot-plugin-index/main/index.json"
)


@dataclass(frozen=True)
class RegistrySource:
    id: str
    name: str
    url: str
    official: bool = False


def _source_file(data_dir: str | Path) -> Path:
    return Path(data_dir) / "plugin_registries.json"


def default_sources() -> list[RegistrySource]:
    return [
        RegistrySource(
            id=DEFAULT_SOURCE_ID,
            name="官方插件源",
            url=DEFAULT_INDEX_URL,
            official=True,
        )
    ]


def validate_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("插件源只支持 http/https")
    if not parsed.hostname:
        raise ValueError("插件源缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("插件源 URL 不允许携带凭据")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("插件源不能指向本机")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
    ):
        raise ValueError("插件源不能指向内网地址")
    return url


async def load_registry_sources(data_dir: str | Path) -> list[RegistrySource]:
    try:
        data = await json_read(_source_file(data_dir))
    except (OSError, ValueError) as e:
        logger.error(f"读取插件源配置失败: {e}")
        return default_sources()
    if not isinstance(data, dict):
        return default_sources()
    raw = data.get("sources")
    if not isinstance(raw, list):
        return default_sources()
    result: list[RegistrySource] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            source_id = str(item["id"])
            if not re.fullmatch(r"[a-z0-9_-]{1,64}", source_id):
                continue
            source = RegistrySource(
                id=source_id,
                name=str(item["name"]),
                url=validate_source_url(str(item["url"])),
                official=bool(item.get("official", False)),
            )
        except (KeyError, ValueError):
            continue
        if source.official and source.id != DEFAULT_SOURCE_ID:
            source = RegistrySource(source.id, source.name, source.url, False)
        result.append(source)
    if not any(source.official for source in result):
        result.insert(0, default_sources()[0])
    return result


async def add_registry_source(
    data_dir: str | Path,
    *,
    id: str,
    name: str,
    url: str,
) -> RegistrySource:
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", id):
        raise ValueError("插件源 id 只能包含小写字母、数字、下划线和连字符")
    if id == DEFAULT_SOURCE_ID:
        raise ValueError("不能覆盖官方源")
    source = RegistrySource(id=id, name=name, url=validate_source_url(url), official=False)
    path = _source_file(data_dir)

    def merge(current):
        data = current if isinstance(current, dict) else {}
        sources = [
            item
            for item in data.get("sources", [])
            if isinstance(item, dict) and item.get("id") != id
        ]
        sources.append(
            {"id": source.id, "name": source.name, "url": source.url, "official": False}
        )
        return {"schema_version": 1, "sources": sources}

    await json_update(path, merge, default={"schema_version": 1, "sources": []})
    return source


async def remove_registry_source(data_dir: str | Path, source_id: str) -> None:
    if source_id == DEFAULT_SOURCE_ID:
        raise ValueError("官方源不能删除")
    path = _source_file(data_dir)

    def merge(current):
        data = current if isinstance(current, dict) else {}
        sources = [
            item
            for item in data.get("sources", [])
            if isinstance(item, dict) and item.get("id") != source_id
        ]
        return {"schema_version": 1, "sources": sources}

    await json_update(path, merge, default={"schema_version": 1, "sources": []})
