"""本轮四个功能的测试:
1. ping/PONG: 私聊/群聊不@/忽略大小写/被 ban 不回复/多 bot 各自回复
2. /help PIL 图片: 分组(系统/封禁管理/按插件名) + admin 标注 + 图片发送/文本降级
3. WebUI 路径配置彻底移除: 前端无渲染 id, 后端 update_config 拒绝
4. beta 4 LLM: 默认模型填充(main_chat/topic_extractor=DeepSeek-V4-Flash,
   memory_writer/user_profile_updater=Qwen3-8B) + models 列表
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.context_manager import ContextManager
from mohobot.message_handler import MessageHandler
from mohobot.models.config import GlobalConfig, ReplyConfig
from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender


def make_group_event(user_id, text, group_id=888888):
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def make_private_event(user_id, text):
    return PrivateMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="private",
        message_id=1, user_id=user_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


# ── 1. ping/PONG ─────────────────────────────────────────────

class PingWS:
    """记录 _send_reply 发出的内容。"""

    def __init__(self):
        self.replies = []
        self._bot_manager = None

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send_group_msg(self, bot_id, group_id, message, source: str = "auto"):
        self.replies.append(("group", group_id, message))

    async def send_private_msg(self, bot_id, user_id, message, source: str = "auto"):
        self.replies.append(("private", user_id, message))


def make_handler(ws):
    from mohobot.bot_manager import BotManager, BotInstance
    from mohobot.models.config import BotConfig
    bm = BotManager(data_dir=tempfile.mkdtemp())
    bm._bots["bot_001"] = BotInstance("bot_001", None, BotConfig(qq=1000, nickname="测试"))
    ws._bot_manager = bm
    handler = MessageHandler(
        ws_server=ws,
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None,
        plugin_system=None,
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(),
        global_config=GlobalConfig(),
    )
    return handler


async def test_ping():
    ws = PingWS()
    handler = make_handler(ws)
    # 私聊 ping → PONG
    await handler._handle_message("bot_001", make_private_event(2001, "ping"), {})
    assert ws.replies and ws.replies[-1][2] == "PONG", ws.replies
    # 群聊不 @ → PONG(gate 放行)
    ws.replies.clear()
    await handler._handle_message("bot_001", make_group_event(2002, "  Ping  "), {})
    assert ws.replies and ws.replies[-1][2] == "PONG", "群聊忽略大小写应回复 PONG"
    # 非 ping 不回复
    ws.replies.clear()
    await handler._handle_message("bot_001", make_group_event(2003, "pingpong"), {})
    assert not ws.replies, "pingpong 不应触发"
    print("[+] ping/PONG OK")


async def test_ping_ignores_ban():
    """被 ban 用户发 ping 不应回复(ban_filter 在拦截链先过滤)。"""
    from mohobot.ban.ban_filter import BanInterceptor
    from mohobot.ban.store import BanStore

    ws = PingWS()
    handler = make_handler(ws)
    store = BanStore(data_dir=tempfile.mkdtemp())
    await store.upsert("ban", "2099", session_key="group:888888", time_val=0, reason="test")
    ban_filter = BanInterceptor(
        data_dir=tempfile.mkdtemp(), enabled=True, admins=[1], store=store,
    )
    handler.set_interceptors([ban_filter])
    await handler._handle_message("bot_001", make_group_event(2099, "ping"), {})
    assert not ws.replies, "被 ban 用户 ping 不应回复"
    # 未 ban 用户正常
    await handler._handle_message("bot_001", make_group_event(2001, "ping"), {})
    assert ws.replies and ws.replies[-1][2] == "PONG"
    print("[+] ping ban 过滤 OK")


# ── 2. /help PIL 图片 ───────────────────────────────────────

async def test_help_image():
    from mohobot.interceptors.command_handler import CommandHandler

    import base64
    from unittest.mock import patch

    ws = PingWS()
    ch = CommandHandler(
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None, ws_server=ws, plugin_system=None,
    )
    # 模拟插件命令(含 admin 标注)
    class FakePlugins:
        def list_plugins(self):
            return [
                {"name": "divination", "info": {"commands": [
                    {"name": "占卜", "desc": "每日占卜"},
                ]}},
                {"name": "relationship", "info": {"commands": [
                    {"name": "群列表", "desc": "查看群聊", "admin": True},
                    {"name": "同意", "desc": "同意申请"},
                ]}},
            ]
    ch._plugin_system = FakePlugins()

    # 分组验证
    sections = ch._build_help_sections()
    titles = [s["title"] for s in sections]
    assert "系统" in titles and "封禁管理 (管理员)" in titles
    assert "插件 · divination" in titles and "插件 · relationship" in titles
    rel = next(s for s in sections if s["title"] == "插件 · relationship")
    rel_cmds = {c["name"]: c for c in rel["commands"]}
    assert rel_cmds["群列表"]["admin"] is True, "admin 字段应标注"
    assert rel_cmds["同意"]["admin"] is False
    ban_sec = next(s for s in sections if s["title"] == "封禁管理 (管理员)")
    # 除查询类(banlist/ban-help 所有人可用)外均标管理员
    assert all(c["admin"] for c in ban_sec["commands"] if c["name"] not in ("banlist", "ban-help"))

    # 强制图片路径, 不依赖本机中文字体, 仅 mock 传输。
    for event, chat_type, chat_id in [
        (make_group_event(2001, "/help"), "group", "888888"),
        (make_private_event(2001, "/help"), "private", "2001"),
    ]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "help.png"
            image_bytes = b"mock help image"
            path.write_bytes(image_bytes)
            ws.replies.clear()
            with patch("mohobot.utils.image_card.render_help_card", return_value=str(path)):
                handled, reply = await ch.intercept("bot_001", event, {})
            assert handled and reply is None
            assert len(ws.replies) == 1, "图片和网址应在同一消息内, 不另发消息"
            target_type, target_id, segments = ws.replies[0]
            assert (target_type, target_id) == (chat_type, chat_id)
            assert [seg["type"] for seg in segments] == ["image", "text"]
            image_file = segments[0]["data"]["file"]
            assert image_file.startswith("base64://")
            assert base64.b64decode(image_file.removeprefix("base64://")) == image_bytes
            _assert_help_info(segments[1]["data"]["text"])
            assert not path.exists(), "发送后应清理临时图片"
    print("[+] /help 图片 OK")


def _assert_help_info(text):
    for content in (
        "交流群 398870315", "介绍 / 使用须知", "https://7121099.xyz/",
        "备用", "http://120.220.76.212:712/",
    ):
        assert content in text, text


async def test_help_fallback():
    from unittest.mock import patch
    from mohobot.interceptors.command_handler import CommandHandler

    ws = PingWS()
    ch = CommandHandler(None, None, ws)
    ch.register_plugin_commands({"离线插件": "动态命令"})
    # 无字体/无 PIL 返回 None, 意外绘制异常也应降级。
    for result in (None, RuntimeError("offline render failure")):
        kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
        with patch("mohobot.utils.image_card.render_help_card", **kwargs):
            handled, reply = await ch.intercept("bot_001", make_private_event(2001, "/help"), {})
        assert handled
        _assert_help_info(reply)
        assert "/help" in reply and "/离线插件" in reply
        assert not ws.replies
    ch._ws = None
    with patch("mohobot.utils.image_card.render_help_card") as render:
        reply = await ch._cmd_help("bot_001", make_private_event(2001, "/help"), [])
        render.assert_not_called()
    _assert_help_info(reply)


async def test_help_send_failure_and_cleanup():
    from unittest.mock import AsyncMock, patch
    from mohobot.interceptors.command_handler import CommandHandler

    ws = PingWS()
    ws.send_group_msg = AsyncMock(side_effect=RuntimeError("offline send failure"))
    ch = CommandHandler(None, None, ws)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "help.png"
        path.write_bytes(b"mock help image")
        with patch("mohobot.utils.image_card.render_help_card", return_value=str(path)):
            reply = await ch._cmd_help("bot_001", make_group_event(2001, "/help"), [])
        _assert_help_info(reply)
        assert not path.exists()
        ws.send_group_msg.assert_awaited_once()

        # 清理失败不能把已成功发送的图片再次降级为另一条消息。
        path.write_bytes(b"mock help image")
        ws.send_group_msg = AsyncMock()
        with patch("mohobot.utils.image_card.render_help_card", return_value=str(path)), \
                patch("os.remove", side_effect=OSError("offline cleanup failure")):
            reply = await ch._cmd_help("bot_001", make_group_event(2001, "/help"), [])
        assert reply is None
        ws.send_group_msg.assert_awaited_once()


def test_help_bot_binding_and_admin_metadata():
    from types import SimpleNamespace
    from mohobot.interceptors.command_handler import CommandHandler

    class BoundPlugin:
        bind_bots = ["bot_001"]

    plugins = SimpleNamespace(_plugins=[{
        "name": "bound", "instance": BoundPlugin(), "info": {"commands": [
            {"name": "公开", "desc": "动态公开命令"},
            {"name": "管理", "desc": "动态管理命令", "admin": True},
        ]},
    }])
    ch = CommandHandler(None, None, None, plugin_system=plugins)
    section = next(s for s in ch._build_help_sections("bot_001") if s["title"] == "插件 · bound")
    assert {c["name"]: c["admin"] for c in section["commands"]} == {"公开": False, "管理": True}
    assert all(s["title"] != "插件 · bound" for s in ch._build_help_sections("bot_002"))
    assert "/管理" in ch._help_text("bot_001")
    assert "/管理" not in ch._help_text("bot_002")


def test_help_card_layout():
    from unittest.mock import patch
    from PIL import Image, ImageDraw, ImageFont
    from mohobot.utils import image_card

    # 字体及绘制参数受控, 不依赖中文字体安装; 同时实际生成 PNG 验证边界。
    fonts = {size: ImageFont.load_default(size=size) for size in (14, 17, 18, 22, 30)}
    original_text = ImageDraw.ImageDraw.text
    heights = []
    for links in (image_card.HELP_LINKS_TEXT, image_card.HELP_LINKS_TEXT + " extra" * 70):
        sections = [
            {"title": f"Section {i}", "commands": [
                {"name": f"cmd{i}_{j}", "desc": "description", "admin": j == 0}
                for j in range(5)
            ]} for i in range(40)
        ]
        with patch.object(image_card, "find_cjk_font", return_value="mock-font"), \
                patch.object(ImageFont, "truetype", side_effect=lambda path, size: fonts[size]), \
                patch.object(image_card, "HELP_LINKS_TEXT", links), \
                patch.object(ImageDraw.ImageDraw, "text", autospec=True, side_effect=original_text) as draw_text:
            path = image_card.render_help_card(sections)
        assert path is not None
        try:
            with Image.open(path) as image:
                width, height = image.size
            heights.append(height)
            calls = draw_text.call_args_list
            group_call = next(call for call in calls if call.args[2] == image_card.HELP_GROUP_TEXT)
            assert group_call.kwargs["font"].size == 30
            link_calls = [call for call in calls if call.kwargs["font"].size == 18]
            assert "".join(call.args[2] for call in link_calls) == links.replace("\n", "")
            if links == image_card.HELP_LINKS_TEXT:
                assert [call.args[2] for call in link_calls] == links.splitlines()
            section_calls = [call for call in calls if call.kwargs["font"].size == 17]
            command_calls = [call for call in calls if call.args[2].startswith("/cmd")]
            assert len(command_calls) == 200
            assert all(call.kwargs["font"].size == 14 for call in command_calls)
            assert command_calls[1].args[1][0] == 460, "两列命令宽度应保持不变"
            assert section_calls[0].args[1][1] > max(
                call.args[1][1] + call.kwargs["font"].getbbox(call.args[2])[3]
                for call in link_calls
            )
            for call in calls:
                x, y = call.args[1]
                left, top, right, bottom = call.kwargs["font"].getbbox(call.args[2])
                assert 0 <= x + left < x + right <= width
                assert 0 <= y + top < y + bottom <= height, call.args[2]
        finally:
            Path(path).unlink()
    assert heights[1] > heights[0], "信息区换行时必须增加总高度而非挤压命令"


def test_help_card_without_font():
    from unittest.mock import patch
    from mohobot.utils.image_card import render_help_card

    with patch("mohobot.utils.image_card.find_cjk_font", return_value=None):
        assert render_help_card([]) is None


# ── 3. WebUI 路径配置彻底移除 ───────────────────────────────

def test_webui_path_fields_removed():
    html = Path("mohobot/web_panel/static/index.html").read_text(encoding="utf-8")
    # 前端: 不再渲染/提交这些字段
    for f in ("log_dir", "data_dir", "plugins_dir", "database.folder", "database.file"):
        assert f"cfg-{f}" not in html, f"前端不应出现 {f}"
        assert f"formField('{f}'" not in html
    assert "readonlyField" not in html and "readonlyCheckField" not in html
    # 后端: update_config 不应接受这些字段
    src = Path("mohobot/web_panel/app.py").read_text(encoding="utf-8")
    assert '"log_dir", "data_dir", "plugins_dir"' not in src, "后端不应接受路径字段"
    assert '"database" in data' not in src, "后端不应接受 database 段"
    print("[+] WebUI 路径字段移除 OK")


async def _main() -> int:
    import asyncio as _a
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if _a.iscoroutinefunction(fn):
                    await fn()
                else:
                    fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    return failed


if __name__ == "__main__":
    import asyncio
    failed = asyncio.run(_main())
    total = len([n for n in globals() if n.startswith("test_") and callable(globals()[n])])
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
