"""私聊主动聊天引擎离线回归: 调度状态机、免打扰、上限、窗口丢弃与重启恢复。

不连网、不读真实配置: 引擎运行时服务全部用桩注入, 时间用可控假时钟。
测试函数为原生 async(_run_all.py 在事件循环内逐个 await)。
"""

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "proactive_chat"
sys.path.insert(0, str(_PLUGIN_DIR))

from mohobot.file_store import json_update
from mohobot.utils.time_utils import TZ_UTC8

from proactive_chat_core.engine import ProactiveEngine, session_key
from proactive_chat_core.schedule import (
    is_quiet_time,
    next_non_quiet_time,
    random_interval_seconds,
    render_prompt,
)


# ── 桩服务 ────────────────────────────────────────────────────

class _Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class _ContextManagerStub:
    def __init__(self):
        self.appended: list[dict] = []

    async def capture_session(self, bot_id, chat_type, chat_id):
        return {"id": "sess-1", "generation": "gen-1", "persona_id": ""}

    async def load_context(self, bot_id, chat_type, chat_id, *, session_id=None, generation=None):
        return [{"role": "user", "content": "你好", "timestamp": 1}]

    async def append_context(self, bot_id, chat_type, chat_id, entries, *, session_id=None, generation=None):
        self.appended.append({"bot_id": bot_id, "chat_id": chat_id, "entries": entries,
                              "session_id": session_id, "generation": generation})


class _PersonaStub:
    def resolve(self, bot_config, session_persona_id=""):
        return {"id": "p1", "name": "测试人设", "content": "人设正文", "source": "default"}


class _LlmStub:
    def __init__(self, reply="在吗？好久没聊了"):
        self.reply = reply
        self.calls: list[dict] = []
        self.on_call = None  # 可注入生成期间副作用(如模拟用户来信)
        self._chat_client = object()  # 引擎以 _chat_client 判定可用性

    async def chat(self, *, bot_id, event, context, raw_event, bot_config=None, persona_content=None):
        self.calls.append({"bot_id": bot_id, "event": event, "context": context,
                           "persona_content": persona_content})
        if self.on_call is not None:
            await self.on_call()
        return (self.reply, None)


class _BotManagerStub:
    def __init__(self):
        self.instance = SimpleNamespace(
            config=SimpleNamespace(persona_id="", chat_model_override="", nickname="测试Bot"),
            qq=111,
        )

    def get(self, bot_id):
        return self.instance


class _MessageHandlerStub:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail = False

    async def send_proactive_reply(self, bot_id, event, text):
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append({"bot_id": bot_id, "text": text})
        return text


def _make_engine(config: dict, state_path: Path, clock: _Clock,
                 llm: _LlmStub | None = None, mh: _MessageHandlerStub | None = None,
                 ctx: _ContextManagerStub | None = None):
    llm = llm or _LlmStub()
    mh = mh or _MessageHandlerStub()
    ctx = ctx or _ContextManagerStub()
    refs = {
        "bot_manager": _BotManagerStub(),
        "llm_service": llm,
        "message_handler": mh,
        "context_manager": ctx,
        "persona_service": _PersonaStub(),
        "ws_server": None,
    }
    engine = ProactiveEngine(
        state_path=state_path, config=config, get_ref=refs.get, clock=clock,
    )
    engine.plugin_start_time = clock.now
    return engine, llm, mh, ctx


_BASE_CONFIG = {
    "enable": True,
    "min_interval_minutes": 1,
    "max_interval_minutes": 1,
    "quiet_hours": "",
    "max_unanswered_times": 4,
    "proactive_prompt": "主动找他聊聊, 已未回复 {{unanswered_count}} 次, 现在 {{current_time}}",
    "exclude_users": [],
    "enable_auto_trigger": False,
    "auto_trigger_after_minutes": 5,
    "tick_interval_sec": 30,
}


async def _read_state(state_path: Path) -> dict:
    from mohobot.file_store import json_read
    state = await json_read(state_path)
    return state if isinstance(state, dict) else {}


# ── 纯函数 ────────────────────────────────────────────────────

