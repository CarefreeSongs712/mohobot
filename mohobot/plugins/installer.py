from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from mohobot.plugins.manifest import PluginManifest, find_manifest, parse_manifest

MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
INSTALL_PREFIX = ".plugin-install-"
BACKUP_PREFIX = ".plugin-backup-"
GITHUB_REPO_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
GH_PROXY = "https://v4.gh-proxy.org/"


class InstallError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_github_repo(url: str) -> tuple[str, str]:
    match = GITHUB_REPO_RE.fullmatch(url.strip())
    if not match:
        raise InstallError("只支持 https://github.com/{owner}/{repo}[.git]")
    return match.group(1), match.group(2)


def github_archive_url(url: str, ref: str = "main") -> str:
    owner, repo = parse_github_repo(url)
    return f"https://github.com/{owner}/{repo}/archive/refs/heads/{ref}.zip"


def with_proxy(url: str, proxy: str = GH_PROXY) -> str:
    if not proxy:
        return url
    return proxy.rstrip("/") + "/" + url


def download_file(
    url: str,
    destination: Path,
    *,
    timeout: float = 30.0,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "mohobot-plugin-installer"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise InstallError("下载文件超过大小限制")
            total = 0
            with destination.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise InstallError("下载文件超过大小限制")
                    handle.write(chunk)
    except InstallError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise InstallError(f"下载失败: {exc}") from exc
    return destination


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual.lower() != expected.strip().lower():
        raise InstallError(f"SHA256 不匹配: 期望 {expected}, 实际 {actual}")


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        total = 0
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0]:
                raise InstallError(f"压缩包包含非法路径: {info.filename}")
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise InstallError(f"压缩包包含符号链接: {info.filename}")
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise InstallError("解压后文件超过大小限制")
        archive.extractall(destination)


def find_plugin_root(directory: Path) -> Path:
    directory = Path(directory)
    if find_manifest(directory) is not None:
        return directory
    children = [item for item in directory.iterdir() if item.is_dir()]
    if len(children) == 1:
        nested = children[0]
        if find_manifest(nested) is not None:
            return nested
    raise InstallError("压缩包内找不到 metadata.yaml")


def _backup_path(plugins_dir: Path) -> Path:
    plugins_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=BACKUP_PREFIX, dir=plugins_dir))


def install_from_directory(
    source: Path,
    plugins_dir: Path,
    *,
    expected_name: str | None = None,
    expected_version: str | None = None,
) -> tuple[PluginManifest, Path]:
    source = Path(source).resolve()
    manifest_path = find_manifest(source)
    if manifest_path is None:
        raise InstallError("插件目录缺少 metadata.yaml")
    manifest = parse_manifest(manifest_path)
    if expected_name and manifest.name != expected_name:
        raise InstallError(f"插件名不匹配: 期望 {expected_name}, 实际 {manifest.name}")
    if expected_version and manifest.version != expected_version:
        raise InstallError(
            f"插件版本不匹配: 期望 {expected_version}, 实际 {manifest.version}"
        )

    plugins_dir = Path(plugins_dir).resolve()
    plugins_dir.mkdir(parents=True, exist_ok=True)
    destination = plugins_dir / manifest.name
    if destination.exists():
        raise InstallError(f"插件已安装: {manifest.name}")

    backup = None
    try:
        if destination.exists():
            backup = _backup_path(plugins_dir)
            shutil.copytree(destination, backup / "old")
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        return manifest, destination
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        if backup is not None and (backup / "old").exists():
            shutil.copytree(backup / "old", destination)
        raise InstallError(f"安装失败: {exc}") from exc
    finally:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


def install_from_archive(
    archive: Path,
    plugins_dir: Path,
    *,
    expected_name: str | None = None,
    expected_version: str | None = None,
    expected_sha256: str = "",
    proxy: str = "",
) -> tuple[PluginManifest, Path]:
    archive = Path(archive)
    if not archive.is_file():
        raise InstallError("压缩包不存在")
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise InstallError("压缩包超过大小限制")
    if expected_sha256:
        verify_sha256(archive, expected_sha256)

    plugins_dir = Path(plugins_dir).resolve()
    plugins_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=INSTALL_PREFIX, dir=plugins_dir))
    try:
        safe_extract_zip(archive, staging / "extract")
        root = find_plugin_root(staging / "extract")
        return install_from_directory(
            root,
            plugins_dir,
            expected_name=expected_name,
            expected_version=expected_version,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def uninstall_plugin(
    name: str,
    plugins_dir: Path,
    recycle_dir: Path,
    *,
    config_dir: Path | None = None,
    data_dir: Path | None = None,
    delete_config: bool = False,
    delete_data: bool = False,
) -> Path:
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise InstallError("非法插件名")
    plugins_dir = Path(plugins_dir)
    destination = plugins_dir / name
    if not destination.is_dir():
        raise InstallError(f"插件未安装: {name}")
    recycle_dir = Path(recycle_dir)
    recycle_dir.mkdir(parents=True, exist_ok=True)
    target = recycle_dir / f"{name}-{int(time.time())}"
    if target.exists():
        raise InstallError(f"回收站目标已存在: {target}")
    shutil.move(str(destination), str(target))
    if delete_config and config_dir is not None:
        (Path(config_dir) / f"{name}.json").unlink(missing_ok=True)
    if delete_data and data_dir is not None:
        for candidate in (
            Path(data_dir) / "plugins_data" / name,
            Path(data_dir) / "plugin_data" / name,
        ):
            shutil.rmtree(candidate, ignore_errors=True)
    return target


def upgrade_from_archive(
    archive: Path,
    plugins_dir: Path,
    expected_name: str,
    *,
    expected_sha256: str | None = None,
) -> tuple[PluginManifest, Path]:
    archive = Path(archive)
    if not archive.is_file():
        raise InstallError("压缩包不存在")
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise InstallError("压缩包超过大小限制")
    if expected_sha256:
        verify_sha256(archive, expected_sha256)
    plugins_dir = Path(plugins_dir)
    destination = plugins_dir / expected_name
    if not destination.is_dir():
        raise InstallError(f"插件未安装: {expected_name}")
    with tempfile.TemporaryDirectory(prefix=".plugin-upgrade-", dir=plugins_dir) as temp:
        temp_path = Path(temp)
        extracted = temp_path / "extract"
        safe_extract_zip(archive, extracted)
        root = find_plugin_root(extracted)
        manifest = parse_manifest(find_manifest(root) or (root / "metadata.yaml"))
        if manifest.name != expected_name:
            raise InstallError(
                f"更新包名称 {manifest.name} 与已安装插件 {expected_name} 不一致"
            )
        backup = temp_path / "backup"
        shutil.copytree(destination, backup)
        try:
            shutil.rmtree(destination)
            shutil.move(str(root), str(destination))
            installed_path = find_manifest(destination)
            if installed_path is None:
                raise InstallError("更新后清单缺失")
            installed = parse_manifest(installed_path)
            if installed.name != expected_name:
                raise InstallError("更新后清单名称不一致")
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            if backup.exists():
                shutil.move(str(backup), str(destination))
            raise
    return manifest, destination
