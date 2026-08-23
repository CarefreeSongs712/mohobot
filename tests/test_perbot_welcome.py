"""per-bot 插件绑定 + relationship 欢迎消息测试:
1. wifepicker bind_bots=["bot_001"]: 拦截/观察钩子/命令收集对非绑定 bot 全部跳过
2. 新好友欢迎: friend_add → 随机延迟 3~5s → 私聊发送(模板昵称替换)
3. 新入群欢迎: group_increase(不退群) → 群欢迎; 自动退群的场景不发
4. 开关关闭/模板为空 → 不发送
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_group_event(user_id, text):
    from mohobot.models.onebot import GroupMessageEvent, Sender
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=888888,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


# ── 1. per-bot 绑定 ─────────────────────────────────────────

async def test_perbot_binding():
    from mohobot.interceptors.plugin_system import PluginSystem
    ps = PluginSystem(plugins_dir="plugins", data_dir=tempfile.mkdtemp())
    await ps.load_plugins()
    meta = next(m for m in ps._plugins if m["name"] == "wifepicker")
    inst = meta["instance"]
    assert getattr(inst.__class__, "bind_bots") == ["bot_001"]

    # intercept: 非绑定 bot 直接跳过
    ev = make_group_event(2001, "/今日老婆")
    handled, reply = await ps.intercept("bot_002", ev, {})
    assert not handled, "bot_002 不应处理 wifepicker 命令"
    # 绑定 bot 正常处理
    handled2, reply2 = await ps.intercept("bot_001", ev, {})
    assert handled2, "bot_001 应处理 wifepicker 命令"

    # 观察钩子同样过滤
    handled3, _ = await ps.dispatch_observed("bot_002", make_group_event(2001, "抽老婆"), {})
    assert not handled3
    print("[+] per-bot 拦截/观察过滤 OK")


async def test_command_collection_filtered():
    from mohobot.interceptors.command_handler import CommandHandler
    from mohobot.context_manager import ContextManager
    from mohobot.interceptors.plugin_system import PluginSystem

    ps = PluginSystem(plugins_dir="plugins", data_dir=tempfile.mkdtemp())
    await ps.load_plugins()
    ch = CommandHandler(
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None, ws_server=None, plugin_system=ps,
    )
    cmds_001 = ch.collect_plugin_commands("bot_001")
    cmds_002 = ch.collect_plugin_commands("bot_002")
    assert "今日老婆" in cmds_001, "bot_001 应看到 wifepicker 命令"
    assert "今日老婆" not in cmds_002, "bot_002 不应看到 wifepicker 命令"
    assert "占卜" in cmds_002, "未绑定插件(bot_002)命令不受影响"
    # /help 分组同样过滤
    sections = ch._build_help_sections("bot_002")
    titles = [s["title"] for s in sections]
    assert not any("wifepicker" in t for t in titles), titles
    print("[+] 命令收集按 bot 过滤 OK")


# ── 2/3/4. relationship 欢迎消息 ────────────────────────────

class WelcomeWS:
    """mock ws_server: 记录私聊/群聊发送 + API 响应。"""

    def __init__(self):
        self.private = []
        self.group = []
        self._bot_manager = None

    async def send_private_msg(self, bot_id, user_id, message):
        self.private.append((bot_id, user_id, message))

    async def send_group_msg(self, bot_id, group_id, message):
        self.group.append((bot_id, group_id, message))

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        if action == "get_group_info":
            return {"status": "ok", "retcode": 0, "data": {"group_name": "测试群"}}
        if action == "get_group_member_list":
            return {"status": "ok", "retcode": 0, "data": [{"user_id": 1001}, {"user_id": 1002}, {"user_id": 1003}]}
        return {"status": "ok", "retcode": 0, "data": {}}


def make_cfg(ws, **extra):
    sys.path.insert(0, "plugins/relationship")
    from relationship_core.config import PluginConfig
    data = {
        "manage_group": "",
        "manage_users": [],
        "welcome_friend_enabled": True,
        "welcome_friend_msg": "你好，这里是【此处替换为 bot 的昵称】～\n欢迎~",
        "welcome_group_enabled": True,
        "welcome_group_msg": "你好，这里是【此处替换为 bot 的昵称】～\n欢迎进群~",
    }
    data.update(extra)
    return PluginConfig(data, admins=[1001], ws_server=ws, data_dir=tempfile.mkdtemp())


def make_handle(ws, **cfg_extra):
    sys.path.insert(0, "plugins/relationship")
    from relationship_core.notice.handle import NoticeHandle
    return NoticeHandle(make_cfg(ws, **cfg_extra))


async def test_friend_welcome():
    ws = WelcomeWS()
    handle = make_handle(ws)
    raw = {"post_type": "notice", "notice_type": "friend_add",
           "user_id": "12345", "self_id": "1000"}
    with mock.patch("relationship_core.notice.handle.random.uniform", return_value=0.01):
        await handle.handle("bot_001", None, raw)
    assert ws.private and ws.private[-1][1] == 12345
    text = ws.private[-1][2]
    assert "你好，这里是bot_001" in text, "昵称替换应回退 bot_id"
    assert "欢迎~" in text
    # 昵称替换(bot_manager 提供 nickname)
    from mohobot.bot_manager import BotManager, BotInstance
    from mohobot.models.config import BotConfig
    bm = BotManager(data_dir=tempfile.mkdtemp())
    bm._bots["bot_001"] = BotInstance("bot_001", None, BotConfig(qq=1000, nickname="天依"))
    ws._bot_manager = bm
    with mock.patch("relationship_core.notice.handle.random.uniform", return_value=0.01):
        await handle.handle("bot_001", None, raw)
    assert "你好，这里是天依" in ws.private[-1][2], ws.private[-1][2]

    # 新占位「xxx（bot昵称）」同样替换
    handle2 = make_handle(ws, welcome_friend_msg="这里是xxx（bot昵称）！\n介绍页: http://x")
    with mock.patch("relationship_core.notice.handle.random.uniform", return_value=0.01):
        await handle2.handle("bot_001", None, raw)
    assert "这里是天依！" in ws.private[-1][2], ws.private[-1][2]
    print("[+] 新好友欢迎 OK")


async def test_friend_welcome_disabled():
    ws = WelcomeWS()
    handle = make_handle(ws, welcome_friend_enabled=False)
    raw = {"post_type": "notice", "notice_type": "friend_add",
           "user_id": "12345", "self_id": "1000"}
    await handle.handle("bot_001", None, raw)
    assert not ws.private, "开关关闭不应发送"
    # 模板为空也不发送
    handle2 = make_handle(ws, welcome_friend_msg="")
    await handle2.handle("bot_001", None, raw)
    assert not ws.private
    print("[+] 开关/空模板关闭 OK")


async def test_group_welcome_only_when_stay():
    from relationship_core.notice.decision import NoticeDecision, NoticeResult

    ws = WelcomeWS()
    handle = make_handle(ws)
    raw = {"post_type": "notice", "notice_type": "group_increase", "sub_type": "invite",
           "group_id": "888", "user_id": "1000", "self_id": "1000", "operator_id": "666"}

    # 不退群 → 发欢迎
    with mock.patch.object(NoticeDecision, "decide",
                           new=mock.AsyncMock(return_value=NoticeResult(leave_group=False))), \
         mock.patch("relationship_core.notice.handle.random.uniform", return_value=0.01):
        await handle.handle("bot_001", None, raw)
    assert ws.group and ws.group[-1][1] == 888
    assert "欢迎进群~" in ws.group[-1][2]

    # 自动退群 → 不发欢迎
    ws.group.clear()
    with mock.patch.object(NoticeDecision, "decide",
                           new=mock.AsyncMock(return_value=NoticeResult(leave_group=True))):
        await handle.handle("bot_001", None, raw)
    assert not ws.group, "自动退群的场景不应发欢迎"
    print("[+] 新入群欢迎(仅不退群) OK")


async def _main() -> int:
    import asyncio
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if asyncio.iscoroutinefunction(fn):
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
    failed = asyncio.run(_main())
    total = len([n for n in globals() if n.startswith("test_") and callable(globals()[n])])
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