def test_quiet_hours():
    from datetime import datetime
    # 同日区间
    assert is_quiet_time("1-7", datetime(2026, 10, 11, 3, 0)) is True
    assert is_quiet_time("1-7", datetime(2026, 10, 11, 7, 0)) is False  # 右开区间
    # 跨天区间
    assert is_quiet_time("23-7", datetime(2026, 10, 11, 23, 30)) is True
    assert is_quiet_time("23-7", datetime(2026, 10, 11, 2, 0)) is True
    assert is_quiet_time("23-7", datetime(2026, 10, 11, 12, 0)) is False
    # 非法配置按"非免打扰"处理
    assert is_quiet_time("", datetime(2026, 10, 11, 3, 0)) is False
    assert is_quiet_time("abc", datetime(2026, 10, 11, 3, 0)) is False


def test_random_interval_bounds():
    seconds = random_interval_seconds(2, 2)
    assert seconds == 120  # min=max 时窗口收敛
    for _ in range(20):
        seconds = random_interval_seconds(1, 5)
        assert 60 <= seconds <= 300


def test_render_prompt():
    text = render_prompt("未回复 {{unanswered_count}} 次, 时间 {{current_time}}", 3)
    assert "未回复 3 次" in text
    assert "{{" not in text and "}}" not in text


# ── 观察钩子: 登记/重置/排除 ─────────────────────────────────

async def test_observe_registers_and_schedules():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)

        await engine.on_private_message("bot_001", "123", "小明")
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 0
        assert entry["sender_name"] == "小明"
        assert entry["next_trigger_time"] == clock.now + 60  # min=max=1 分钟
        assert entry["last_message_time"] == clock.now

        # 再来一条: 计数保持 0, 窗口重排
        clock.now += 100
        await engine.on_private_message("bot_001", "123", "小明")
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 0
        assert entry["next_trigger_time"] == clock.now + 60


async def test_observe_resets_unanswered_counter():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        key = session_key("bot_001", "123")
        await engine._mutate_state(lambda state: state.setdefault("sessions", {}).update(
            {key: {"unanswered_count": 3, "next_trigger_time": 9999}}))

        clock.now += 10
        await engine.on_private_message("bot_001", "123", "小明")
        state = await _read_state(state_path)
        entry = state["sessions"][key]
        assert entry["unanswered_count"] == 0  # 用户回复归零
        assert entry["next_trigger_time"] == clock.now + 60  # 旧任务被重排


async def test_exclude_user():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        config = dict(_BASE_CONFIG, exclude_users=["123"])
        engine, llm, mh, ctx = _make_engine(config, state_path, clock)

        await engine.on_private_message("bot_001", "123", "小明")
        state = await _read_state(state_path)
        assert "bot_001:123" not in state.get("sessions", {})

        # 已跟踪会话进入排除名单后, 消息到达即移除
        await engine._mutate_state(_seed_entry("bot_001", "456"))
        engine.update_config({"exclude_users": ["456"]})
        await engine.on_private_message("bot_001", "456", "小红")
        state = await _read_state(state_path)
        assert "bot_001:456" not in state.get("sessions", {})


def _seed_entry(bot_id: str, user_id: str):
    def _update(state: dict) -> None:
        state.setdefault("sessions", {})[session_key(bot_id, user_id)] = {
            "unanswered_count": 0, "last_message_time": 1.0,
        }
    return _update


# ── tick 主流程 ───────────────────────────────────────────────

def _seed_due(bot_id: str, user_id: str, due_at: float, unanswered: int = 0):
    def _update(state: dict) -> None:
        state.setdefault("sessions", {})[session_key(bot_id, user_id)] = {
            "unanswered_count": unanswered,
            "last_message_time": 1.0,
            "sender_name": "小明",
            "last_scheduled_at": due_at - 60,
            "next_trigger_time": due_at,
        }
    return _update


async def test_tick_full_flow_success():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))

        await engine.tick()

        # 生成: 合成事件 + 人设 + 上下文
        assert len(llm.calls) == 1
        call = llm.calls[0]
        assert call["event"].user_id == 123
        assert call["event"].self_id == 111
        assert call["event"].message_type == "private"
        assert "已未回复 0 次" in call["event"].raw_message
        assert call["persona_content"] == "人设正文"
        assert call["context"][0]["content"] == "你好"
        # 发送与回写
        assert len(mh.sent) == 1 and mh.sent[0]["text"] == "在吗？好久没聊了"
        assert len(ctx.appended) == 1
        appended = ctx.appended[0]
        assert appended["session_id"] == "sess-1" and appended["generation"] == "gen-1"
        assert appended["entries"][0]["role"] == "assistant"
        assert appended["entries"][0]["content"] == "在吗？好久没聊了"
        # 计数与重排
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 1
        assert entry["next_trigger_time"] == clock.now + 60


