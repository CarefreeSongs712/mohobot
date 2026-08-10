"""环境感知插件与框架注入测试:
1. on_perception: 时间/节假日/农历/节气/群聊环境(含群名 API+缓存/消息类型)
2. collect_perception: 多插件收集拼接
3. message_handler: 感知缓存刷新 + legacy 注入(system 段, 不写 context)
   + agent _agent_perception_provider
4. runtime._attach_perception 拼接(仅回复生成路径)
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender


def make_group_event(user_id, text, group_id=888888, with_image=False):
    segs = [{"type": "text", "data": {"text": text}}]
    if with_image:
        segs.append({"type": "image", "data": {"url": "http://x/a.png"}})
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id), message=segs,
    )


def make_private_event(user_id, text):
    return PrivateMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="private",
        message_id=1, user_id=user_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def load_perception():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "perception_plugin_main", "plugins/perception/main.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GroupInfoWS:
    def __init__(self, name="测试群"):
        self.name = name
        self.calls = 0

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=5.0):
        if action == "get_group_info":
            self.calls += 1
            return {"status": "ok", "retcode": 0, "data": {"group_name": self.name}}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send_group_msg(self, bot_id, group_id, message):
        pass

    async def send_private_msg(self, bot_id, user_id, message):
        pass


# ── 1. 感知内容 ─────────────────────────────────────────────

async def test_perception_content():
    mod = load_perception()
    inst = mod.Plugin()
    ws = GroupInfoWS()
    inst._ws_server = ws
    mod._GROUP_NAME_CACHE.clear()

    # 群聊 + 图片
    text = await inst.on_perception("bot_001", make_group_event(2001, "hi", with_image=True), {})
    assert "发送时间:" in text and "周一" in text and "工作日" in text
    assert "农历" in text and "年" in text
    assert "节气" in text
    assert "平台: QQ" in text and "群聊" in text and "群名: 测试群" in text and "含图片" in text
    # 私聊
    text2 = await inst.on_perception("bot_001", make_private_event(2002, "hi"), {})
    assert "私聊" in text2 and "群名" not in text2
    # 群名缓存: 第二次不调 API
    await inst.on_perception("bot_001", make_group_event(2003, "hi"), {})
    assert ws.calls == 1, f"群名应缓存, 实际调用 {ws.calls} 次"
    print("[+] 感知内容 OK")


async def test_perception_disabled():
    mod = load_perception()
    inst = mod.Plugin()
    inst._ws_server = GroupInfoWS()
    mod._GROUP_NAME_CACHE.clear()
    # 全部关闭 → 只有时间
    inst.plugin_config = {k: False for k in inst._DEFAULTS}
    text = await inst.on_perception("bot_001", make_group_event(2001, "hi"), {})
    assert "发送时间" in text
    assert "农历" not in text and "群名" not in text and "工作日" not in text
    print("[+] 感知开关 OK")


# ── 2. collect_perception 收集 ──────────────────────────────

async def test_collect_perception():
    from mohobot.interceptors.plugin_system import PluginSystem

    ps = PluginSystem(plugins_dir="plugins", data_dir=tempfile.mkdtemp())
    await ps.load_plugins()
    # 只保留感知插件(其余插件无 on_perception, 不影响)
    text = await ps.collect_perception("bot_001", make_group_event(2001, "hi"), {})
    assert "发送时间" in text, "应收集到感知文本"
    assert "农历" in text or "chinese" in text  # 感知内容
    print("[+] collect_perception OK")


# ── 3. message_handler 注入 ─────────────────────────────────

async def test_message_handler_injection():
    from mohobot.context_manager import ContextManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import GlobalConfig, ReplyConfig

    handler = MessageHandler(
        ws_server=None,
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None,
        plugin_system=None,
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(),
        global_config=GlobalConfig(),
    )
    # 模拟感知缓存(插件收集结果)
    handler._perception_text[("bot_001", "group", "888888")] = "发送时间: 2026-08-10 | 工作日"

    # legacy: _build_legacy_context 附加感知 system 段, 不写回文件
    context = await handler._build_legacy_context("bot_001", "group", "888888")
    sys_segs = [e for e in context if e.get("role") == "system"]
    assert any("【环境感知】" in e.get("content", "") for e in sys_segs), context
    on_disk = await handler._ctx_mgr.load_context("bot_001", "group", "888888")
    assert on_disk == [], "感知不应写入 context 文件"

    # agent: _agent_perception_provider 返回缓存
    perc = await handler._agent_perception_provider("bot_001", "group", "888888")
    assert "发送时间" in perc
    assert await handler._agent_perception_provider("bot_001", "private", "1") == ""
    print("[+] message_handler 注入 OK")


# ── 4. runtime 拼接(仅回复生成) ────────────────────────────

async def test_runtime_attach():
    from mohobot.agent.runtime import SessionPipeline, BotAgentManager

    # 静态拼接方法(定义于 SessionPipeline, 回复路径 _reply_one_topic 使用)
    assert SessionPipeline._attach_perception("旧上下文", "感知1") == \
        "旧上下文\n\n【环境感知】\n感知1"
    assert SessionPipeline._attach_perception("", "感知1") == "【环境感知】\n感知1"
    assert SessionPipeline._attach_perception("旧上下文", "") == "旧上下文"

    # get_or_create 传递 perception_provider
    mgr = BotAgentManager({"agent": {}}, None)
    calls = []

    async def fake_perc(bot_id, chat_type, chat_id):
        calls.append((bot_id, chat_type, chat_id))
        return "环境信息"

    async def fake_ctx(bot_id, chat_type, chat_id):
        return "对话历史"

    rt = mgr.get_or_create(
        "bot_001", bot_nickname="测试", persona="人设",
        context_provider=fake_ctx, perception_provider=fake_perc,
    )
    assert rt.perception_provider is not None
    # 通过 SessionPipeline 实例调用 _get_perception(感知缓存路径)
    pipe = SessionPipeline.__new__(SessionPipeline)  # 仅测试方法绑定
    pipe.runtime = rt
    pipe.chat_type = "group"
    pipe.chat_id = "888888"
    text = await pipe._get_perception()
    assert text == "环境信息", text
    print("[+] runtime 拼接 OK")


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
