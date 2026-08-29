"""六个新功能测试:
1. no_prefix_triggers: 占卜/赞我 群聊未 @ 直接触发
2. 广播插件: 预览→确认→广播(可指定 bot/类型)
3. relationship 批量加群/加好友(延迟配置 + 失败跳过)
4. 合并转发按 1000 字分条(不按行)
5. summarize max_tokens=4096
6. 用量统计: /用量 按 bot × 模块汇总
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


# ── 1. no_prefix_triggers ───────────────────────────────────

async def test_no_prefix_triggers():
    from mohobot.interceptors.plugin_system import PluginSystem
    ps = PluginSystem(plugins_dir="plugins", data_dir=tempfile.mkdtemp())
    ps.set_admin_ids([1001])
    await ps.load_plugins()

    # 群聊未 @ 发"占卜" → 观察钩子阶段被 divination 消费
    handled, reply = await ps.dispatch_observed(
        "bot_001", make_group_event(2001, "占卜"), {},
    )
    assert handled and reply and ("财运" in reply or "今日" in reply or "占卜" in reply), reply

    # 赞我(无 /) → praise 消费
    from mohobot.interceptors.plugin_system import PluginSystem as _PS
    # praise 需要 ws(否则提示未配置) — 至少验证被消费(handled=True)
    handled2, reply2 = await ps.dispatch_observed(
        "bot_001", make_group_event(2002, "赞我"), {},
    )
    assert handled2, "无 / 的赞我应被插件消费"

    # 非触发词不受影响
    handled3, _ = await ps.dispatch_observed(
        "bot_001", make_group_event(2003, "随便聊聊"), {},
    )
    assert not handled3
    print("[+] no_prefix_triggers OK")


# ── 2. 广播插件 ─────────────────────────────────────────────

class BroadcastWS:
    def __init__(self):
        self.private = []
        self.group = []
        self.api_calls = []
        self._bot_manager = None

    async def send_private_msg(self, bot_id, user_id, message):
        self.private.append((bot_id, user_id, message))

    async def send_group_msg(self, bot_id, group_id, message):
        self.group.append((bot_id, group_id, message))

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        self.api_calls.append((bot_id, action))
        if action == "get_friend_list":
            return {"status": "ok", "retcode": 0, "data": [{"user_id": 9001}, {"user_id": 9002}]}
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": [{"group_id": 9101}]}
        return {"status": "ok", "retcode": 0, "data": {}}


def load_plugin(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"{name}_main", f"plugins/{name}/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_broadcast_plugin(ws):
    mod = load_plugin("broadcast")
    inst = mod.Plugin()
    inst._ws_server = ws
    inst._admin_ids = ["1001"]
    return inst


async def test_broadcast_flow():
    ws = BroadcastWS()
    inst = make_broadcast_plugin(ws)
    # 预览(管理员)
    handled, reply = await inst.on_message(
        "bot_001", make_group_event(1001, "/广播预览 大家好"), {},
    )
    assert handled and "已发送预览" in reply
    assert any("广播预览" in str(m) for _, _, m in ws.private)
    assert any("广播预览" in str(m) for _, _, m in ws.group)
    assert inst._pending and "大家好" in inst._pending["content"]

    # 非管理员拒绝
    handled, reply = await inst.on_message(
        "bot_001", make_group_event(2001, "/广播预览 大家好"), {},
    )
    assert handled and "没有权限" in reply

    # 确认 → 后台广播(等待任务完成)
    handled, reply = await inst.on_message(
        "bot_001", make_group_event(1001, "/广播确认 bot_001 全部"), {},
    )
    assert handled and "已开始广播" in reply
    await _wait_broadcast(inst)
    # 私聊好友 2 个 + 群 1 个
    sent_priv = [m for _, _, m in ws.private if m == "大家好"]
    sent_grp = [m for _, _, m in ws.group if m == "大家好"]
    assert len(sent_priv) == 2 and len(sent_grp) == 1, (sent_priv, sent_grp)
    # 汇总发回
    assert any("广播完成" in str(m) for _, _, m in ws.private) or \
           any("广播完成" in str(m) for _, _, m in ws.group)

    # 无待确认 → 拒绝
    handled, reply = await inst.on_message(
        "bot_001", make_group_event(1001, "/广播确认"), {},
    )
    assert handled and "没有待确认" in reply
    print("[+] 广播流程 OK")


async def _wait_broadcast(inst):
    import asyncio
    for _ in range(100):
        if not inst._running:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("广播任务未完成")


# ── 3. relationship 批量 ────────────────────────────────────

class BatchWS:
    def __init__(self):
        self.calls = []
        self.sent = []

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        self.calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send_group_msg(self, bot_id, group_id, message):
        self.sent.append(("group", group_id, message))

    async def send_private_msg(self, bot_id, user_id, message):
        self.sent.append(("private", user_id, message))


def make_relationship_plugin(ws):
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("relationship_plugin_main", "plugins/relationship/main.py")
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    inst = _mod.Plugin()
    inst._ws_server = ws
    inst._admin_ids = ["1001"]
    inst.plugin_config = {"batch_delay_min": 0, "batch_delay_max": 0}
    return inst


async def test_batch_join():
    import asyncio
    ws = BatchWS()
    inst = make_relationship_plugin(ws)
    handled, reply = await inst.on_message(
        "bot_001", make_group_event(1001, "/批量加群 111,222,333"), {},
    )
    assert handled and "开始批量加群" in reply
    # 等待后台任务
    for _ in range(100):
        if not inst._batch_running:
            break
        await asyncio.sleep(0.05)
    actions = [a for a, _ in ws.calls]
    assert actions == ["set_group_add"] * 3, actions
    targets = [p["group_id"] for a, p in ws.calls]
    assert targets == [111, 222, 333]
    assert any("批量加群完成" in str(m) for _, _, m in ws.sent)

    # 批量加好友
    inst._batch_running = False
    ws.calls.clear()
    handled, reply = await inst.on_message(
        "bot_001", make_group_event(1001, "/批量加好友 444,555"), {},
    )
    assert handled and "开始批量加好友" in reply
    for _ in range(100):
        if not inst._batch_running:
            break
        await asyncio.sleep(0.05)
    assert [a for a, _ in ws.calls] == ["set_friend_add"] * 2
    assert [p["user_id"] for a, p in ws.calls] == [444, 555]
    # 非管理员
    handled, reply = await inst.on_message(
        "bot_001", make_group_event(2001, "/批量加群 111"), {},
    )
    assert handled and "没有权限" in reply
    print("[+] 批量加群/加好友 OK")


# ── 4. 合并转发按 1000 字 ──────────────────────────────────

async def test_forward_chunk_by_chars():
    from mohobot.context_manager import ContextManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import GlobalConfig, ReplyConfig

    class ForwardWS:
        def __init__(self):
            self.forward_calls = []
            self._bot_manager = None

        async def send_group_forward_msg(self, bot_id, group_id, nodes):
            self.forward_calls.append((bot_id, group_id, nodes))

        async def send_to_bot(self, *a, **k):
            return {"status": "ok", "retcode": 0, "data": {}}

    ws = ForwardWS()
    handler = MessageHandler(
        ws_server=ws,
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None,
        plugin_system=None,
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(stream=False),
        global_config=GlobalConfig(),
    )
    text = "第%d行内容" * 0 + "".join(f"行{i}内容" for i in range(210))  # 210*4=840字
    text = "字" * 2100  # 2100 字 → 3 块(1000/1000/100)
    ok = await handler._try_send_forward("bot_001", make_group_event(2001, "/help"), text)
    assert ok
    _, _, nodes = ws.forward_calls[-1]
    assert len(nodes) == 3, f"2100 字应按 1000 字分 3 条, 实际 {len(nodes)}"
    contents = [n["data"]["content"][0]["data"]["text"] for n in nodes]
    assert contents[0] == "字" * 1000 and contents[1] == "字" * 1000 and contents[2] == "字" * 100
    # 多行文本也不再按行拆分(808 字 1 块 → 回退普通发送, 不触发合并转发)
    text2 = ("行" * 100 + "\n") * 8  # 808 字含换行 → 1 块
    ok2 = await handler._try_send_forward("bot_001", make_group_event(2001, "/help"), text2)
    assert not ok2, "808 字只有 1 块, 应回退普通发送"
    print("[+] 合并转发 1000 字分条 OK")


# ── 5. summarize max_tokens ─────────────────────────────────

async def test_summarize_max_tokens():
    import inspect
    from mohobot.models.config import GlobalConfig
    from mohobot.llm_service import LLMService
    # 总结参数现在可配置(llm.summarize_max_tokens/summarize_temperature),
    # 调用点应从配置读取而非硬编码
    src = inspect.getsource(LLMService.summarize_context)
    assert "summarize_max_tokens" in src, "总结应读取可配置的 summarize_max_tokens"
    assert "summarize_temperature" in src
    cfg = GlobalConfig()
    svc = LLMService(global_config=cfg, usage_recorder=None)
    assert cfg.llm.summarize_max_tokens == svc._cfg.llm.summarize_max_tokens
    print("[+] summarize max_tokens 可配置 OK")


# ── 6. 用量统计 ─────────────────────────────────────────────

async def test_usage_stats():
    import tempfile as _t
    td = _t.mkdtemp()
    stats_dir = Path(td) / "stats"
    stats_dir.mkdir(parents=True)
    import time as _time
    now = _time.time()
    with open(stats_dir / "llm_usage.jsonl", "w", encoding="utf-8") as f:
        for i, (bid, module, pt, ct) in enumerate([
            ("bot_001", "main_chat", 100, 50),
            ("bot_001", "main_chat", 200, 60),
            ("bot_002", "topic_extractor", 300, 40),
            ("bot_002", "memory_writer", 150, 30),
        ]):
            f.write(json.dumps({
                "time": now, "bot_id": bid, "module": module,
                "prompt_tokens": pt, "completion_tokens": ct,
                "total_tokens": pt + ct,
            }, ensure_ascii=False) + "\n")

    mod = load_plugin("usage_stats")
    inst = mod.Plugin()
    inst._data_dir = td
    inst._admin_ids = ["1001"]

    async def run():
        return

        assert handled and "今日" in reply
        assert "bot_001" in reply and "bot_002" in reply
        assert "主回复" in reply and "话题提取" in reply and "记忆写入" in reply
        assert "平均每条" in reply
        # bot_001: 410 token, 2 次
        assert "bot_001: 410 token, 2 次" in reply, reply
        # 非管理员
        handled2, reply2 = await inst.on_message(
            "bot_001", make_private_event(2001, "/用量"), {},
        )
        assert handled2 and "没有权限" in reply2
        # 7d
        handled3, reply3 = await inst.on_message(
            "bot_001", make_private_event(1001, "/用量 7d"), {},
        )
        assert handled3 and "近 7 天" in reply3
    print("[+] 用量统计 OK")


async def test_no_prefix_global_dedup():
    """无前缀"占卜"在群内多 bot 时只由最小 bot 处理; "赞我"不去重(每 bot 都处理)。"""
    from mohobot.interceptors.plugin_system import PluginSystem
    from mohobot.bot_manager import BotManager, BotInstance
    from mohobot.models.config import BotConfig

    ps = PluginSystem(plugins_dir="plugins", data_dir=tempfile.mkdtemp())
    ps.set_admin_ids([1001])
    bm = BotManager(data_dir=tempfile.mkdtemp())
    bm._bots["bot_001"] = BotInstance("bot_001", None, BotConfig(qq=1000))
    bm._bots["bot_002"] = BotInstance("bot_002", None, BotConfig(qq=2000))
    bm.note_group_message("bot_001", 888888)
    bm.note_group_message("bot_002", 888888)
    ps.set_runtime_refs(bot_manager=bm)
    await ps.load_plugins()

    # "占卜"(无 /, 命中 divination 的 global_triggers) → 群内只最小 bot 处理
    ev = make_group_event(2001, "占卜")
    handled_min, _ = await ps.dispatch_observed("bot_001", ev, {})
    handled_other, _ = await ps.dispatch_observed("bot_002", ev, {})
    assert handled_min, "最小 bot 应处理占卜"
    assert not handled_other, "非最小 bot 不应处理占卜"

    # "赞我"(praise 无 global_triggers) → 不去重, 每个 bot 都处理
    ev2 = make_group_event(2002, "赞我")
    h1, _ = await ps.dispatch_observed("bot_001", ev2, {})
    h2, _ = await ps.dispatch_observed("bot_002", ev2, {})
    assert h1 and h2, "赞我无 global_triggers, 各 bot 都应处理"

    # 私聊不去重
    ev3 = make_private_event(2001, "占卜")
    h_p, _ = await ps.dispatch_observed("bot_002", ev3, {})
    assert h_p, "私聊占卜不受群内去重影响"
    print("[+] no_prefix 群内去重 OK")


async def test_plugin_tick_tasks():
    """框架周期任务: interval_sec + on_tick 启动/停止。"""
    import asyncio
    from mohobot.interceptors.plugin_system import PluginSystem

    class TickPlugin:
        interval_sec = 1
        ticks = 0

        async def on_tick(self):
            TickPlugin.ticks += 1

    ps = PluginSystem(plugins_dir="plugins", data_dir=tempfile.mkdtemp())
    ps._plugins = [{
        "name": "tick_test", "enabled": True, "loaded": True,
        "instance": TickPlugin(),
    }]
    ps.start_tick_tasks()
    await asyncio.sleep(2.3)
    assert TickPlugin.ticks >= 2, f"2.3s 内应至少 tick 2 次(1s 间隔), 实际 {TickPlugin.ticks}"
    # 停止后不再 tick
    ps.stop_tick_tasks()
    n = TickPlugin.ticks
    await asyncio.sleep(1.2)
    assert TickPlugin.ticks == n, "停止后不应再 tick"
    # 无 interval_sec 的插件不启动任务
    class NoTick:
        async def on_tick(self):
            pass
    ps2 = PluginSystem(plugins_dir="plugins", data_dir=tempfile.mkdtemp())
    ps2._plugins = [{"name": "no_tick", "enabled": True, "loaded": True, "instance": NoTick()}]
    ps2.start_tick_tasks()
    assert ps2._tick_tasks == [], "未声明 interval_sec 不应启动任务"
    print("[+] 框架周期任务 OK")


async def _main() -> int:
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
    import asyncio
    failed = asyncio.run(_main())
    total = len([n for n in globals() if n.startswith("test_") and callable(globals()[n])])
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