async def test_tick_unanswered_cap_pauses():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now, unanswered=4))

        await engine.tick()

        assert llm.calls == [] and mh.sent == []
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 4  # 保持
        assert "next_trigger_time" not in entry  # 暂停: 待发任务清理


async def test_tick_quiet_hours_reschedules():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        # 引擎读 UTC+8 当前时间; 把免打扰区间设为覆盖任意时刻的窗口
        engine.update_config({"quiet_hours": "0-24"})

        await engine.tick()

        assert llm.calls == [] and mh.sent == []
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["next_trigger_time"] == clock.now + 60  # 跳过并重排


async def test_generation_window_discard():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        key = session_key("bot_001", "123")

        async def user_replies_mid_generation():
            # 模拟 LLM 生成期间用户来新消息(观察钩子会更新时间并重排)
            def _update(state: dict) -> None:
                entry = state["sessions"][key]
                entry["last_message_time"] = clock.now + 999
                entry["unanswered_count"] = 0
                entry["next_trigger_time"] = clock.now + 999 + 60
            await engine._mutate_state(_update)

        llm.on_call = user_replies_mid_generation
        await engine.tick()

        assert mh.sent == [] and ctx.appended == []  # 丢弃, 不发送不回写
        state = await _read_state(state_path)
        entry = state["sessions"][key]
        assert entry["unanswered_count"] == 0
        assert entry["next_trigger_time"] == clock.now + 999 + 60  # 保留观察钩子的重排


async def test_send_failure_reschedules_without_counting():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        mh.fail = True
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))

        await engine.tick()

        assert len(llm.calls) == 1 and mh.sent == [] and ctx.appended == []
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 0  # 未送达不计数
        assert entry["next_trigger_time"] == clock.now + 60  # 仅重排


async def test_empty_reply_reschedules_without_counting():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        llm.reply = "  "
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))

        await engine.tick()

        assert mh.sent == [] and ctx.appended == []
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 0
        assert entry["next_trigger_time"] == clock.now + 60


async def test_user_reply_during_send_keeps_observe_schedule():
    """发送期间用户回复: 消息已发出→补记上下文; 计数/重排以观察钩子为准不被覆盖。"""
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        key = session_key("bot_001", "123")

        async def user_replies_mid_send():
            # 模拟分段发送期间用户回复(观察钩子: 归零 + 重排)
            def _update(state: dict) -> None:
                entry = state["sessions"][key]
                entry["last_message_time"] = clock.now + 50
                entry["unanswered_count"] = 0
                entry["next_trigger_time"] = clock.now + 50 + 60
            await engine._mutate_state(_update)

        async def send_proactive_reply(bot_id, event, text):
            await user_replies_mid_send()
            mh.sent.append({"bot_id": bot_id, "text": text})
            return text

        mh.send_proactive_reply = send_proactive_reply
        await engine.tick()

        # 已发出的消息补记上下文
        assert len(mh.sent) == 1 and len(ctx.appended) == 1
        # 计数不 +1; 触发时间保留观察钩子写入的值
        state = await _read_state(state_path)
        entry = state["sessions"][key]
        assert entry["unanswered_count"] == 0
        assert entry["next_trigger_time"] == clock.now + 50 + 60


# ── LLM 自主决策(SKIP/PAUSE) ─────────────────────────────────

async def test_llm_choice_skip():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        llm.reply = "[SKIP]"

        await engine.tick()

        assert mh.sent == [] and ctx.appended == []  # 不发送不回写
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 0  # 拒绝不计数
        assert entry["next_trigger_time"] == clock.now + 60  # 正常重排


