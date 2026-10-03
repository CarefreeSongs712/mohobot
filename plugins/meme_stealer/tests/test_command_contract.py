"""/meme 指令路由与委托契约。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meme_stealer_core.app import StealerApp
from meme_stealer_core.host import ConfigStore

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_COMMANDS = {
    "on", "off", "auto_on", "auto_off", "group", "偷",
    "status",
    "tag_stats", "clean", "capacity", "list", "delete", "blacklist",
    "scope", "rebuild_index",
}
NON_ADMIN_COMMANDS = {"status", "list"}


def _event():
    return SimpleNamespace(plain_result=lambda text: text)


def _app(tmp_path, backing: dict | None = None) -> tuple[StealerApp, ConfigStore]:
    store = ConfigStore(tmp_path / "plugins_config" / "meme_stealer.json", backing or {})
    return StealerApp(store, tmp_path / "data", plugin_dir=ROOT), store


async def _run(app, args, *, is_admin=True):
    return [message async for message in app.run_command(_event(), args, is_admin=is_admin)]


@pytest.mark.asyncio
async def test_real_startup_exposes_api_and_list_command_works_with_empty_database(tmp_path):
    app, _ = _app(tmp_path)
    routes = {path: handler for path, handler, _ in app.plugin_api.iter_routes()}

    messages = await _run(app, ["list"], is_admin=False)

    assert routes["/analyze"] == app.plugin_api.handle_analyze_image
    assert messages and "暂无" in messages[0]
    assert app.db_service.count_total() == 0


def test_command_table_routes_every_subcommand_to_an_existing_handler(tmp_path):
    app, _ = _app(tmp_path)
    assert set(StealerApp.COMMANDS) == EXPECTED_COMMANDS
    for name, (path, admin_only, _arity) in StealerApp.COMMANDS.items():
        owner, method = path.split(".", 1)
        assert callable(getattr(getattr(app, owner), method)), name
        assert admin_only is (name not in NON_ADMIN_COMMANDS), name


@pytest.mark.asyncio
async def test_admin_only_commands_reject_regular_users(tmp_path):
    app, store = _app(tmp_path, {"steal_meme": False})
    messages = await _run(app, ["on"], is_admin=False)
    assert messages == ["❌ 该指令仅限管理员使用"]
    assert store["steal_meme"] is False


@pytest.mark.asyncio
async def test_help_and_unknown_subcommands(tmp_path):
    app, _ = _app(tmp_path)
    assert "/meme status" in (await _run(app, []))[0]
    assert "/meme status" in (await _run(app, ["help"]))[0]
    unknown = (await _run(app, ["nope"]))[0]
    assert unknown.startswith("未知子命令: nope")


@pytest.mark.asyncio
async def test_extra_arguments_are_folded_into_the_last_parameter(tmp_path):
    app, _ = _app(tmp_path)
    seen = []

    async def delete_image(event, identifier=""):
        seen.append(identifier)
        yield "ok"

    app.image_commands.delete_image = delete_image
    await _run(app, ["delete", "my", "meme.png"])
    assert seen == ["my meme.png"]


@pytest.mark.asyncio
async def test_clean_command_does_not_run_as_part_of_tag_stats():
    from meme_stealer_core.core.commands.command_handler import CommandHandler

    cleaned = []

    async def clean_raw():
        cleaned.append(True)
        return 2

    def get_tag_stats(_top_n):
        return {
            "total_emojis": 1,
            "total_with_tags": 1,
            "zero_tag_count": 0,
            "top_tags": [{"tag": "happy", "count": 1}],
            "single_use_tags": [],
            "top_scenes": [],
        }

    plugin = SimpleNamespace(
        db_service=SimpleNamespace(get_tag_stats=get_tag_stats),
        event_handler=SimpleNamespace(_clean_raw_directory=clean_raw),
    )
    handler = CommandHandler(plugin)
    event = _event()

    stats = [message async for message in handler.tag_stats(event)]
    assert len(stats) == 1
    assert "标签统计" in stats[0]
    assert cleaned == []

    invalid = [message async for message in handler.clean(event, "categories")]
    assert "用法: /meme clean" in invalid[0]
    assert cleaned == []

    results = [message async for message in handler.clean(event)]
    assert results == ["✅ raw目录清理完成，共删除 2 张原始图片"]
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_toggle_commands_persist_and_report_save_failure(tmp_path, monkeypatch):
    app, store = _app(tmp_path, {"steal_meme": False, "auto_send_meme": False})

    for args, key, expected in (
        (["on"], "steal_meme", True),
        (["off"], "steal_meme", False),
        (["auto_on"], "auto_send_meme", True),
        (["auto_off"], "auto_send_meme", False),
    ):
        messages = await _run(app, args)
        assert messages and not messages[0].startswith("❌")
        assert store[key] is expected
        on_disk = json.loads(store.path.read_text(encoding="utf-8"))
        assert on_disk[key] is expected

    monkeypatch.setattr(app, "update_config", lambda _updates: False)
    failed = await _run(app, ["off"])
    assert failed and failed[0].startswith("❌")
    assert store["steal_meme"] is False


@pytest.mark.asyncio
async def test_status_reports_configured_models(tmp_path):
    app, _ = _app(
        tmp_path,
        {"vision_model": "qwen-vl-max", "vision_base_url": "https://vl.example/v1",
         "embedding_model": "text-embedding-v4", "embedding_base_url": "https://emb.example/v1"},
    )
    status = (await _run(app, ["status"], is_admin=False))[0]
    assert "视觉模型: qwen-vl-max" in status
    assert "嵌入模型: text-embedding-v4" in status
    # 不暴露接口地址
    assert "vl.example" not in status
