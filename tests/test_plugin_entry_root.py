"""Installed entry paths, import roots and configuration across plugin overlays."""

import asyncio
import json
from zipfile import ZipFile

import httpx
import pytest

from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.plugins.installer import install_from_archive
from mohobot.web_panel.app import WebPanel


ROOT_SCHEMA = {"choices": {"type": "list", "default": []}}


def _manifest(name, entry):
    return (
        f"name: {name}\nauthor: TestFixture\ndisplay_name: Entry fixture\n"
        "version: 1.0.0\ndescription: Offline contract test\n"
        "repo: https://github.com/TestFixture/offline-plugin\nlicense: MIT\n"
        f"api_version: 1\nentry: {entry}\n"
    )


def _install(tmp_path, system, name, entry, schema, *, manifest_name="metadata.yaml", code=None):
    archive = tmp_path / f"{name}.zip"
    nested = "/" in entry
    root = "fixture-release"
    with ZipFile(archive, "w") as zf:
        zf.writestr(f"{root}/{manifest_name}", _manifest(name, entry))
        zf.writestr(f"{root}/{entry}", code or (
            "from .helper import ORIGIN\n"
            "class Plugin:\n"
            "    origin = ORIGIN\n"
        ))
        zf.writestr(f"{root}/helper.py", "ORIGIN = 'installed'\n")
        zf.writestr(f"{root}/_conf_schema.json", json.dumps(schema))
        if nested:
            child, filename = entry.rsplit("/", 1)
            zf.writestr(f"{root}/{child}/metadata.yaml", _manifest("not_the_package", filename))
            zf.writestr(f"{root}/{child}/_conf_schema.json", '{"wrong": {"type": "string"}}')
    return install_from_archive(archive, system.installed_dir, expected_name=name)


def _meta(system, name):
    return next(meta for meta in system._plugins if meta["name"] == name)


async def _check_entry_root(tmp_path, entry, manifest_name):
    name = "entry_root_fixture"
    system = PluginSystem(plugins_dir=str(tmp_path / "builtin"), data_dir=str(tmp_path / "runtime"))
    manifest, installed = _install(tmp_path, system, name, entry, ROOT_SCHEMA, manifest_name=manifest_name)
    assert (installed / entry).is_file()
    assert manifest.entry == entry
    try:
        await system.load_plugins()
        meta = _meta(system, name)
        assert meta["source"] == "installed"
        assert meta["file"] == f"{name}/{entry}"
        assert meta["error"] is None, meta["error"]
        assert meta["instance"].origin == "installed"
        assert system.get_config_schema(name) == ROOT_SCHEMA
        assert meta["instance"].plugin_config == {"choices": []}
        first = meta["instance"]
        assert await system.reload_plugin(name)
        reloaded = _meta(system, name)
        assert reloaded["instance"] is not first
        assert reloaded["instance"].origin == "installed"
        assert reloaded["instance"].plugin_config == {"choices": []}
    finally:
        await system.shutdown_plugins()


@pytest.mark.asyncio
async def test_direct_builtin_entry_uses_sibling_schema_and_ignores_manifest(tmp_path):
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    name = "direct_root_fixture"
    (builtin / f"{name}.py").write_text("class Plugin: pass\n", encoding="utf-8")
    (builtin / "metadata.yaml").write_text(_manifest("other_package", f"{name}.py"), encoding="utf-8")
    (builtin / "_conf_schema.json").write_text(json.dumps(ROOT_SCHEMA), encoding="utf-8")
    system = PluginSystem(plugins_dir=str(builtin), data_dir=str(tmp_path / "runtime"))
    try:
        assert await system.load_plugins() == 1
        meta = _meta(system, name)
        assert meta["source"] == "builtin"
        assert meta["file"] == f"{name}.py"
        assert meta["manifest"] is None
        assert meta["error"] is None
        first = meta["instance"]
        assert first.plugin_config == {"choices": []}
        assert await system.reload_plugin(name)
        assert _meta(system, name)["instance"] is not first
    finally:
        await system.shutdown_plugins()


