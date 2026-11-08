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
    # 面板/手工配置可能写成单个 int, 不能因迭代失败拖垮 /help
    assert resolve_contacts({"admin_qqs": 123})["admins"] == ["123"]
    assert resolve_contacts({"admin_qqs": 123.0})["admins"] == ["123.0"]
    print("[1] 联系方式解析(含标量配置) OK")


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
        assert "联系管理员: 90001" in text
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


GB = 1024 ** 3


def test_memory_conservation_free_based() -> None:
    """用 free/buffers/cached 做版本无关分解: 应用 + 缓存 + 空闲 == 总量。"""
    import plugins.status.main as status_main

    class VM:
        def __init__(self, total, free, available, used, buffers, cached):
            self.total = total
            self.free = free
            self.available = available
            self.used = used
            self.buffers = buffers
            self.cached = cached

    class Swap:
        total = 0
        used = 0
        percent = 0.0

    class FakePsutil:
        def __init__(self, version, vm):
            self.version_info = version
            self._vm = vm

        def virtual_memory(self):
            return self._vm

        def swap_memory(self):
            return Swap()

    old = status_main.HAS_PSUTIL
    status_main.HAS_PSUTIL = True
    try:
        # 7.2 真实形态: available 远大于 free; 不能把 available 当加法分量。
        total = 155 * GB // 10
        free = 1256 * GB // 100
        available = 1427 * GB // 100
        cache = 194 * GB // 100
        vm72 = VM(total, free, available, 123 * GB // 100, 0, cache)
        with patch.object(status_main, "psutil", FakePsutil((7, 2, 2), vm72)):
            data72 = status_main._read_memory()
        assert data72["used"] == total - free - cache
        assert data72["cache"] == cache
        assert data72["free"] == free
        assert data72["available"] == available
        assert data72["used"] + data72["cache"] + data72["free"] == total
        assert abs(data72["percent"] - (total - free) / total * 100) < 0.01

        # 7.0/5.9 旧形态: used 不含 cache, free=10, cache=40 → app=50
        vm_old = VM(100 * GB, 10 * GB, 60 * GB, 50 * GB, 5 * GB, 35 * GB)
        with patch.object(status_main, "psutil", FakePsutil((5, 9, 0), vm_old)):
            data_old = status_main._read_memory()
        assert data_old["used"] == 50 * GB
        assert data_old["cache"] == 40 * GB
        assert data_old["free"] == 10 * GB
        assert data_old["used"] + data_old["cache"] + data_old["free"] == 100 * GB
        assert round(data_old["percent"]) == 90
        assert round(data_old["app_percent"]) == 50

        # 异常/不一致数据: cache 超出剩余空间时截断, 仍严格守恒
        vm_bad = VM(100 * GB, 90 * GB, 95 * GB, 5 * GB, 5 * GB, 35 * GB)
        with patch.object(status_main, "psutil", FakePsutil((7, 2, 2), vm_bad)):
            data_bad = status_main._read_memory()
        assert data_bad["cache"] == 10 * GB
        assert data_bad["used"] == 0
        assert data_bad["used"] + data_bad["cache"] + data_bad["free"] == 100 * GB
    finally:
        status_main.HAS_PSUTIL = old
    print("[8] 内存三分量守恒(free 口径) OK")


def test_cpu_and_disk_readers() -> None:
    import os
    from types import SimpleNamespace
    import plugins.status.main as status_main

    class FakePsutil:
        version_info = (7, 2, 2)

        def cpu_percent(self, interval=None, percpu=False):
            return [10.0, 20.0, 30.0]

        def cpu_count(self, logical=True):
            return 3

        def disk_usage(self, mount):
            used = 30 * GB if mount.endswith("data") else 60 * GB
            return SimpleNamespace(total=100 * GB, used=used, percent=60.0)

        def disk_partitions(self, all=False):
            main = status_main._main_disk_mount()
            return [
                SimpleNamespace(mountpoint=main, fstype="ext4"),
                SimpleNamespace(mountpoint=main, fstype="ext4"),  # 重复主盘
                SimpleNamespace(mountpoint=os.path.join(main, "data"), fstype="ext4"),
            ]

    old = status_main.HAS_PSUTIL
    status_main.HAS_PSUTIL = True
    try:
        with patch.object(status_main, "psutil", FakePsutil()):
            cpu = status_main._read_cpu()
            disks = status_main._read_disks()
        assert cpu["count"] == 3
        assert cpu["average"] == 20.0
        assert cpu["cores"] == [10.0, 20.0, 30.0]
        assert len(disks) == 2, "重复主盘应去重"
        assert disks[0]["mount"] == os.path.abspath(status_main._main_disk_mount())
        assert disks[1]["mount"].endswith("data")
    finally:
        status_main.HAS_PSUTIL = old

    # Windows: "/" 与系统盘符应视为同一卷(复审 N2)
    if os.name == "nt":
        class WinPsutil(FakePsutil):
            def disk_partitions(self, all=False):
                return [
                    SimpleNamespace(mountpoint="/", fstype="NTFS"),
                    SimpleNamespace(mountpoint=os.path.abspath(os.sep), fstype="NTFS"),
                ]

        with patch.object(status_main, "psutil", WinPsutil()):
            disks = status_main._read_disks()
        assert len(disks) == 1, "Windows 主盘不应重复显示 / 与 C:\\"
    print("[9] CPU/磁盘读取与主盘去重 OK")


async def test_status_render_and_send_fallbacks() -> None:
    import plugins.status.main as status_main

    class StatusEvent:
        user_id = 2001
        message = [{"type": "text", "data": {"text": "/status"}}]

    class SendFailWS:
        async def send_image(self, *args, **kwargs):
            raise RuntimeError("offline send")

    old_bm = status_main.Plugin._bot_manager
    old_has = status_main.HAS_PSUTIL
    status_main.Plugin._bot_manager = None
    status_main.HAS_PSUTIL = False
    try:
        plugin = status_main.Plugin()
        plugin.plugin_config = {
            "admin_qqs": [], "community_group": "123", "help_url": "https://x/",
        }

        # 未注入 ws: 不应渲染, 直接文本
        plugin._ws_server = None
        with patch("mohobot.utils.status_card.render_status_card") as render:
            handled, reply = await plugin.on_message("bot_001", StatusEvent(), {})
        assert handled and "Mohobot 状态" in reply
        render.assert_not_called()

        # 渲染异常: 降级文本, 不冒泡
        plugin._ws_server = SendFailWS()
        with patch("mohobot.utils.status_card.render_status_card",
                   side_effect=RuntimeError("render boom")):
            handled, reply = await plugin.on_message("bot_001", StatusEvent(), {})
        assert handled and "Mohobot 状态" in reply

        # 发送失败: 降级文本并清理临时图
        fd, path = tempfile.mkstemp(suffix=".png", prefix="status_fail_")
        import os
        os.close(fd)
        with patch("mohobot.utils.status_card.render_status_card", return_value=path):
            handled, reply = await plugin.on_message("bot_001", StatusEvent(), {})
        assert handled and "Mohobot 状态" in reply
        assert not Path(path).exists(), "发送失败也应清理临时图"
    finally:
        status_main.Plugin._bot_manager = old_bm
        status_main.HAS_PSUTIL = old_has
    print("[9] /status 渲染/发送降级与临时文件 OK")


def test_help_text_admin_filter() -> None:
    ch = CommandHandler(None, None, None)
    full = ch._help_text(is_admin=True)
    limited = ch._help_text()
    assert "/persona" in full
    assert "/persona" not in limited
    assert "管理员指令已折叠" in limited
    print("[10] 文本降级管理员折叠 OK")


async def test_help_multipage_send_failure_no_duplicate() -> None:
    class FailingWS:
        def __init__(self):
            self.calls = 0

        async def send_private_msg(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("second page failed")

    ws = FailingWS()
    ch = CommandHandler(None, None, ws)
    ch.register_plugin_commands({f"cmd{i}": "x" for i in range(35)})

    created: list[str] = []

    def _fake_render(*args, **kwargs):
        import os
        fd, path = tempfile.mkstemp(suffix=".png", prefix="help_fail_")
        os.close(fd)
        Path(path).write_bytes(b"img")
        created.append(path)
        return path

    with patch("mohobot.utils.image_card.render_help_card", side_effect=_fake_render):
        reply = await ch._cmd_help("bot_001", FakeEvent(), ["工具"])
    assert "已发送 1/2 页" in reply
    assert "可用指令" not in reply, "已发页后不应再整段重发文本"
    assert len(created) == 2
    assert all(not Path(p).exists() for p in created)
    print("[11] 多页发送失败不重复文本 OK")


def test_help_detail_lines_separate() -> None:
    from PIL import ImageDraw, ImageFont
    from mohobot.utils import image_card

    fonts = {size: ImageFont.load_default(size=size) for size in (12, 14, 17, 22)}
    sections = [{
        "title": "工具 · demo", "category": "工具",
        "commands": [{
            "name": "demo", "desc": "演示", "usage": "/demo <参数>",
            "aliases": ["d", "演示别名"], "admin": False,
        }],
    }]
    original = ImageDraw.ImageDraw.text
    with patch.object(image_card, "find_cjk_font", return_value="mock-font"), \
            patch.object(ImageFont, "truetype", side_effect=lambda path, size: fonts[size]), \
            patch.object(ImageDraw.ImageDraw, "text", autospec=True, side_effect=original) as draw_text:
        path = image_card.render_help_card(
            sections, contacts={
                "admins": ["1"], "group": "2", "url": "https://x/",
            },
        )
    try:
        calls = [
            call for call in draw_text.call_args_list
            if call.args[2].startswith("用法：") or call.args[2].startswith("别名：")
        ]
        assert len(calls) == 2
        usage_call = next(c for c in calls if c.args[2].startswith("用法："))
        alias_call = next(c for c in calls if c.args[2].startswith("别名："))
        assert alias_call.args[1][1] == usage_call.args[1][1] + 20, "别名应独立一行"
    finally:
        Path(path).unlink()
    print("[12] 用法/别名分行渲染 OK")


async def main() -> None:
    test_contacts_resolution()
    test_status_bot_list_and_fallback()
    await test_status_without_psutil()
    test_help_categories_and_admin_collapse()
    test_help_pagination_and_category_filter()
    test_help_desc_parsing()
    await test_help_category_route_and_multipage()
    test_memory_conservation_free_based()
    test_cpu_and_disk_readers()
    await test_status_render_and_send_fallbacks()
    test_help_text_admin_filter()
    await test_help_multipage_send_failure_no_duplicate()
    test_help_detail_lines_separate()


if __name__ == "__main__":
    asyncio.run(main())
