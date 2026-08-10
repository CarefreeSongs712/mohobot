"""四个新功能测试:
1. praise 插件配置化: 自定义点赞总数/每次点数/成功/失败/上限消息模板
2. 群聊最近消息: 缓冲记录 + 格式化 + 满 N 淘汰
3. 注入: agent 路径(_agent_context_provider) 与 legacy 路径(context 附加 system 段)
4. 配置关闭(0)时不记录/不注入
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.context_manager import ContextManager
from mohobot.message_handler import MessageHandler
from mohobot.models.config import ReplyConfig
from mohobot.models.onebot import GroupMessageEvent, Sender


def make_group_event(user_id, text, group_id=888888, time=1000000, card="张三"):
    return GroupMessageEvent(
        time=time, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id, card=card, nickname="昵称" + str(user_id)),
        message=[{"type": "text", "data": {"text": text}}],
    )


# ── 1. praise 插件配置化 ─────────────────────────────────────

async def test_praise_configurable():
    import sys
    # 用独立模块名加载, 避免把 sys.modules["main"] 覆盖为 praise 的 main
    # (test_wifepicker 也用 from main import Plugin, 全量跑时互相冲突)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "praise_plugin_main", "plugins/praise/main.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    Plugin = mod.Plugin

    class WS:
        def __init__(self):
            self.calls = 0
            self.times = []

        async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=5.0):
            self.calls += 1
            self.times.append(params["times"])
            return {"status": "ok", "retcode": 0, "data": {}}

    ws = WS()
    Plugin._ws_server = ws
    Plugin._like_limit_cache.clear()

    # 自定义: 25 赞, 每次 5 → 5 次调用; 自定义成功模板
    inst = Plugin()
    inst.plugin_config = {
        "like_total": 25,
        "like_times_per_call": 5,
        "success_msg": "👍 已赞 {count} 下",
        "fail_msg": "❌ {detail}",
        "limit_msg": "🛑 今日已上限",
    }
    handled, reply = await inst.on_message("bot_001", make_group_event(2001, "/赞我"), {})
    assert handled and reply == "👍 已赞 25 下", reply
    assert ws.calls == 5 and ws.times == [5, 5, 5, 5, 5], f"{ws.calls} {ws.times}"

    # 失败模板渲染(占位符 {detail})
    class FailWS:
        async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=5.0):
            return {"status": "failed", "retcode": 1, "data": None, "wording": "操作频繁"}

    Plugin._ws_server = FailWS()
    Plugin._like_limit_cache.clear()
    handled, reply = await inst.on_message("bot_001", make_group_event(2002, "/赞我"), {})
    assert handled and "操作频繁" in reply, reply

    # 上限缓存提示模板(缓存值必须是当天)
    from mohobot.utils.time_utils import format_utc8
    Plugin._like_limit_cache["bot_001:2002"] = format_utc8("%Y-%m-%d")
    handled, reply = await inst.on_message("bot_001", make_group_event(2002, "/赞我"), {})
    assert handled and reply == "🛑 今日已上限", reply
    print("[+] praise 配置化 OK")


# ── 2+3. 群聊最近消息缓冲与注入 ─────────────────────────────

def make_handler(recent_count=10):
    handler = MessageHandler(
        ws_server=None,
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None,
        plugin_system=None,
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(),
        global_config=type("GC", (), {"group_recent_msgs_count": recent_count})(),
    )
    return handler


async def test_group_recent_record_and_format():
    handler = make_handler(recent_count=3)
    # 记录 4 条 → 只保留最近 3 条
    for i in range(4):
        handler._note_group_recent("bot_001", make_group_event(2000 + i, f"消息{i}", time=1000 + i))
    text = handler._format_group_recent("bot_001", 888888)
    assert "消息0" not in text, "最旧 1 条应被淘汰"
    assert "消息1" in text and "消息3" in text and "消息2" in text
    assert "张三" in text and "08:16" in text  # 昵称 + 时间戳 1003 → 08:16 (UTC+8)
    # 其他群/其他 bot 无缓冲
    assert handler._format_group_recent("bot_001", 999999) == ""
    assert handler._format_group_recent("bot_002", 888888) == ""
    print("[+] 群聊最近消息缓冲 OK")


async def test_group_recent_inject_agent_and_legacy():
    handler = make_handler(recent_count=10)
    handler._note_group_recent("bot_001", make_group_event(2001, "今晚吃什么", time=1000000))
    handler._note_group_recent("bot_001", make_group_event(2002, "火锅吧", time=1000001))

    # agent 路径: _agent_context_provider 群聊追加最近消息
    ctx_text = await handler._agent_context_provider("bot_001", "group", "888888")
    assert "【群聊最近消息】" in ctx_text and "火锅吧" in ctx_text, ctx_text

    # 私聊不注入
    ctx_private = await handler._agent_context_provider("bot_001", "private", "2001")
    assert "【群聊最近消息】" not in ctx_private

    # legacy 路径: context 附加 system 段(不写回文件)
    context = await handler._ctx_mgr.load_context("bot_001", "group", "888888")
    full = await handler._build_legacy_context("bot_001", "group", "888888")
    assert any(e.get("role") == "system" and "【群聊最近消息】" in e.get("content", "")
               for e in full), "legacy 应附加 system 段"
    # 文件未被污染
    on_disk = await handler._ctx_mgr.load_context("bot_001", "group", "888888")
    assert on_disk == context == []
    print("[+] agent/legacy 注入 OK")


async def test_group_recent_disabled():
    handler = make_handler(recent_count=0)
    handler._note_group_recent("bot_001", make_group_event(2001, "你好"))
    assert handler._format_group_recent("bot_001", 888888) == ""
    # 关闭时注入为空
    ctx_text = await handler._agent_context_provider("bot_001", "group", "888888")
    assert "【群聊最近消息】" not in ctx_text
    print("[+] 关闭开关 OK")


async def _main() -> int:
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                await fn()
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