async def test_llm_choice_pause():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now, unanswered=2))
        llm.reply = "[PAUSE]"

        await engine.tick()

        assert mh.sent == [] and ctx.appended == []
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 2  # 保持
        assert "next_trigger_time" not in entry  # 暂停: 待发任务已消耗不再重排
        # 对方回复后恢复(观察钩子归零+重排)
        await engine.on_private_message("bot_001", "123", "回来了")
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 0
        assert entry["next_trigger_time"] == clock.now + 60


async def test_llm_choice_marker_variants():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        for variant in ("【SKIP】", "(skip)", "  skip  ", "PAUSE"):
            await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
            llm.reply = variant
            await engine.tick()
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert mh.sent == []
        # 末次为 PAUSE: 待发任务不再重排
        assert "next_trigger_time" not in entry


async def test_llm_choice_marker_with_content_sends_content():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        llm.reply = "[skip] 其实还是想说: 好久不见"

        await engine.tick()

        assert len(mh.sent) == 1 and mh.sent[0]["text"] == "其实还是想说: 好久不见"
        state = await _read_state(state_path)
        assert state["sessions"]["bot_001:123"]["unanswered_count"] == 1


async def test_llm_choice_disabled_treats_marker_as_text():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        engine.update_config({"enable_llm_choice": False})
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        llm.reply = "[SKIP]"

        await engine.tick()

        # 开关关闭: 无决策指令, 标记按普通文本原样发送
        assert len(mh.sent) == 1 and mh.sent[0]["text"] == "[SKIP]"


async def test_decision_guidance_in_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))

        await engine.tick()

        raw = llm.calls[0]["event"].raw_message
        assert "【发送前自主判断】" in raw and "[SKIP]" in raw and "[PAUSE]" in raw
        assert "已未回复 0 次" in raw  # 未回复状态注入决策依据

        # 关闭开关后不再附加
        engine.update_config({"enable_llm_choice": False})
        await engine._mutate_state(_seed_due("bot_001", "456", clock.now))
        await engine.tick()
        assert "【发送前自主判断】" not in llm.calls[-1]["event"].raw_message


# ── 启动恢复与冷启动自动触发 ─────────────────────────────────

async def test_startup_cleans_stale_and_keeps_fresh():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=10000.0)
        state_path = Path(tmp) / "state.json"

        def _seed(state: dict) -> None:
            sessions = state.setdefault("sessions", {})
            # 新鲜到期(容忍期内) → 保留
            sessions["bot_001:111"] = {"unanswered_count": 0, "next_trigger_time": 9990.0}
            # 停机期间过期 → 清理
            sessions["bot_001:222"] = {"unanswered_count": 2, "next_trigger_time": 5000.0}
            # 无任务 → 自动触发武装
            sessions["bot_001:333"] = {"unanswered_count": 0}

        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed)

        engine.update_config({"enable_auto_trigger": True, "auto_trigger_after_minutes": 5})
        await engine.on_startup()

        state = await _read_state(state_path)
        sessions = state["sessions"]
        assert sessions["bot_001:111"]["next_trigger_time"] == 9990.0
        assert "next_trigger_time" not in sessions["bot_001:222"]
        assert sessions["bot_001:333"]["auto_trigger_at"] == clock.now + 300


async def test_auto_trigger_converts_only_if_silent():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=10000.0)
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(
            dict(_BASE_CONFIG, enable_auto_trigger=True), state_path, clock,
        )
        engine.plugin_start_time = 10000.0

        await engine._mutate_state(lambda state: state.setdefault("sessions", {}).update({
            # 静默会话: 启动后无消息 → 到点转正
            session_key("bot_001", "111"): {"unanswered_count": 0, "auto_trigger_at": 10000.0},
            # 启动后用户来过消息 → 放弃
            session_key("bot_001", "222"): {
                "unanswered_count": 0, "auto_trigger_at": 10000.0,
                "last_message_time": 10050.0,
            },
        }))

        clock.now = 10060.0
        await engine.tick()

        state = await _read_state(state_path)
        sessions = state["sessions"]
        assert sessions["bot_001:111"]["next_trigger_time"] == clock.now + 60
        assert "auto_trigger_at" not in sessions["bot_001:111"]
        assert "next_trigger_time" not in sessions["bot_001:222"]
        assert "auto_trigger_at" not in sessions["bot_001:222"]


# ── 配置开关与 bot 离线 ───────────────────────────────────────

