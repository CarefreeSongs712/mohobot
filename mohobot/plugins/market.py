from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mohobot.plugins.installer import InstallError, parse_github_repo, with_proxy
from mohobot.plugins.manifest import NAME_RE, SEMVER_RE

INDEX_SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


@dataclass(frozen=True)
class MarketEntry:
    author: str
    name: str
    version: str
    repo: str
    description: str
    display_name: str = ""
    download_url: str = ""
    sha256: str = ""
    api_version: int = 1
    mohobot_version: str = ""
    license: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    updated_at: str = ""

    @property
    def plugin_id(self) -> str:
        return f"{self.author}/{self.name}"


@dataclass(frozen=True)
class MarketIndex:
    plugins: dict[str, MarketEntry]
    name: str = ""
    version: str = ""
    updated_at: str = ""


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InstallError(f"市场条目缺少字段: {key}")
    return value.strip()


def parse_market_entry(key: str, data: Any) -> MarketEntry:
    if not isinstance(data, dict):
        raise InstallError(f"市场条目 {key} 必须是对象")
    author = _required_text(data, "author")
    name = _required_text(data, "name")
    if not NAME_RE.fullmatch(name):
        raise InstallError(f"市场条目 {key} 的 name 非法")
    if key != f"{author}/{name}":
        raise InstallError(f"市场条目键 {key} 与 author/name 不一致")
    version = _required_text(data, "version")
    if not SEMVER_RE.fullmatch(version):
        raise InstallError(f"市场条目 {key} 的 version 非法")
    repo = _required_text(data, "repo")
    parse_github_repo(repo)
    description = _required_text(data, "description")
    sha256 = _required_text(data, "sha256")
    if not SHA256_RE.fullmatch(sha256):
        raise InstallError(f"市场条目 {key} 的 sha256 非法")
    api_version = data.get("api_version")
    if not isinstance(api_version, int) or isinstance(api_version, bool) or api_version < 1:
        raise InstallError(f"市场条目 {key} 的 api_version 非法")
    tags_raw = data.get("tags") or []
    if not isinstance(tags_raw, list) or any(not isinstance(item, str) for item in tags_raw):
        raise InstallError(f"市场条目 {key} 的 tags 非法")
    return MarketEntry(
        author=author,
        name=name,
        version=version,
        repo=repo,
        description=description,
        display_name=str(data.get("display_name") or ""),
        download_url=str(data.get("download_url") or ""),
        sha256=sha256.lower(),
        api_version=api_version,
        mohobot_version=str(data.get("mohobot_version") or ""),
        license=str(data.get("license") or ""),
        tags=tuple(item.strip() for item in tags_raw if item.strip()),
        updated_at=str(data.get("updated_at") or ""),
    )


def parse_market_index(payload: Any) -> MarketIndex:
    if not isinstance(payload, dict):
        raise InstallError("市场索引根节点必须是对象")
    meta = payload.get("$meta")
    if not isinstance(meta, dict):
        raise InstallError("市场索引缺少 $meta")
    if meta.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise InstallError("不支持的市场索引 schema_version")
    plugins_raw = payload.get("plugins")
    if not isinstance(plugins_raw, dict):
        raise InstallError("市场索引缺少 plugins 对象")
    plugins: dict[str, MarketEntry] = {}
    for key, value in plugins_raw.items():
        entry = parse_market_entry(str(key), value)
        plugins[entry.plugin_id] = entry
    return MarketIndex(
        plugins=plugins,
        name=str(meta.get("name") or ""),
        version=str(meta.get("version") or ""),
        updated_at=str(meta.get("updated_at") or ""),
    )


def load_market_index(path: Path) -> MarketIndex:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"读取市场索引失败: {exc}") from exc
    return parse_market_index(payload)


def entry_download_url(entry: MarketEntry, proxy: str = "") -> str:
    if entry.download_url:
        return with_proxy(entry.download_url, proxy) if proxy else entry.download_url
    return with_proxy(entry.repo.rstrip("/") + "/archive/refs/heads/main.zip", proxy) if proxy else entry.repo.rstrip("/") + "/archive/refs/heads/main.zip"
