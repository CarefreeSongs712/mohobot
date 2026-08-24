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
    """mock ws_server: 群/好友列表可配置, 记录发送。"""

    def __init__(self):
        self.private = []
        self.group = []
        self._bot_manager = None
        self.groups = [{"group_id": 100}]      # 当前群列表
        self.friends = [{"user_id": 1001}]     # 当前好友列表

    async def send_private_msg(self, bot_id, user_id, message):
        self.private.append((bot_id, user_id, message))

    async def send_group_msg(self, bot_id, group_id, message):
        self.group.append((bot_id, group_id, message))

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": list(self.groups)}
        if action == "get_friend_list":
            return {"status": "ok", "retcode": 0, "data": list(self.friends)}
        return {"status": "ok", "retcode": 0, "data": {}}


def make_handle(ws, data_dir, **cfg_extra):
    """welcome 插件实例(独立欢迎插件, 监控模式)。"""
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("welcome_plugin_main", "plugins/welcome/main.py")
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    inst = _mod.Plugin()
    inst._ws_server = ws
    inst._data_dir = data_dir
    data = {
        "welcome_friend_enabled": True,
        "welcome_friend_msg": "你好，这里是【此处替换为 bot 的昵称】～\n欢迎~",
        "welcome_group_enabled": True,
        "welcome_group_msg": "你好，这里是【此处替换为 bot 的昵称】～\n欢迎进群~",
        "delay_min": 0,
        "delay_max": 0,
        "check_every_heartbeats": 1,
    }
    data.update(cfg_extra)
    inst.plugin_config = data
    return inst


def heartbeat():
    return {"post_type": "meta_event", "meta_event_type": "heartbeat"}


async def test_first_boot_baseline():
    """首启: 当前列表为基线, 不欢迎已有群/好友。"""
    td = tempfile.mkdtemp()
    ws = WelcomeWS()
    handle = make_handle(ws, td)
    await handle.on_meta("bot_001", None, heartbeat())
    assert not ws.group and not ws.private, "首启不应欢迎已有"
    # 基线已持久化
    from mohobot.file_store import json_read
    known = await json_read(Path(td) / "plugins_data" / "welcome" / "known.json")
    assert known["groups"]["bot_001"] == ["100"]
    assert known["friends"]["bot_001"] == ["1001"]
    print("[+] 首启基线 OK")


async def test_new_group_and_friend_welcome():
    td = tempfile.mkdtemp()
    ws = WelcomeWS()
    handle = make_handle(ws, td)
    # 首启基线
    await handle.on_meta("bot_001", None, heartbeat())
    # 新群 + 新好友
    ws.groups = [{"group_id": 100}, {"group_id": 200}]
    ws.friends = [{"user_id": 1001}, {"user_id": 2002}]
    await handle.on_meta("bot_001", None, heartbeat())
    assert ws.group and ws.group[-1][1] == 200
    assert "欢迎进群~" in ws.group[-1][2]
    assert ws.private and ws.private[-1][1] == 2002
    assert "你好，这里是bot_001" in ws.private[-1][2], "昵称回退 bot_id"
    # 基线更新
    from mohobot.file_store import json_read
    known = await json_read(Path(td) / "plugins_data" / "welcome" / "known.json")
    assert known["groups"]["bot_001"] == ["100", "200"]
    assert known["friends"]["bot_001"] == ["1001", "2002"]
    # 再次检查无变化 → 不重复欢迎
    ws.private.clear()
    ws.group.clear()
    await handle.on_meta("bot_001", None, heartbeat())
    assert not ws.group and not ws.private, "无新增不应重复欢迎"
    print("[+] 新增群/好友欢迎 OK")


async def test_friend_welcome_disabled():
    td = tempfile.mkdtemp()
    ws = WelcomeWS()
    handle = make_handle(ws, td, welcome_friend_enabled=False)
    await handle.on_meta("bot_001", None, heartbeat())
    ws.friends = [{"user_id": 1001}, {"user_id": 2002}]
    ws.groups = [{"group_id": 100}, {"group_id": 200}]
    await handle.on_meta("bot_001", None, heartbeat())
    assert not ws.private, "好友开关关闭不应发送"
    assert ws.group and ws.group[-1][1] == 200, "群开关仍生效"
    print("[+] 开关关闭 OK")


async def test_low_frequency_check():
    td = tempfile.mkdtemp()
    ws = WelcomeWS()
    handle = make_handle(ws, td, check_every_heartbeats=2)
    # 第 1 次心跳: 不检查(不建基线)
    await handle.on_meta("bot_001", None, heartbeat())
    ws.groups = [{"group_id": 100}, {"group_id": 200}]
    # 第 2 次心跳: 检查 → 首启基线(此时 200 已存在 → 作为基线不欢迎)
    await handle.on_meta("bot_001", None, heartbeat())
    assert not ws.group, "第 2 次心跳是首启基线, 不应欢迎"
    # 第 3 次心跳: 不检查; 第 4 次: 检查 → 发现新群 300
    ws.groups.append({"group_id": 300})
    await handle.on_meta("bot_001", None, heartbeat())
    assert not ws.group, "奇数心跳不应检查"
    await handle.on_meta("bot_001", None, heartbeat())
    assert ws.group and ws.group[-1][1] == 300
    print("[+] 降低频率 OK")


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