async def test_disable_clears_pending_tasks():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))

        engine.update_config({"enable": False})
        await engine.tick()  # 关闭后的首次 tick 清理待发任务

        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert "next_trigger_time" not in entry
        assert llm.calls == [] and mh.sent == []


async def test_offline_bot_postpones_instead_of_firing():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        # 到期远超离线顺延阈值; bot_manager 返回 None 模拟离线
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now - 100000))

        engine._get_ref = lambda name: None
        await engine.tick()

        assert mh.sent == [] and ctx.appended == []
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        # 顺延到 now + 300, 不发送
        assert entry["next_trigger_time"] == clock.now + 300


# ── 真实 PluginSystem 装载路径 ────────────────────────────────

async def test_plugin_loads_through_plugin_system():
    """插件经真实 PluginSystem 装载: schema 解析/注入/观察钩子/tick/热更新/关闭。"""
    import shutil

    from mohobot.interceptors.plugin_system import PluginSystem
    from mohobot.models.onebot import PrivateMessageEvent, Sender

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plugins_dir = tmp_path / "plugins" / "proactive_chat"
        plugins_dir.parent.mkdir()
        shutil.copytree(_PLUGIN_DIR, plugins_dir,
                        ignore=shutil.ignore_patterns("__pycache__"))
        ps = PluginSystem(plugins_dir=str(plugins_dir.parent), data_dir=str(tmp_path / "data"))

        class _StubBotManager:
            def get(self, bot_id):
                from mohobot.models.config import BotConfig
                cfg = BotConfig(bot_id="bot_001", qq=111, nickname="测试Bot")
                return SimpleNamespace(config=cfg, qq=111) if bot_id == "bot_001" else None

        class _StubCtx:
            async def capture_session(self, *a, **k):
                return {"id": "s", "generation": "g", "persona_id": ""}

            async def load_context(self, *a, **k):
                return []

            async def append_context(self, *a, **k):
                pass

        class _StubLlm:
            _chat_client = object()

            async def chat(self, **kwargs):
                return ("主动问候测试", None)

        class _StubPersona:
            def resolve(self, bot_config, pid=""):
                return {"content": "x"}

        sent = []

        class _StubMH:
            async def send_proactive_reply(self, bot_id, event, text):
                sent.append((bot_id, text))
                return text

        ps.set_runtime_refs(
            bot_manager=_StubBotManager(),
            llm_service=_StubLlm(),
            context_manager=_StubCtx(),
            persona_service=_StubPersona(),
            message_handler=_StubMH(),
        )
        count = await ps.load_plugins()
        assert count == 1
        meta = ps._plugins[0]
        assert meta["name"] == "proactive_chat" and meta["enabled"]
        schema = meta.get("config_schema") or {}
        assert "proactive_prompt" in schema and "quiet_hours" in schema
        inst = meta["instance"]
        # schema 默认值注入
        assert inst.plugin_config["min_interval_minutes"] == 30
        assert inst.plugin_config["max_unanswered_times"] == 4
        assert inst.plugin_config["tick_interval_sec"] == 30

        await ps.startup_plugins()

        # 观察钩子: 私聊消息登记但不消费
        event = PrivateMessageEvent(
            time=1, self_id=111, post_type="message", message_type="private",
            user_id=123, message=[{"type": "text", "data": {"text": "hi"}}],
            raw_message="hi", sender=Sender(user_id=123, nickname="小明"),
        )
        handled, response = await ps.dispatch_observed("bot_001", event, {"raw": True})
        assert handled is False and response is None
        state = await _read_state(tmp_path / "data" / "proactive_chat" / "state.json")
        entry = state["sessions"]["bot_001:123"]
        assert entry["sender_name"] == "小明"
        assert entry["next_trigger_time"] > 0

        # 周期接口与配置热更新(interval_sec 由 on_tick 从配置刷新)
        await inst.on_tick()
        assert inst.interval_sec >= 5
        inst.plugin_config = dict(inst.plugin_config, tick_interval_sec=60)
        inst.on_config_update()
        await inst.on_tick()
        assert inst.interval_sec == 60

        # 端到端: 强制到期 → 走真实注入链路应完成生成与发送
        # (回归: 引擎引用名与插件类属性曾因下划线前缀错位, 到期会话被静默顺延)
        def _force_due(state):
            for e in (state.get("sessions") or {}).values():
                e["next_trigger_time"] = time.time() - 400
            return state

        await json_update(tmp_path / "data" / "proactive_chat" / "state.json", _force_due)
        inst.plugin_config = dict(inst.plugin_config, enable=True)
        inst.on_config_update()
        await inst.on_tick()
        assert sent and sent[0][0] == "bot_001" and sent[0][1] == "主动问候测试"
        state = await _read_state(tmp_path / "data" / "proactive_chat" / "state.json")
        assert state["sessions"]["bot_001:123"]["unanswered_count"] == 1

        await ps.shutdown_plugins()


