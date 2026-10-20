"""Issue #9 回归: 插件/主链的策略消费(禁用开关、离群剔除、封禁、回执)。

覆盖:
1. `proactive_chat.on_config_update(config)` 签名匹配框架调用, 不再 TypeError;
2. 主动聊天: 排队期间被封禁的用户, 到期后不生成/不发送;
3. `BotConfig.enabled=false` 的 bot 不再处理事件;
4. bot 离群后从群在线集合剔除, 全局指令抽签不再选中它;
5. 表情包发送在 bot 离线时回报失败(不误计成功)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "plugins" / "proactive_chat"))
sys.path.insert(0, str(_REPO / "plugins" / "meme_stealer"))


# ── 1. on_config_update 签名 ────────────────────────────────

def test_proactive_on_config_update_accepts_config_argument():
    """框架以 updater(merged) 调用; 旧签名不带参数会 TypeError。"""
    import importlib.util

    path = _REPO / "plugins" / "proactive_chat" / "main.py"
    spec = importlib.util.spec_from_file_location("proactive_plugin_main", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["proactive_plugin_main"] = module
    spec.loader.exec_module(module)

    plugin = module.Plugin.__new__(module.Plugin)
    plugin.plugin_config = {"enable": True, "max_unanswered_times": 4}
    captured: dict = {}

    class _Engine:
        def update_config(self, cfg):
            captured.update(cfg)

    plugin.engine = _Engine()
    # 框架实际调用形态: 传入合并后的配置 dict
    plugin.on_config_update({"enable": False, "max_unanswered_times": 9})
    assert captured["enable"] is False
    assert captured["max_unanswered_times"] == 9

    # 兼容无参调用(旧调用点)
    plugin.on_config_update()
    assert captured["enable"] is False


# ── 2. 主动聊天封禁检查 ─────────────────────────────────────

async def test_proactive_skips_banned_user():
    """排队期间被封禁: 到期**不生成也不发送**, 并清理该会话。"""
    from proactive_chat_core.engine import ProactiveEngine

    generated: list = []
    state: dict = {"sessions": {"bot_001:123": {"next_trigger_time": 0, "unanswered_count": 0}}}

    class _BanStore:
        @staticmethod
        async def is_banned(key, target):
            return (key == "private:123", "test")

    async def _fake_generate(*args, **kwargs):
        generated.append(args)
        return "主动消息"

    engine = ProactiveEngine(
        state_path=Path("/tmp/unused-proactive-state.json"),
        config={"enable": True},
        get_ref=lambda name: _BanStore() if name == "ban_store" else None,
    )
    engine._read_sessions = _read_state(state)  # type: ignore[assignment]
    engine._mutate_state = _mutate_state(state)  # type: ignore[assignment]
    engine._generate = _fake_generate  # type: ignore[attr-defined]

    # 走真实入口: 封禁用户必须被守卫拦下, 不进入生成阶段
    await engine._run_session("bot_001:123", {"enable": True}, 0.0)
    assert generated == [], "被封禁用户不得生成主动消息"
    assert "bot_001:123" not in state["sessions"], "被封禁会话应被清理"

    # 未封禁用户: 允许继续(生成函数被调用或至少不因守卫提前返回)
    assert await engine._is_banned_key("bot_001:456") is False


def _read_state(state: dict):
    async def _inner():
        return dict(state.get("sessions") or {})
    return _inner


def _mutate_state(state: dict):
    async def _inner(fn):
        fn(state)
        return None
    return _inner


async def test_proactive_without_ban_store_continues():
    """未注入 BanStore 时不阻断(降级)。"""
    from proactive_chat_core.engine import ProactiveEngine

    engine = ProactiveEngine(
        state_path=Path("/tmp/unused-proactive-state.json"),
        config={"enable": True},
        get_ref=lambda name: None,
    )
    assert await engine._is_banned_key("bot_001:123") is False


# ── 3. BotConfig.enabled 消费 ───────────────────────────────

async def test_disabled_bot_events_ignored():
    """enabled=false 的 bot: 事件不派发(连接保持)。"""
    from mohobot.ws_server import WSServer

    ws = object.__new__(WSServer)
    ws._task_supervisor = None
    ws._on_event = None
    ws._bot_manager = SimpleNamespace(
        handle_api_response=_noop, note_group_left=_noop_sync,
    )
    ws._history_barrier = None

    class _Instance:
        bound = True
        bot_id = "bot_001"
        qq = 111
        config = SimpleNamespace(enabled=False)

    # 只要不抛异常且不进入事件派发即可; 用 _on_event 记录是否被调用
    called: list = []

    async def _on_event(*args):
        called.append(args)

    ws._on_event = _on_event
    await ws._dispatch(_Instance(), {
        "post_type": "message", "message_type": "private",
        "message_id": 1, "user_id": 10001,
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "sender": {"user_id": 10001, "nickname": "u"},
    })
    await asyncio.sleep(0)
    assert called == [], "禁用 bot 的事件不得派发"

    # enabled=True 时正常派发
    class _Enabled(_Instance):
        config = SimpleNamespace(enabled=True)

    await ws._dispatch(_Enabled(), {
        "post_type": "message", "message_type": "private",
        "message_id": 2, "user_id": 10001,
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "sender": {"user_id": 10001, "nickname": "u"},
    })
    await asyncio.sleep(0.05)
    assert called, "启用 bot 的事件应正常派发"


async def _noop(*args, **kwargs):
    return None


def _noop_sync(*args, **kwargs):
    return None


# ── 4. 离群剔除 ─────────────────────────────────────────────

def test_note_group_left_removes_bot_from_group_set():
    from mohobot.bot_manager import BotManager

    bm = BotManager.__new__(BotManager)
    bm._group_bots = {"20002": {"bot_001", "bot_002"}}
    bm._group_pick_cache = {("20002", "msg1"): ("bot_001", 0.0)}

    bm.note_group_left("bot_001", 20002)
    assert bm._group_bots["20002"] == {"bot_002"}
    assert ("20002", "msg1") not in bm._group_pick_cache, "抽签缓存必须失效"

    # 最后一个 bot 也离开 → 群集合整体移除
    bm.note_group_left("bot_002", 20002)
    assert "20002" not in bm._group_bots


async def test_group_decrease_notice_removes_bot():
    """ws_server 收到 group_decrease notice 时调用 note_group_left。"""
    from mohobot.ws_server import WSServer

    calls: list = []

    class _Bm:
        @staticmethod
        async def handle_api_response(*a):
            return None

        @staticmethod
        def note_group_left(bot_id, group_id):
            calls.append((bot_id, group_id))

    ws = object.__new__(WSServer)
    ws._task_supervisor = None
    ws._on_event = None
    ws._bot_manager = _Bm()
    ws._history_barrier = None

    class _Instance:
        bound = True
        bot_id = "bot_001"
        qq = 111
        config = SimpleNamespace(enabled=True)

    await ws._dispatch(_Instance(), {
        "post_type": "notice", "notice_type": "group_decrease",
        "sub_type": "kick_me", "group_id": 20002, "user_id": 111,
    })
    assert calls == [("bot_001", 20002)], calls


# ── 5. 表情包发送回执 ───────────────────────────────────────

async def test_meme_bridge_reports_failure_when_bot_offline():
    """bot 离线时 send_segments 必须返回 False(不误计成功)。"""
    import importlib.util

    path = _REPO / "plugins" / "meme_stealer" / "main.py"
    spec = importlib.util.spec_from_file_location("meme_plugin_main", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["meme_plugin_main"] = module
    spec.loader.exec_module(module)

    attempted: list = []

    class _Ws:
        _bot_manager = SimpleNamespace(get=lambda bot_id: None)  # bot 不在线

        @staticmethod
        async def send_group_msg(*args, **kwargs):
            attempted.append(args)  # 记录是否真的尝试发送

    module.Plugin._ws_server = _Ws()
    bridge = module._MohobotBridge()
    ok = await bridge.send_segments("bot_001", "group", "20002", [])
    assert ok is False, "离线发送必须回报失败"
    assert attempted == [], "离线时根本不应调用发送接口(否则可能被静默丢弃却报成功)"


async def test_meme_bridge_reports_success_when_online():
    import importlib.util

    path = _REPO / "plugins" / "meme_stealer" / "main.py"
    spec = importlib.util.spec_from_file_location("meme_plugin_main2", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["meme_plugin_main2"] = module
    spec.loader.exec_module(module)

    sent: list = []

    class _Ws:
        _bot_manager = SimpleNamespace(get=lambda bot_id: object())

        @staticmethod
        async def send_group_msg(bot_id, chat_id, segments):
            sent.append((bot_id, chat_id))

    module.Plugin._ws_server = _Ws()
    bridge = module._MohobotBridge()
    ok = await bridge.send_segments("bot_001", "group", "20002", [])
    assert ok is True and sent == [("bot_001", "20002")]
