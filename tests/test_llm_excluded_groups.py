"""LLM 排除群(llm_excluded_groups)回归测试。

覆盖:
1. 配置解析: 宽松解析(逗号分隔字符串/非法项) + save/load 往返
2. _group_llm_excluded 判定: 命中/未命中/无配置/非法群号
3. 消息管线端到端(_handle_message):
   - 排除群内 @bot 消息被静默跳过(_stream_llm_reply 不调用、不写上下文)
   - 排除群内 ping 不回 PONG
   - 未列入名单的群照常走 LLM
   - 排除群内插件命令在拦截器链正常处理后, LLM 链路被拦
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import GlobalConfig, _int_list
from mohobot.message_handler import MessageHandler
from mohobot.models.onebot import GroupMessageEvent, Sender


def test_int_list_parsing():
    assert _int_list([1127440436, "123"]) == [1127440436, 123]
    assert _int_list("1127440436,456") == [1127440436, 456]
    assert _int_list(["abc", "", None]) == []
    assert _int_list(None) == []
    assert _int_list(123) == []
    print("PASS test_int_list_parsing")


def test_config_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "global.yaml"
        cfg = GlobalConfig(llm_excluded_groups=[1127440436, 999])
        cfg.save(str(path))
        loaded = GlobalConfig.load(str(path))
        assert loaded.llm_excluded_groups == [1127440436, 999]
        # 缺省为空列表
        default_cfg = GlobalConfig.load(str(Path(td) / "missing.yaml"))
        assert default_cfg.llm_excluded_groups == []
    print("PASS test_config_roundtrip")


def test_group_llm_excluded_predicate():
    def handler_with(config):
        h = MessageHandler.__new__(MessageHandler)
        h._global_config = config
        return h

    h = handler_with(GlobalConfig(llm_excluded_groups=[1127440436]))
    assert h._group_llm_excluded(1127440436) is True
    assert h._group_llm_excluded("1127440436") is True, "字符串群号也能命中"
    assert h._group_llm_excluded(123) is False

    h = handler_with(GlobalConfig(llm_excluded_groups=[]))
    assert h._group_llm_excluded(1127440436) is False

    h = handler_with(None)
    assert h._group_llm_excluded(1127440436) is False

    h = handler_with(GlobalConfig(llm_excluded_groups=[1]))
    assert h._group_llm_excluded(None) is False, "非法群号不崩溃"
    print("PASS test_group_llm_excluded_predicate")


class _FakePluginSystem:
    """插件系统桩: intercept(/插件命令 → 回复) + 全部钩子放行。"""

    async def intercept(self, bot_id, event, raw):
        text = "".join(
            seg.get("data", {}).get("text", "")
            for seg in (event.message or [])
            if isinstance(seg, dict) and seg.get("type") == "text"
        ).strip()
        if text == "/插件命令":
            return (True, "插件回复")
        return (False, None)

    async def dispatch_observed(self, bot_id, event, raw):
        return (False, None)

    async def collect_perception(self, bot_id, event, raw):
        return ""

    async def dispatch_request(self, bot_id, event, raw):
        return False

    async def dispatch_meta(self, bot_id, event, raw):
        return None


class _RecordingWS:
    """记录 send 调用的 WSServer 桩; 带 bot_manager 让 gate 的 @mention 生效。"""

    def __init__(self):
        self.sent = []
        self._bot_manager = _FakeBotManager()

    async def send_group_msg(self, bot_id, group_id, message, **kw):
        self.sent.append(("group", group_id, message))

    async def send_private_msg(self, bot_id, user_id, message, **kw):
        self.sent.append(("private", user_id, message))


class _FakeBotManager:
    """最小 bot 管理器桩: get() 返回 bot_001 → QQ 1000 的实例。"""

    def note_group_message(self, bot_id, group_id):
        return None

    def get(self, bot_id):
        if bot_id == "bot_001":
            from types import SimpleNamespace
            return SimpleNamespace(qq=1000, bot_id="bot_001")
        return None


class _RecordingLLM:
    """LLM 桩: 被调用即计数, yield 一段回复。"""

    def __init__(self):
        self.call_count = 0

    def chat_stream(self, *args, **kwargs):
        self.call_count += 1
        return self._gen()

    async def _gen(self):
        yield ("桩回复", True)


def _group_event(group_id, text, message_id=1, extra_segs=None):
    segs = extra_segs or []
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=message_id, user_id=2001, group_id=group_id,
        sender=Sender(user_id=2001, nickname="测试用户"),
        message=segs + [{"type": "text", "data": {"text": text}}],
    )


class _FakeCtxMgr:
    """上下文桩: append 记录调用。"""

    def __init__(self):
        self.appended = []

    async def load_context(self, bot_id, chat_type, chat_id):
        return []

    async def append_context(self, bot_id, chat_type, chat_id, msgs):
        self.appended.append((bot_id, chat_type, chat_id, msgs))


def _handler(config, ws, llm, ctx):
    h = MessageHandler.__new__(MessageHandler)
    h._global_config = config
    plugins = _FakePluginSystem()
    h._interceptors = [plugins]
    h._plugins = plugins
    h._llm = llm
    h._ctx_mgr = ctx
    h._ws = ws
    # __new__ 绕过构造后, 手工绑齐管线用到的最小属性集
    h._group_recent_count = 0
    h._perception_text = {}
    h._group_recent_msgs = {}
    h._group_mid_window = {}
    h._writer_registry = {}
    h._quote_cache = {}
    h._repeat_state = {}
    h._last_image_time = {}
    h._data_dir = "."
    h._emotion = None
    h._db = None
    h._image_cache = None
    h._command_handler = None
    h._forward_min_len = 600      # 短回复不走合并转发
    h._forward_chunk_chars = 1000
    return h


async def test_excluded_group_silences_llm():
    """排除群内 @bot 消息: gate 放行 → 拦截点命中 → LLM 不调用、上下文不写。"""
    config = GlobalConfig(llm_excluded_groups=[1127440436])
    ctx = _FakeCtxMgr()
    llm = _RecordingLLM()
    ws = _RecordingWS()
    h = _handler(config, ws, llm, ctx)

    ev = _group_event(1127440436, "你好", extra_segs=[{"type": "at", "data": {"qq": "1000"}}])
    await h._handle_message("bot_001", ev, {})

    assert llm.call_count == 0, "排除群内 LLM 不应被调用"
    assert ctx.appended == [], "排陔回复不应写上下文"
    assert ws.sent == [], "不应发送任何消息"
    print("PASS test_excluded_group_silences_llm")


async def test_excluded_group_ping_silent():
    """排除群内 ping 不回 PONG。"""
    config = GlobalConfig(llm_excluded_groups=[1127440436])
    ctx = _FakeCtxMgr()
    llm = _RecordingLLM()
    ws = _RecordingWS()
    h = _handler(config, ws, llm, ctx)

    ev = _group_event(1127440436, "ping")
    await h._handle_message("bot_001", ev, {})

    assert ws.sent == [], "排除群内 ping 不应回 PONG"
    assert llm.call_count == 0
    print("PASS test_excluded_group_ping_silent")


async def test_non_excluded_group_uses_llm():
    """未列入名单的群 @bot: 照常走 LLM 并写上下文。"""
    config = GlobalConfig(llm_excluded_groups=[1127440436])
    ctx = _FakeCtxMgr()
    llm = _RecordingLLM()
    ws = _RecordingWS()
    h = _handler(config, ws, llm, ctx)

    calls = []

    async def _stub_stream(bot_id, event, context, raw):
        calls.append((bot_id, event.group_id))
        await h._send_reply(bot_id, event, "桩回复")
        return "桩回复"

    h._stream_llm_reply = _stub_stream

    ev = _group_event(777, "你好", extra_segs=[{"type": "at", "data": {"qq": "1000"}}])
    await h._handle_message("bot_001", ev, {})

    assert len(calls) == 1, "未排除群照常进入 LLM 链路"
    assert len(ctx.appended) == 1, "完成轮次应写上下文"
    assert ws.sent, "应有回复发往群"
    print("PASS test_non_excluded_group_uses_llm")


async def test_excluded_group_plugin_command_still_works():
    """排除群内插件命令: 拦截器链正常消费, 不受 LLM 排除影响。"""
    config = GlobalConfig(llm_excluded_groups=[1127440436])
    ctx = _FakeCtxMgr()
    llm = _RecordingLLM()
    ws = _RecordingWS()
    h = _handler(config, ws, llm, ctx)

    ev = _group_event(1127440436, "/插件命令")
    await h._handle_message("bot_001", ev, {})

    assert llm.call_count == 0, "插件命令消费后不走 LLM"
    assert ("group", 1127440436, "插件回复") in ws.sent, "插件回复应正常发出"
    print("PASS test_excluded_group_plugin_command_still_works")


async def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if asyncio.iscoroutinefunction(t):
            await t()
        else:
            t()
    print("\nALL LLM EXCLUDED GROUPS TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