@pytest.mark.asyncio
async def test_nested_manifest_name_must_match_package_directory_before_execution(tmp_path):
    name = "wrong_dir_root_fixture"
    system = PluginSystem(plugins_dir=str(tmp_path / "builtin"), data_dir=str(tmp_path / "runtime"))
    marker = tmp_path / "entry-executed"
    code = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        "class Plugin: pass\n"
    )
    _, installed = _install(tmp_path, system, name, "src/worker.py", ROOT_SCHEMA, code=code)
    installed.rename(installed.parent / "wrong_package_name")
    try:
        assert await system.load_plugins() == 0
        meta = _meta(system, name)
        assert meta["source"] == "installed"
        assert not meta["loaded"]
        assert meta["error"] == f"metadata name={name} 与安装目录 wrong_package_name 不一致"
        assert not marker.exists()
    finally:
        await system.shutdown_plugins()


@pytest.mark.asyncio
async def test_nested_overlay_panel_uses_active_schema_without_migrating_archive(tmp_path):
    name = "nested_overlay_fixture"
    builtin = tmp_path / "builtin"
    package = builtin / name
    package.mkdir(parents=True)
    builtin_schema = {"choices": {"type": "string", "default": ""}}
    overlay_schema = ROOT_SCHEMA | {"extra": {"type": "string", "default": "new-default"}}
    (package / "main.py").write_text("class Plugin:\n    origin = 'builtin'\n", encoding="utf-8")
    (package / "_conf_schema.json").write_text(json.dumps(builtin_schema), encoding="utf-8")
    runtime = tmp_path / "runtime"
    saved = runtime / "plugins_config" / f"{name}.json"
    saved.parent.mkdir(parents=True)
    old_config = {"choices": "red,blue", "plugin_private": {"keep": [1]}}
    initial = json.dumps(old_config, indent=2).encode()
    saved.write_bytes(initial)
    system = PluginSystem(plugins_dir=str(builtin), data_dir=str(runtime))
    panel = WebPanel(
        data_dir=str(runtime), config_path=str(tmp_path / "global.yaml"),
        password_hash=WebPanel._hash_password("test-password"), plugin_system=system,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=panel._app), base_url="http://offline.test",
        ) as client:
            login = await client.post("/api/login", json={"username": "admin", "password": "test-password"})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            endpoint = f"/api/plugins/{name}/config"

            async def observe(origin, schema, config):
                meta = _meta(system, name)
                assert meta["source"] == origin
                assert meta["instance"].origin == origin
                assert meta["instance"].plugin_config == config
                result = await client.get(endpoint, headers=headers)
                assert result.status_code == 200, result.text
                assert result.json() == {"schema": schema, "config": config}

            assert await system.load_plugins() == 1
            await observe("builtin", builtin_schema, {"choices": "red,blue"})
            assert saved.read_bytes() == initial
            _, installed = _install(tmp_path, system, name, "src/worker.py", overlay_schema)
            assert await system.reload_plugin(name)
            await observe("installed", overlay_schema, {"choices": ["red", "blue"], "extra": "new-default"})
            assert saved.read_bytes() == initial

            updated = await client.post(endpoint, headers=headers, json={"data": {"extra": "edited"}})
            assert updated.status_code == 200, updated.text
            assert updated.json() == {
                "status": "ok", "config": {"choices": ["red", "blue"], "extra": "edited"},
                "application": "applied",
            }
            assert _meta(system, name)["instance"].plugin_config == updated.json()["config"]
            patched = saved.read_bytes()
            assert json.loads(patched) == {**old_config, "extra": "edited"}

            removed = await client.delete(f"/api/plugins/{name}", headers=headers)
            assert removed.status_code == 200, removed.text
            assert not installed.exists()
            await observe("builtin", builtin_schema, {"choices": "red,blue"})
            assert saved.read_bytes() == patched
    finally:
        await system.shutdown_plugins()
        await panel.stop()


def test_installed_entry_uses_package_root_for_manifest_and_schema(tmp_path):
    """三种入口形态各自独立目录执行。

    不用 parametrize: 项目自研 legacy runner 只支持无参或仅 ``tmp_path``
    的用例入口, 而 CI 在 push main 时仍会执行该 runner (#171 审查 P1)。
    """
    for index, (entry, manifest_name) in enumerate((
        ("main.py", "metadata.yaml"),
        ("src/worker.py", "metadata.yaml"),
        ("src/nested/worker.py", "metadata.yml"),
    )):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        asyncio.run(_check_entry_root(case_dir, entry, manifest_name))
