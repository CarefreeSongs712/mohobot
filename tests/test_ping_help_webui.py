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

    async def send_group_msg(self, bot_id, group_id, message):
        self.replies.append(("group", group_id, message))

    async def send_private_msg(self, bot_id, user_id, message):
        self.replies.append(("private", user_id, message))


def make_handler(ws):
    from mohobot.bot_manager import BotManager, BotInstance
    from mohobot.models.config import BotConfig
    bm = BotManager(data_dir=tempfile.mkdtemp())
    bm._bots["bot_001"] = BotInstance("bot_001", None, BotConfig(qq=1000, nickname="测试", agent_enabled=False))
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

    class HelpWS:
        def __init__(self):
            self.images = []
            self.sent = []

        async def send_image(self, bot_id, chat_type, chat_id, image_path):
            self.images.append((chat_type, chat_id, image_path))

        async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
            return {"status": "ok", "retcode": 0, "data": {}}

    ws = HelpWS()
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

    # 图片发送(PIL + 中文字体可用时)
    reply = await ch._cmd_help("bot_001", make_group_event(2001, "/help"), [])
    if reply is not None:
        # 降级为文本也应包含核心内容
        assert "/占卜" in reply and "群列表" in reply, reply
        assert ws.images == [], "有文本回复时不应发图"
    else:
        assert ws.images, "应发送帮助图片"
        chat_type, chat_id, path = ws.images[-1]
        # 发送后临时文件已被清理, 只需验证参数正确
        assert chat_type == "group" and str(chat_id) == "888888"
        assert path.endswith(".png")
    print("[+] /help 图片 OK")


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


# ── 4. beta 4 LLM 默认值 ────────────────────────────────────

def test_beta_llm_defaults():
    cfg = GlobalConfig()  # 默认配置
    m = cfg.agent.llm_modules
    assert m["main_chat"]["model"] == "DeepSeek-V4-Flash"
    assert m["topic_extractor"]["model"] == "DeepSeek-V4-Flash"
    assert m["memory_writer"]["model"] == "Qwen3-8B"
    assert m["user_profile_updater"]["model"] == "Qwen3-8B"
    assert "DeepSeek-V4-Flash" in cfg.llm.models and "Qwen3-8B" in cfg.llm.models
    # 旧配置(空 model)自动填充
    from mohobot.models.config import _fill_agent_llm_defaults
    old = {"main_chat": {"model": ""}, "memory_writer": {"model": "  "}}
    filled = _fill_agent_llm_defaults(old)
    assert filled["main_chat"]["model"] == "DeepSeek-V4-Flash"
    assert filled["memory_writer"]["model"] == "Qwen3-8B"
    assert "topic_extractor" in filled, "缺失模块应补齐默认"
    print("[+] beta LLM 默认值 OK")


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
