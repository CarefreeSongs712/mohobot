import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.onebot import PrivateMessageEvent, Sender

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "tests_beszel_servers_plugin", _ROOT / "plugins" / "beszel_servers" / "main.py"
)
plugin_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(plugin_mod)


def _record(name, status, info=None, updated="2026-11-16 15:36:33.374Z"):
    return {
        "id": f"rec_{name}",
        "name": name,
        "status": status,
        "host": "10.0.0.1",
        "port": 45876,
        "updated": updated,
        "info": info or {},
    }


def _private_event(text: str) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=0, self_id=1, post_type="message",
        message_type="private", sub_type="friend",
        message_id=42, user_id=1001,
        message=[{"type": "text", "data": {"text": text}}],
        raw_message=text, font=0,
        sender=Sender(user_id=1001, nickname="测试"),
    )


def test_global_triggers():
    assert plugin_mod.Plugin.global_triggers == {"/servers", "/服务器"}


def test_build_view_sorts_and_trims():
    systems = [
        _record("C", "up", {"cpu": 10, "mp": 20, "dp": 30, "la": [0.1, 0.2, 0.3], "u": 3600, "v": "0.21.1"}),
        _record("A", "up", {"cpu": 40, "mp": 50, "dp": 60, "la": [1.0, 0.9, 0.8], "u": 7200, "v": "0.21.1"}),
        _record("B", "down", {}),
    ]
    view = plugin_mod.build_view(systems, 2)
    assert [row["name"] for row in view["rows"]] == ["A", "B"]
    assert view["total"] == 3
    assert view["hidden"] == 1
    assert view["rows"][0]["cpu"] == 40
    assert view["rows"][0]["status_label"] == "运行中"
    assert view["rows"][1]["status_label"] == "离线"
    assert view["rows"][1]["cpu"] is None
    assert view["updated_text"] == "23:36:33"


def test_parse_updated_utc8():
    parsed = plugin_mod._parse_updated("2026-11-16 15:36:33.374Z")
    assert parsed is not None
    assert parsed.strftime("%Y-%m-%d %H:%M:%S") == "2026-11-16 23:36:33"
    assert plugin_mod._parse_updated("") is None
    assert plugin_mod._parse_updated("not-a-date") is None


def test_build_text_reports():
    systems = [
        _record("A", "up", {"cpu": 42, "mp": 55, "dp": 61, "la": [0.5, 0.4, 0.3], "u": 90061, "v": "0.21.2"}),
        _record("B", "down", {}),
        _record("C", "paused", {"cpu": 1}),
    ]
    text = plugin_mod.build_text(plugin_mod.build_view(systems, 2))
    assert "服务器状态（Beszel · 3 台）" in text
    assert "A  运行中  CPU 42%" in text
    assert "B  离线" in text
    assert "另有 1 台未显示" in text
    assert "数据更新 23:36:33（UTC+8）" in text


def test_render_card():
    systems = [
        _record("host-a", "up", {"cpu": 42, "mp": 55, "dp": 61, "la": [0.5, 0.4, 0.3], "u": 90061, "v": "0.21.2"}),
        _record("host-b", "down", {}, updated="2026-11-16 14:00:00.000Z"),
    ]
    path = plugin_mod.render_card(plugin_mod.build_view(systems, 12))
    from mohobot.utils.image_card import find_cjk_font

    if path is None:
        assert find_cjk_font() is None
        return
    try:
        assert os.path.getsize(path) > 1000
        with open(path, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"
    finally:
        os.remove(path)


async def test_on_message_requires_config():
    plugin = plugin_mod.Plugin()
    handled, reply = await plugin.on_message("bot_001", _private_event("/servers"), {})
    assert handled is True
    assert "未配置" in reply
    handled, reply = await plugin.on_message("bot_001", _private_event("/服务器"), {})
    assert handled is True
    assert "未配置" in reply


async def test_on_message_ignores_other_text():
    plugin = plugin_mod.Plugin()
    handled, reply = await plugin.on_message("bot_001", _private_event("看看服务器"), {})
    assert handled is False
    assert reply is None
