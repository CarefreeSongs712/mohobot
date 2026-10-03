"""数据目录整体搬家（如从 AstrBot 版复制过来）后，库条目与文件都要保住。"""

import hashlib
import shutil
from pathlib import Path

import pytest
from PIL import Image

from meme_stealer_core.app import StealerApp
from meme_stealer_core.host import ConfigStore

PLUGIN_DIR = Path(__file__).resolve().parents[1]
KEPT = ("desc", "overlay_text", "is_favorite", "use_count", "scope_mode", "origin_target", "tags")


async def _seed_old_library(old_dir: Path, tmp_path: Path) -> tuple[str, str]:
    app = StealerApp(ConfigStore(tmp_path / "old.json", {}), old_dir, plugin_dir=PLUGIN_DIR)
    image = app.plugin_config.ensure_category_dir("happy") / "a.png"
    Image.new("RGB", (20, 20), "blue").save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    await app.db_service.insert_batch(
        [
            {
                "path": str(image), "hash": digest, "category": "happy", "desc": "旧描述",
                "overlay_text": "啊？", "tags": ["猫"], "is_favorite": 1, "use_count": 9,
                "scope_mode": "local", "origin_target": "group:1",
            }
        ]
    )
    pending = app.plugin_config.ensure_pending_dir() / "p.png"
    Image.new("RGB", (20, 20), "green").save(pending)
    await app.db_service.insert_pending(
        {"path": str(pending), "hash": "pending-hash", "category": "uncategorized", "desc": "待审"}
    )
    return str(image), digest


async def _start(new_dir: Path, tmp_path: Path) -> StealerApp:
    app = StealerApp(ConfigStore(tmp_path / "new.json", {}), new_dir, plugin_dir=PLUGIN_DIR)
    await app.initialize()
    return app


def _assert_migrated(app: StealerApp, new_dir: Path):
    index = app.db_service.get_index_cache_readonly()
    new_image = new_dir.resolve() / "categories" / "happy" / "a.png"
    assert list(index) == [str(new_image)]
    assert new_image.is_file()
    meta = index[str(new_image)]
    assert {key: meta[key] for key in KEPT} == {
        "desc": "旧描述", "overlay_text": "啊？", "is_favorite": 1, "use_count": 9,
        "scope_mode": "local", "origin_target": "group:1", "tags": ["猫"],
    }
    rows, _, _ = app.db_service.get_pending_paginated(page=1, page_size=10)
    assert [row["path"] for row in rows] == [str(new_dir.resolve() / "pending" / "p.png")]


@pytest.mark.asyncio
async def test_copied_data_dir_keeps_metadata_while_old_dir_still_exists(tmp_path):
    old_dir = tmp_path / "astrbot" / "data" / "plugin_data" / "astrbot_plugin_stealer"
    await _seed_old_library(old_dir, tmp_path)
    new_dir = tmp_path / "mohobot" / "data" / "plugins_data" / "meme_stealer"
    shutil.copytree(old_dir, new_dir)

    app = await _start(new_dir, tmp_path)
    try:
        _assert_migrated(app, new_dir)
    finally:
        await app.terminate()


@pytest.mark.asyncio
async def test_moved_data_dir_keeps_metadata_when_old_dir_is_gone(tmp_path):
    old_dir = tmp_path / "astrbot" / "data" / "plugin_data" / "astrbot_plugin_stealer"
    await _seed_old_library(old_dir, tmp_path)
    new_dir = tmp_path / "mohobot" / "data" / "plugins_data" / "meme_stealer"
    shutil.move(str(old_dir), str(new_dir))

    app = await _start(new_dir, tmp_path)
    try:
        _assert_migrated(app, new_dir)
    finally:
        await app.terminate()


def test_windows_paths_from_another_machine_are_mapped(tmp_path):
    app = StealerApp(ConfigStore(tmp_path / "c.json", {}), tmp_path / "data", plugin_dir=PLUGIN_DIR)
    target = app.plugin_config.ensure_category_dir("happy") / "a.png"
    target.write_bytes(b"x")
    raw = r"C:\AstrBot\data\plugin_data\astrbot_plugin_stealer\categories\happy\a.png"
    assert app._relocated_path(raw, "categories", depth=2) == app.base_dir.resolve() / "categories" / "happy" / "a.png"
    # 已在当前目录下的路径、对不上布局的路径都不改
    assert app._relocated_path(str(target), "categories", depth=2) is None
    assert app._relocated_path("/elsewhere/raw/a.png", "categories", depth=2) is None
    assert app._relocated_path("/elsewhere/categories/sad/a.png", "categories", depth=2) is None
