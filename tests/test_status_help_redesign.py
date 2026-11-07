"""状态卡 / 帮助卡改版回归测试。

覆盖:
1. 联系方式解析(插件配置键与已解析键)
2. /status 插件: 昵称排序与空昵称回退 QQ、无 psutil 降级
3. /help: 分类顺序、管理员折叠、分页、别名/管理员标记解析
4. /help <分类> 路由与多页发送
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.interceptors.command_handler import CommandHandler
from mohobot.utils.contact_info import (
    DEFAULT_COMMUNITY_GROUP,
    DEFAULT_HELP_URL,
    resolve_contacts,
)


class FakeEvent:
    def __init__(self, user_id=2001, message=None):
        self.user_id = user_id
        self.message = message or []


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_private_msg(self, bot_id, user_id, message):
        self.sent.append(("private", str(user_id), message))

    async def send_group_msg(self, bot_id, group_id, message):
        self.sent.append(("group", str(group_id), message))


def test_contacts_resolution() -> None:
    default = resolve_contacts(None, fallback_admins=[10001])
    assert default == {
        "admins": ["10001"],
        "group": DEFAULT_COMMUNITY_GROUP,
        "url": DEFAULT_HELP_URL,
    }
    plugin_cfg = resolve_contacts({
        "admin_qqs": "10001, 10002",
        "community_group": "123456",
        "help_url": "https://example.test/",
    })
    assert plugin_cfg["admins"] == ["10001", "10002"]
    assert plugin_cfg["group"] == "123456"
    assert plugin_cfg["url"] == "https://example.test/"
    # 已解析键也可直接回传(渲染层内部使用)
    resolved = resolve_contacts({
        "admins": ["9"], "group": "8", "url": "https://x.test/",
    })
    assert resolved == {"admins": ["9"], "group": "8", "url": "https://x.test/"}
    print("[1] 联系方式解析 OK")


def test_status_bot_list_and_fallback() -> None:
    from plugins.status.main import Plugin

    class Cfg:
        def __init__(self, nickname):
            self.nickname = nickname

    class Bot:
        def __init__(self, bot_id, qq, nickname, connected_at):
            self.bot_id = bot_id
            self.qq = qq
            self.config = Cfg(nickname)
            self.connected_at = connected_at

    class BM:
        all_bots = [
            Bot("bot_002", 222, "", 0.0),
            Bot("bot_001", 111, "一号", 0.0),
        ]

    old_bm = Plugin._bot_manager
    Plugin._bot_manager = BM()
    try:
        plugin = Plugin()
        bots = plugin._bot_list()
        assert [b["bot_id"] for b in bots] == ["bot_001", "bot_002"]
        assert bots[0]["nickname"] == "一号"
        assert bots[1]["nickname"] == "222", "空昵称应回退 QQ"
    finally:
        Plugin._bot_manager = old_bm
    print("[2] Bot 昵称排序/回退 OK")


async def test_status_without_psutil() -> None:
    import plugins.status.main as status_main

    old = status_main.HAS_PSUTIL
    old_admins = status_main.Plugin._admin_ids
    status_main.HAS_PSUTIL = False
    status_main.Plugin._admin_ids = ["90001"]
    try:
        plugin = status_main.Plugin()
        plugin.plugin_config = {
            "admin_qqs": [], "community_group": "123", "help_url": "https://x/",
        }
        data = await plugin._collect_status("bot_001")
        assert data["cpu"] == {} and data["disks"] == []
        assert data["contacts"]["admins"] == ["90001"]
        assert data["contacts"]["group"] == "123"
        text = status_main._text_report(data)
        assert "管理员: 90001" in text
        assert "交流群: 123" in text
    finally:
        status_main.HAS_PSUTIL = old
        status_main.Plugin._admin_ids = old_admins
    print("[3] 无 psutil 降级 OK")


def test_help_categories_and_admin_collapse() -> None:
    ch = CommandHandler(None, None, None)
    sections = ch._build_help_sections()
    cats = [s["category"] for s in sections]
    order = list(ch._HELP_CATEGORY_ORDER)
    assert cats == sorted(cats, key=order.index), cats
    assert {"内置", "会话", "封禁", "管理"} <= set(cats)

    fake = [{
        "title": "工具 · demo", "category": "工具",
        "commands": [
            {"name": "公开", "desc": "", "admin": False},
            {"name": "管理1", "desc": "", "admin": True},
            {"name": "管理2", "desc": "", "admin": True},
        ],
    }]
    pages, total, available, hidden = ch._build_help_pages(fake, is_admin=False)
    assert (total, available, hidden) == (3, 1, 2)
    names = [c["name"] for sec in pages[0] for c in sec["commands"]]
    assert names == ["公开"]

    _, _, _, hidden_admin = ch._build_help_pages(fake, is_admin=True)
    assert hidden_admin == 0
    print("[4] 分类顺序与管理员折叠 OK")


def test_help_pagination_and_category_filter() -> None:
    ch = CommandHandler(None, None, None)
    sections = [
        {"title": "工具 · big", "category": "工具", "commands": [
            {"name": f"c{i}", "desc": "", "admin": False} for i in range(65)
        ]},
        {"title": "娱乐 · fun", "category": "娱乐", "commands": [
            {"name": "d1", "desc": "", "admin": False},
        ]},
    ]
    pages, total, available, hidden = ch._build_help_pages(sections, is_admin=True)
    assert total == 66 and available == 66 and hidden == 0
    assert len(pages) == 3
    assert all(sum(len(s["commands"]) for s in page) <= 30 for page in pages)
    assert sum(len(s["commands"]) for page in pages for s in page) == 66

    only_fun, total_fun, _, _ = ch._build_help_pages(
        sections, is_admin=True, category="娱乐",
    )
    assert total_fun == 1
    assert [s["title"] for s in only_fun[0]] == ["娱乐 · fun"]
    print("[5] 分页与分类过滤 OK")


def test_help_desc_parsing() -> None:
    ch = CommandHandler(None, None, None)
    desc, usage, aliases, admin = ch._parse_help_desc(
        "点歌 <关键词> — 播放(别名: music/听歌/网易云)\n第二行",
        usage="用法: /点歌 a\n/点歌 b",
    )
    assert aliases == ["music", "听歌", "网易云"]
    assert admin is False
    assert "别名" not in desc and "第二行" in desc
    assert usage == "/点歌 a / /点歌 b", usage

    desc2, _, _, admin2 = ch._parse_help_desc("危险操作(管理员)")
    assert admin2 is True
    assert desc2 == "危险操作"
    print("[6] 描述/别名/管理员解析 OK")


async def test_help_category_route_and_multipage() -> None:
    ws = FakeWS()
    ch = CommandHandler(None, None, ws)
    ch._plugin_system = None

    # 未知分类直接返回文本, 不渲染
    reply = await ch._cmd_help("bot_001", FakeEvent(), ["不存在"])
    assert "未知分类" in reply and "可用分类" in reply

    # 注册 35 条插件命令, 总条数超过单页 → 多页发送
    ch.register_plugin_commands({f"cmd{i}": "x" for i in range(35)})
    sections = ch._build_help_sections()
    pages, _, _, _ = ch._build_help_pages(sections, is_admin=False)
    assert len(pages) >= 2, "命令超页时应拆分"

    created: list[str] = []

    def _fake_render(*args, **kwargs):
        fd, path = tempfile.mkstemp(suffix=".png", prefix="help_page_")
        import os
        os.close(fd)
        Path(path).write_bytes(b"img")
        created.append(path)
        return path

    with patch("mohobot.utils.image_card.render_help_card", side_effect=_fake_render):
        result = await ch._cmd_help("bot_001", FakeEvent(), [])
    assert result is None
    assert len(ws.sent) == len(pages), "每个分页一张图"
    assert all(seg["type"] == "image" for _, _, msg in ws.sent for seg in msg)
    assert len(created) == len(pages)
    assert all(not Path(p).exists() for p in created), "发送后应清理临时图"
    print("[7] /help 分类路由与多页发送 OK")


async def main() -> None:
    test_contacts_resolution()
    test_status_bot_list_and_fallback()
    await test_status_without_psutil()
    test_help_categories_and_admin_collapse()
    test_help_pagination_and_category_filter()
    test_help_desc_parsing()
    await test_help_category_route_and_multipage()


if __name__ == "__main__":
    asyncio.run(main())
