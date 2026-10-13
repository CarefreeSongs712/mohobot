"""P1-5 real list entrypoints: scoped DB pagination, counts and fallback."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from meme_stealer_core.app import StealerApp
from meme_stealer_core.core.commands.image_mgmt_command import ImageManagementCommand
from meme_stealer_core.core.config.config import PluginConfig
from meme_stealer_core.core.db.database_service import DatabaseService
from meme_stealer_core.core.db.index_manager import IndexManager
from meme_stealer_core.host import StealerEvent

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _event(text="/meme list", *, group=444, user=42, bot="bot_001"):
    original = SimpleNamespace(group_id=group, user_id=user, self_id=111, message_id=10,
                              sender=SimpleNamespace(nickname="fixture"),
                              message=[{"type": "text", "data": {"text": text}}])
    return original, StealerEvent(bot, original, {})


class _BridgeWS:
    def __init__(self):
        self.sent = []
    async def send_group_msg(self, *args, **kw):
        self.sent.append(args)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
    async def send_private_msg(self, *args, **kw):
        self.sent.append(args)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}


@pytest.mark.asyncio
async def test_real_plugin_list_keeps_local_image_and_counts_inside_scope(tmp_path):
    spec = importlib.util.spec_from_file_location("review_scope_entry", PLUGIN_DIR / "main.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    ws = _BridgeWS()
    entry.Plugin.inject_ws_server(ws)
    entry.Plugin.inject_data_dir(tmp_path)
    entry.Plugin.inject_admin_ids([7777])
    plugin = entry.Plugin()
    # Real app constructor services/DB. Avoid periodic startup services: list needs none.
    plugin.app = StealerApp({"enable_embedding_search": False}, tmp_path, plugin_dir=PLUGIN_DIR)
    app = plugin.app
    rendered = []
    real_render = app.image_render_service.render_emoji_list_page_base64
    async def capture(**kw):
        rendered.append(kw)
        return await real_render(**kw)
    app.image_render_service.render_emoji_list_page_base64 = capture
    for name, origin, mode, created, category in (
        ("private-A", "group:333", "public", 30, "uncategorized"),
        ("public", "", "public", 10, "uncategorized"),
        ("private-B", "group:444", "local", 20, "happy"),
    ):
        path = app.plugin_config.ensure_category_dir(category) / (name + ".png")
        Image.new("RGB", (16, 16), "red").save(path)
        await app.db_service.insert_batch([{"path": str(path), "hash": name, "desc": name,
          "created_at": created, "category": category, "origin_target": origin, "scope_mode": mode}])
    admin, _ = _event("/meme scope private-A.png local", group=333, user=7777)
    _, reply = await plugin.on_message("bot_001", admin, {})
    assert "已更新" in reply
    try:
        for group, bot, expected in ((444, "bot_001", {"public", "private-B"}),
                                     (444, "bot_002", {"public", "private-B"}),
                                     (333, "bot_001", {"public", "private-A"}),
                                     (0, "bot_001", {"public"})):
            original, _ = _event(group=group, bot=bot)
            renders_before = len(rendered)
            await plugin.on_message(bot, original, {})
            # 必须断言本次确实渲染过: 否则可见集合为空时会提前 yield "暂无数据",
            # rendered[-1] 复检上一次的结果, 断言就失去鉴别力。
            assert len(rendered) == renders_before + 1, (group, bot, len(rendered))
            assert {i["desc"] for i in rendered[-1]["items"]} == expected
            assert rendered[-1]["total_all"] == len(expected)
            assert rendered[-1]["total_filtered"] == len(expected)
        # Authorized numbering stays compatible with the global numeric admin actions,
        # but admin listing in an ordinary chat still does not reveal foreign images.
        original, _ = _event("/meme list", group=444, user=7777)
        renders_before = len(rendered)
        await plugin.on_message("bot_001", original, {})
        assert len(rendered) == renders_before + 1
        assert [i["desc"] for i in rendered[-1]["items"]] == ["private-B", "public"]
        assert [i["index"] for i in rendered[-1]["items"]] == [2, 3]
        index = app.db_service.get_index_cache_readonly()
        assert app.image_commands._find_target_image(index, "2")["name"] == "private-B.png"
        assert app.image_commands._find_target_image(index, "3")["name"] == "public.png"
        original, _ = _event("/meme list uncategorized 1 99", group=444)
        renders_before = len(rendered)
        await plugin.on_message("bot_001", original, {})
        assert len(rendered) == renders_before + 1
        assert [i["desc"] for i in rendered[-1]["items"]] == ["public"]
        assert rendered[-1]["total_filtered"] == 1 and rendered[-1]["total_all"] == 2
        app.image_render_service = None
        _, text = await plugin.on_message("bot_001", original, {})
        assert "private-A" not in text
        assert "public" in text
        assert "总 2" in text
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_list_fallback_and_unknown_target_deny_local(tmp_path):
    db = DatabaseService(tmp_path / "emoji.db")
    cfg = PluginConfig({}, tmp_path)
    index = {}
    for name, mode, origin in (("private-A", "local", "group:333"),
                               ("public", "public", "")):
        path = tmp_path / (name + ".png")
        Image.new("RGB", (8, 8), "blue").save(path)
        index[str(path)] = {"scope_mode": mode, "origin_target": origin, "desc": name,
                            "category": "uncategorized", "hash": name}
    await db.insert_batch([{"path": path, **meta} for path, meta in index.items()])
    plugin = SimpleNamespace(db_service=db, plugin_config=cfg, image_render_service=None)
    plugin.index_manager = IndexManager(plugin)
    command = ImageManagementCommand(plugin)
    _, unknown = _event(group=0, user=0)
    results = [r async for r in command.list_images(unknown)]
    assert "private-A" not in results[0].get_plain_text()
    assert "public" in results[0].get_plain_text()
    # Production fallback uses the same true index; disable only optional DB path.
    plugin.db_service = None
    _, foreign = _event(group=444)
    results = [r async for r in command.list_images(foreign)]
    assert "private-A" not in results[0].get_plain_text()
    assert "public" in results[0].get_plain_text()