# ── LLM 自主排期([SCHEDULE]) ────────────────────────────────

def _ts(y: int, mo: int, d: int, h: int, mi: int) -> float:
    from datetime import datetime
    return datetime(y, mo, d, h, mi, tzinfo=TZ_UTC8).timestamp()


def test_next_non_quiet_time_helper():
    from datetime import datetime
    # 03:20 处于 1-7 免打扰 → 07:00
    assert next_non_quiet_time("1-7", datetime(2026, 10, 31, 3, 20)) == datetime(2026, 10, 31, 7, 0)
    # 跨天 23-7: 01:10 → 07:00
    assert next_non_quiet_time("23-7", datetime(2026, 10, 31, 1, 10)) == datetime(2026, 10, 31, 7, 0)
    # 非免打扰时段原样返回
    noon = datetime(2026, 10, 31, 12, 0)
    assert next_non_quiet_time("1-7", noon) == noon
    # 非法配置原样返回
    assert next_non_quiet_time("bad", noon) == noon


def test_render_prompt_schedule_placeholder():
    text = render_prompt("最多 {{max_schedule_days}} 天, 未回复 {{unanswered_count}} 次", 2,
                         max_schedule_days=5)
    assert "最多 5 天" in text and "未回复 2 次" in text
    assert "{{" not in text and "}}" not in text


