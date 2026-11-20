from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

MANIFEST_MAX_BYTES = 1024 * 1024
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
AUTHOR_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class PluginManifest:
    name: str
    author: str
    display_name: str
    version: str
    description: str
    repo: str
    license: str
    api_version: int
    mohobot_version: str = ""
    entry: str = "main.py"
    tags: tuple[str, ...] = field(default_factory=tuple)
    logo: str = ""

    @property
    def plugin_id(self) -> str:
        return f"{self.author}/{self.name}"


def find_manifest(plugin_dir: Path) -> Path | None:
    for filename in ("metadata.yaml", "metadata.yml"):
        path = plugin_dir / filename
        if path.is_file():
            return path
    return None


def _text(data: dict[str, Any], key: str, *, required: bool = True) -> str:
    value = data.get(key)
    if value is None:
        if required:
            raise ManifestError(f"missing field: {key}")
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"field {key} must be a non-empty string")
    return value.strip()


def _relative_path(value: str, field_name: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"{field_name} must stay inside the plugin directory")
    return path.as_posix()


def parse_manifest(path: Path) -> PluginManifest:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    if size > MANIFEST_MAX_BYTES:
        raise ManifestError("manifest exceeds 1 MiB")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ManifestError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("manifest root must be a mapping")

    if "desc" in raw and "description" not in raw:
        raw["description"] = raw["desc"]

    name = _text(raw, "name")
    if not NAME_RE.fullmatch(name):
        raise ManifestError("name must match ^[a-z][a-z0-9_]{1,63}$")

    author = _text(raw, "author")
    if not AUTHOR_RE.fullmatch(author):
        raise ManifestError("author must be a valid GitHub login")

    version = _text(raw, "version")
    if not SEMVER_RE.fullmatch(version):
        raise ManifestError("version must be semantic version")
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ManifestError("version must be semantic version") from exc

    repo = _text(raw, "repo").rstrip("/")
    if not REPO_RE.fullmatch(repo):
        raise ManifestError("repo must be https://github.com/{owner}/{repo}")

    api_version = raw.get("api_version")
    if not isinstance(api_version, int) or isinstance(api_version, bool) or api_version < 1:
        raise ManifestError("api_version must be a positive integer")

    mohobot_version = _text(raw, "mohobot_version", required=False)
    if mohobot_version:
        try:
            SpecifierSet(mohobot_version)
        except InvalidSpecifier as exc:
            raise ManifestError("mohobot_version must be a PEP 440 specifier") from exc

    entry = _relative_path(_text(raw, "entry", required=False) or "main.py", "entry")
    if not entry.endswith(".py"):
        raise ManifestError("entry must be a Python file")
    if not (path.parent / entry).is_file():
        raise ManifestError(f"entry file not found: {entry}")

    logo = _text(raw, "logo", required=False)
    if logo:
        logo = _relative_path(logo, "logo")

    tags_raw = raw.get("tags") or []
    if not isinstance(tags_raw, list) or any(not isinstance(item, str) for item in tags_raw):
        raise ManifestError("tags must be a list of strings")

    return PluginManifest(
        name=name,
        author=author,
        display_name=_text(raw, "display_name"),
        version=version,
        description=_text(raw, "description"),
        repo=repo,
        license=_text(raw, "license"),
        api_version=api_version,
        mohobot_version=mohobot_version,
        entry=entry,
        tags=tuple(item.strip() for item in tags_raw if item.strip()),
        logo=logo,
    )


def check_compatibility(
    manifest: PluginManifest,
    *,
    api_version: int,
    mohobot_version: str,
) -> None:
    if manifest.api_version != api_version:
        raise ManifestError(
            f"插件要求 API {manifest.api_version}, 当前支持 {api_version}"
        )
    if manifest.mohobot_version:
        try:
            spec = SpecifierSet(manifest.mohobot_version)
        except InvalidSpecifier as exc:
            raise ManifestError("mohobot_version 约束无效") from exc
        try:
            current = Version(mohobot_version)
        except InvalidVersion as exc:
            raise ManifestError("当前 mohobot 版本无效") from exc
        if current not in spec:
            raise ManifestError(
                f"插件要求 mohobot {manifest.mohobot_version}, 当前 {mohobot_version}"
            )
