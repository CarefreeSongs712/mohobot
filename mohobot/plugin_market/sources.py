from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from loguru import logger

from mohobot.file_store import json_read, json_update

DEFAULT_SOURCE_ID = "official"
DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/CarefreeSongs712/mohobot-plugin-index/main/index.json"
)


@dataclass(frozen=True)
class RegistryIdentity:
    id: str
    url: str
    generation: str


@dataclass(frozen=True)
class RegistrySource:
    id: str
    name: str
    url: str
    official: bool = False
    generation: str = "legacy-v1"

    @property
    def identity(self) -> RegistryIdentity:
        return RegistryIdentity(self.id, self.url, self.generation)


def _source_file(data_dir: str | Path) -> Path:
    return Path(data_dir) / "plugin_registries.json"


def default_sources() -> list[RegistrySource]:
    return [
        RegistrySource(
            id=DEFAULT_SOURCE_ID,
            name="官方插件源",
            url=DEFAULT_INDEX_URL,
            official=True,
            generation="official-v1",
        )
    ]


def normalize_source_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("插件源只支持 http/https")
    if not parsed.hostname:
        raise ValueError("插件源缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("插件源 URL 不允许携带凭据")
    # urlsplit preserves the case of an IPv6 zone identifier.
    host = parsed.hostname
    authority = f"[{host}]" if ":" in host else host
    if parsed.port is not None and parsed.port != {"http": 80, "https": 443}[parsed.scheme]:
        authority += f":{parsed.port}"
    # Preserve even an explicitly empty query; fragments are not sent to the server.
    query = f"?{parsed.query}" if "?" in url.partition("#")[0] else ""
    return f"{parsed.scheme}://{authority}{parsed.path}{query}"


def validate_source_url(url: str) -> str:
    url = normalize_source_url(url)
    host = urlsplit(url).hostname
    assert host is not None
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("插件源不能指向本机")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if _is_blocked_address(address):
            raise ValueError("插件源不能指向内网地址")
        return url
    # 域名在请求时才会真正解析；这里预解析一次，降低 DNS 指向内网的风险。
    # 解析失败不拒绝，避免离线环境把合法源误判；真正请求失败会走缓存回退。
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        infos = []
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_blocked_address(resolved):
            raise ValueError("插件源解析到内网地址")
    return url


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # 198.18.0.0/15 是基准测试保留段；部分本地 DNS/代理会用它承载公网域名。
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network("198.18.0.0/15"):
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


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
    result = default_sources()
    seen = {DEFAULT_SOURCE_ID}
    for item in raw:
        try:
            source = _parse_custom_source(item)
        except (KeyError, ValueError, TypeError):
            continue
        if source.id not in seen:
            result.append(source)
            seen.add(source.id)
    return result


def _check_custom_id(source_id: str) -> None:
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", source_id):
        raise ValueError("插件源 id 只能包含小写字母、数字、下划线和连字符")
    if source_id == DEFAULT_SOURCE_ID:
        raise ValueError("不能覆盖官方源")


def _parse_custom_source(item: dict) -> RegistrySource:
    source_id = item["id"]
    _check_custom_id(source_id)
    generation = item.get("generation", "legacy-v1")
    if not isinstance(generation, str) or not re.fullmatch(r"(?:legacy-v1|[0-9a-f]{32})", generation):
        raise ValueError("插件源代次无效")
    return RegistrySource(source_id, str(item["name"]), normalize_source_url(str(item["url"])), generation=generation)


async def add_registry_source(
    data_dir: str | Path,
    *,
    id: str,
    name: str,
    url: str,
) -> RegistrySource:
    _check_custom_id(id)
    validated = await asyncio.to_thread(validate_source_url, url)
    return await save_registry_source(data_dir, source_id=id, name=name, validated_url=validated)


async def save_registry_source(
    data_dir: str | Path, *, source_id: str, name: str, validated_url: str,
) -> RegistrySource:
    """Store an already validated URL without performing DNS inside a commit lock."""
    _check_custom_id(source_id)
    source = RegistrySource(source_id, name, normalize_source_url(validated_url), generation=uuid4().hex)

    def merge(current):
        nonlocal source
        data = current if isinstance(current, dict) else {}
        sources = []
        prior_seen = False
        for item in data.get("sources", []):
            if not isinstance(item, dict):
                continue
            if item.get("id") != source_id:
                sources.append(item)
                continue
            if not prior_seen:
                prior_seen = True
                try:
                    old = _parse_custom_source(item)
                except (KeyError, ValueError, TypeError):
                    continue
                if old.url == source.url:
                    source = RegistrySource(source_id, name, source.url, generation=old.generation)
        sources.append({"id": source.id, "name": source.name, "url": source.url,
                        "official": False, "generation": source.generation})
        return {"schema_version": 1, "sources": sources}

    await json_update(_source_file(data_dir), merge, default={"schema_version": 1, "sources": []})
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