async def test_schedule_only_defers_without_sending():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        target = _ts(2026, 10, 31, 18, 0)
        llm.reply = "[SCHEDULE: 2026-10-31 18:00]"

        await engine.tick()

        assert mh.sent == [] and ctx.appended == []
        state = await _read_state(state_path)
        entry = state["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 0  # 只排期, 不计数
        assert entry["next_trigger_time"] == target


async def test_schedule_with_content_sends_then_defers():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        target = _ts(2026, 10, 31, 18, 0)
        llm.reply = "晚安呀\n[SCHEDULE: 2026-10-31 18:00]"

        await engine.tick()

        assert len(mh.sent) == 1 and mh.sent[0]["text"] == "晚安呀"
        assert ctx.appended[0]["entries"][0]["content"] == "晚安呀"
        entry = (await _read_state(state_path))["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 1
        assert entry["next_trigger_time"] == target


async def test_schedule_invalid_or_far_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        for bad in ("[SCHEDULE: 2020-01-01 00:00]", "[SCHEDULE: 2027-12-31 18:00]"):
            await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
            llm.reply = f"在吗 {bad}"
            await engine.tick()

        # 非法标记被剥掉但仍按正文发送, 回落到随机窗口
        assert mh.sent and mh.sent[-1]["text"] == "在吗"
        entry = (await _read_state(state_path))["sessions"]["bot_001:123"]
        assert entry["unanswered_count"] == 1
        assert entry["next_trigger_time"] == clock.now + 60


async def test_schedule_guidance_and_toggle():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        llm.reply = "在吗"

        await engine.tick()

        raw = llm.calls[0]["event"].raw_message
        assert "【可选的延后发送】" in raw and "[SCHEDULE:" in raw
        assert "2 条" in raw and "7 天" in raw and "当前时间是" in raw

        # 关闭排期开关: 不再附加指令, 标记按普通文本发送
        engine.update_config({"enable_llm_schedule": False})
        await engine._mutate_state(_seed_due("bot_001", "456", clock.now))
        llm.reply = "[SCHEDULE: 2026-10-31 18:00]"
        await engine.tick()

        assert mh.sent[-1]["text"] == "[SCHEDULE: 2026-10-31 18:00]"
        last_raw = llm.calls[-1]["event"].raw_message
        assert "【可选的延后发送】" not in last_raw
        # “单任务≤2条”是独立的提示词参考, 不随排期开关消失。
        assert "【主动消息节奏】" in last_raw and "2 条" in last_raw


async def test_schedule_marker_variants():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        target = _ts(2026, 10, 31, 18, 0)
        for variant in ("【SCHEDULE：2026-10-31 18:00】", "(schedule: 2026-10-31 18:00)"):
            await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
            llm.reply = variant
            await engine.tick()

        assert mh.sent == []
        entry = (await _read_state(state_path))["sessions"]["bot_001:123"]
        assert entry["next_trigger_time"] == target


async def test_schedule_discarded_when_user_replies_mid_generation():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        key = session_key("bot_001", "123")
        llm.reply = "[SCHEDULE: 2026-10-31 18:00]"

        async def user_replies_mid_generation():
            def _update(state: dict) -> None:
                entry = state["sessions"][key]
                entry["last_message_time"] = clock.now + 999
                entry["unanswered_count"] = 0
                entry["next_trigger_time"] = clock.now + 999 + 60
            await engine._mutate_state(_update)

        llm.on_call = user_replies_mid_generation
        await engine.tick()

        assert mh.sent == [] and ctx.appended == []
        entry = (await _read_state(state_path))["sessions"][key]
        # 用户已在对话: 丢弃排期意图, 保留观察钩子的重排
        assert entry["next_trigger_time"] == clock.now + 999 + 60


async def test_schedule_marker_with_note_strips_whole_marker():
    """标记内允许补充说明; 不能残留 ']' 当正文, 也不能丢排期。"""
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        target = _ts(2026, 10, 31, 18, 0)
        llm.reply = "[SCHEDULE: 2026-10-31 18:00（明天傍晚）]"

        await engine.tick()

        assert mh.sent == [] and ctx.appended == []
        entry = (await _read_state(state_path))["sessions"]["bot_001:123"]
        assert entry["next_trigger_time"] == target


async def test_schedule_multiple_markers_uses_first_valid():
    """最后一个标记写坏时, 前面的合法排期不能被一起丢掉。"""
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        target = _ts(2026, 10, 31, 18, 0)
        llm.reply = "晚安 [SCHEDULE: 2026-10-31 18:00] [SCHEDULE: 明天傍晚]"

        await engine.tick()

        assert len(mh.sent) == 1 and mh.sent[0]["text"] == "晚安"
        entry = (await _read_state(state_path))["sessions"]["bot_001:123"]
        assert entry["next_trigger_time"] == target


async def test_schedule_max_days_uses_calendar_days():
    """max_schedule_days 按日历天判断: 7 天后同日时刻+1h 仍应允许。"""
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        engine, llm, mh, ctx = _make_engine(dict(_BASE_CONFIG), state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        target = _ts(2026, 11, 6, 13, 0)  # 日历差 7 天, 滚动差 7d1h
        llm.reply = "[SCHEDULE: 2026-11-06 13:00]"

        await engine.tick()

        assert mh.sent == []
        entry = (await _read_state(state_path))["sessions"]["bot_001:123"]
        assert entry["next_trigger_time"] == target


def test_schedule_rejects_all_day_quiet():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        engine, _llm, _mh, _ctx = _make_engine(
            dict(_BASE_CONFIG), Path(tmp) / "state.json", clock,
        )
        cfg = {"quiet_hours": "0-24", "max_schedule_days": 7}
        assert engine._resolve_schedule("2026-10-31 18:00", cfg, clock.now) is None


async def test_custom_prompt_renders_schedule_placeholders():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(start=_ts(2026, 10, 30, 12, 0))
        state_path = Path(tmp) / "state.json"
        config = dict(_BASE_CONFIG, proactive_prompt="最多 {{max_schedule_days}} 天, 现在 {{current_time}}")
        engine, llm, mh, ctx = _make_engine(config, state_path, clock)
        await engine._mutate_state(_seed_due("bot_001", "123", clock.now))
        llm.reply = "在吗"

        await engine.tick()

        raw = llm.calls[0]["event"].raw_message
        assert "最多 7 天" in raw
        assert "{{" not in raw and "}}" not in raw
