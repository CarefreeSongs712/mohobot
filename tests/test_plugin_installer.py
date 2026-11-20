import zipfile
from pathlib import Path

import pytest

from mohobot.plugins.installer import (
    InstallError,
    install_from_archive,
    install_from_directory,
    safe_extract_zip,
    uninstall_plugin,
    upgrade_from_archive,
    sha256_file,
)


def _write_plugin(root: Path, name: str = "demo") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text(
        "class Plugin:\n"
        "    async def on_message(self, bot_id, event, raw): return (False, None)\n",
        encoding="utf-8",
    )
    (root / "metadata.yaml").write_text(
        f"name: {name}\n"
        "author: CarefreeSongs712\n"
        "display_name: 演示\n"
        "version: 1.0.0\n"
        "description: demo plugin\n"
        f"repo: https://github.com/CarefreeSongs712/mohobot-plugin-{name}\n"
        "license: MIT\n"
        "api_version: 1\n",
        encoding="utf-8",
    )


def _zip_plugin(tmp_path: Path, name: str = "demo", prefix: str = "repo-main/") -> Path:
    source = tmp_path / "source" / name
    _write_plugin(source, name)
    archive = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for path in source.rglob("*"):
            if path.is_file():
                handle.write(path, prefix + path.relative_to(source).as_posix())
    return archive


def test_install_archive_and_reject_duplicate(tmp_path: Path) -> None:
    archive = _zip_plugin(tmp_path)
    plugins = tmp_path / "data" / "plugins"
    manifest, destination = install_from_archive(archive, plugins)
    assert manifest.name == "demo"
    assert (destination / "main.py").is_file()
    assert (destination / "metadata.yaml").is_file()

    with pytest.raises(InstallError):
        install_from_archive(archive, plugins)


def test_zip_slip_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../evil.py", "print(1)")
    with pytest.raises(InstallError):
        safe_extract_zip(archive, tmp_path / "extract")


def test_sha256_mismatch_is_rejected(tmp_path: Path) -> None:
    archive = _zip_plugin(tmp_path)
    with pytest.raises(InstallError):
        install_from_archive(archive, tmp_path / "plugins", expected_sha256="0" * 64)


def test_install_from_directory(tmp_path: Path) -> None:
    source = tmp_path / "source" / "demo"
    _write_plugin(source)
    manifest, destination = install_from_directory(source, tmp_path / "plugins")
    assert manifest.name == "demo"
    assert destination.name == "demo"
    assert sha256_file(destination / "main.py")


def test_uninstall_moves_to_recycle_bin(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    source = tmp_path / "source" / "demo"
    _write_plugin(source)
    install_from_directory(source, plugins)
    config = plugins.parent / "plugins_config"
    config.mkdir()
    (config / "demo.json").write_text("{}", encoding="utf-8")

    target = uninstall_plugin(
        "demo",
        plugins,
        tmp_path / "recycle",
        config_dir=config,
        delete_config=True,
    )
    assert not (plugins / "demo").exists()
    assert target.is_dir()
    assert not (config / "demo.json").exists()


def test_upgrade_replaces_existing_plugin(tmp_path: Path) -> None:
    source = tmp_path / "source" / "demo"
    _write_plugin(source)
    install_from_directory(source, tmp_path / "plugins")

    updated = tmp_path / "updated" / "demo"
    _write_plugin(updated)
    (updated / "main.py").write_text(
        "class Plugin:\n    value = 2\n",
        encoding="utf-8",
    )
    archive = tmp_path / "demo-v2.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for path in updated.rglob("*"):
            if path.is_file():
                handle.write(path, "demo-main/" + path.relative_to(updated).as_posix())

    manifest, destination = upgrade_from_archive(archive, tmp_path / "plugins", "demo")
    assert manifest.name == "demo"
    assert "value = 2" in (destination / "main.py").read_text(encoding="utf-8")


def test_installed_archive_is_loaded_by_plugin_system(tmp_path: Path) -> None:
    import asyncio

    from mohobot.interceptors.plugin_system import PluginSystem

    archive = _zip_plugin(tmp_path, name="demo")
    install_from_archive(archive, tmp_path / "installed")

    async def run() -> None:
        system = PluginSystem(
            plugins_dir=str(tmp_path / "builtin"),
            data_dir=str(tmp_path / "data"),
            installed_dir=str(tmp_path / "installed"),
        )
        assert await system.load_plugins() == 1
        meta = system.list_plugins()[0]
        assert meta["source"] == "installed"
        assert meta["loaded"] is True
        await system.shutdown_plugins()

    asyncio.run(run())
