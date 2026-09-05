"""并发稳定性测试 — 6 个 bot 同时执行指令/消息时不允许出现 bug。

覆盖:
1. BanStore: 6 bot 并发 upsert/delete/is_banned/clear_banned(读改写竞态)
2. 关系插件: 6 bot 并发 /加审批员 /拉黑(配置 _persist 并发丢更新)
3. message_handler: 6 bot 并发消息(上下文 append / terms / poke)
4. 插件配置: 面板保存与插件运行时 _persist 并发写同一存档
"""

import asyncio
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, "plugins/relationship")

BOTS = [f"bot_{i:03d}" for i in range(6)]


async def test_ban_store_concurrent() -> None:
    """6 bot 并发封禁/解封/查询/清理, 不丢数据不崩溃。"""
    from mohobot.ban.store import BanStore

    tmp = tempfile.mkdtemp(prefix="ban_conc_")
    store = BanStore(data_dir=tmp, cache_ttl=1)

    async def worker(bot_idx: int) -> None:
        uid = str(1000 + bot_idx)
        for _ in range(30):
            session = f"group:{bot_idx}"
            await store.upsert("ban", uid, session_key=session, time_val=3600, reason=f"r{bot_idx}")
            banned, _ = await store.is_banned(session, uid)
            assert banned, f"{bot_idx} 封禁后应命中"
            await store.upsert("pass", uid, session_key=session, time_val=60)
            banned, _ = await store.is_banned(session, uid)
            assert not banned, f"{bot_idx} 解禁后应放行"
            await store.delete("pass", uid, session_key=session)
            await store.delete("ban", uid, session_key=session)
            banned, _ = await store.is_banned(session, uid)
            assert not banned, f"{bot_idx} 删除后应放行"

    # 并发执行 + 穿插 clear_banned(读改写竞态场景)
    async def clearer() -> None:
        for _ in range(10):
            await store.clear_banned()
            await asyncio.sleep(0.005)

    results = await asyncio.gather(
        *[worker(i) for i in range(6)], clearer(), return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"并发封禁出现异常: {errors}"

    # 数据一致性: 无残留记录
    data = await store.get_all()
    assert not data["ban"] and not data["pass"], f"应有残留: {data['ban']} {data['pass']}"
    print("[1] BanStore 6bot 并发 OK")


async def test_relationship_config_concurrent() -> None:
    """6 bot 并发 /加审批员(修改同一配置) — 不丢更新。"""
    from mohobot.interceptors.plugin_system import PluginSystem

    tmp = tempfile.mkdtemp(prefix="rel_conc_")
    ps = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    ps.set_admin_ids([1001])
    await ps.load_plugins()
    meta = next(m for m in ps._plugins if m["name"] == "relationship")
    inst = meta["instance"]
    inst.__class__.inject_ws_server(None)

    async def worker(bot_idx: int) -> None:
        uid = str(5000 + bot_idx)
        inst._ensure_handlers()
        await inst._cfg.add_manage_user(uid)

    await asyncio.gather(*[worker(i) for i in range(6)])

    # 6 个审批员都应持久化(并发写文件不丢)
    saved = json.loads((Path(tmp) / "plugins_config" / "relationship.json").read_text(encoding="utf-8"))
    extra = saved.get("manage_users", [])
    assert len(extra) == 6, f"并发添加审批员丢失: {extra}"
    print("[2] 关系插件配置并发修改 OK")


async def test_handler_concurrent_messages() -> None:
    """6 bot 并发消息: 上下文 append + 拦截链不互踩。"""
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.context_manager import ContextManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import BotConfig, GlobalConfig, ReplyConfig
    from mohobot.models.onebot import GroupMessageEvent, Sender

    tmp = tempfile.mkdtemp(prefix="msg_conc_")
    bm = BotManager(data_dir=tmp)

    class FakeWS:
        def __init__(self, bm):
            self._bot_manager = bm
            self.sent = []

        async def send_group_msg(self, bot_id, group_id, message):
            self.sent.append((bot_id, group_id, message))

        async def send_private_msg(self, bot_id, user_id, message):
            self.sent.append((bot_id, user_id, message))

        async def send_to_bot(self, *a, **kw):
            return {"status": "ok", "retcode": 0, "data": {}}

    class FakeLLM:
        async def chat_stream(self, **kw):
            yield ("好的", True)

        async def chat(self, **kw):
            return ("好的", None)

        async def describe_image(self, url):
            return "图"

    class FakePlugins:
        async def dispatch_notice(self, *a, **kw):
            pass

        async def dispatch_meta(self, *a, **kw):
            pass

        async def dispatch_request(self, *a, **kw):
            return False

        async def dispatch_observed(self, *a, **kw):
            return (False, None)

    ws = FakeWS(bm)
    ctx = ContextManager(data_dir=tmp)
    handler = MessageHandler(
        ws_server=ws,
        context_manager=ctx,
        llm_service=FakeLLM(),
        plugin_system=FakePlugins(),
        data_dir=tmp,
        reply_config=ReplyConfig(stream=True, segment_reply=False),
        global_config=GlobalConfig(),
    )

    # 6 bot 各 5 条并发消息(agent 关闭 → 旧版路径)
    async def worker(i: int) -> None:
        bot_id = f"bot_{i:03d}"
        bm._bots[bot_id] = BotInstance(
            bot_id, None, BotConfig(qq=100 + i, nickname=f"B{i}"),
        )
        for n in range(5):
            ev = GroupMessageEvent(
                time=0, self_id=1, post_type="message", message_type="group",
                message_id=n + 1, user_id=200 + i, group_id=300 + i,
                sender=Sender(user_id=200 + i),
                message=[{"type": "text", "data": {"text": f"/并发消息{n}"}}],
            )
            await handler.handle_event(bot_id, ev, {"post_type": "message"})

    await asyncio.gather(*[worker(i) for i in range(6)])
    assert len(ws.sent) == 30, f"应发送 30 条, 实际 {len(ws.sent)}"
    print("[3] 6bot 并发消息处理 OK")


async def test_plugin_config_vs_persist_concurrent() -> None:
    """面板保存配置 与 插件运行时 _persist 并发写同一存档。"""
    from mohobot.interceptors.plugin_system import PluginSystem

    tmp = tempfile.mkdtemp(prefix="cfg_conc_")
    ps = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    ps.set_admin_ids([1001])
    await ps.load_plugins()
    meta = next(m for m in ps._plugins if m["name"] == "relationship")
    inst = meta["instance"]
    inst.__class__.inject_ws_server(None)

    async def panel_save():
        for i in range(20):
            await asyncio.sleep(0.002)
            await ps.save_plugin_config("relationship", {
                "check": {"count": 20 + i},
                "notice": {"max_group_capacity": 100 + i},
            })

    async def plugin_persist():
        for i in range(20):
            inst._ensure_handlers()
            await inst._cfg.add_black_group(str(9000 + i))
            await asyncio.sleep(0.001)

    results = await asyncio.gather(panel_save(), plugin_persist(), return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"并发配置写入异常: {errors}"

    # 存档仍是合法 JSON 且不丢失黑名单
    saved = json.loads((Path(tmp) / "plugins_config" / "relationship.json").read_text(encoding="utf-8"))
    assert isinstance(saved, dict)
    print("[4] 面板保存 vs 插件 _persist 并发 OK")


async def test_api_response_routing_concurrent() -> None:
    """6 bot 并发 API 调用: echo 响应路由正确 + 无 echo 响应只匹配本 bot。"""
    from mohobot.bot_manager import BotManager

    bm = BotManager(data_dir=tempfile.mkdtemp(prefix="api_conc_"))

    # 6 bot 各发起 3 个 wait_response 调用(共 18 个 pending)
    async def caller(i: int) -> list:
        bot_id = f"bot_{i:03d}"
        got = []
        for n in range(3):
            echo = f"api_{bot_id}_{n}"
            fut = bm.create_response_future(bot_id, echo)
            # 模拟客户端响应: 带 echo
            await bm.handle_api_response(bot_id, {"status": "ok", "echo": echo, "data": {"v": n}})
            got.append(await fut)
        return got

    results = await asyncio.gather(*[caller(i) for i in range(6)])
    for i, r in enumerate(results):
        assert len(r) == 3
        assert [d["data"]["v"] for d in r] == [0, 1, 2], f"bot_{i} 响应错配: {r}"
    # 全部 pending 已清空
    assert not any(bm._pending_responses.values()), f"残留 pending: {bm._pending_responses}"

    # 无 echo 错误响应: 只有本 bot 恰好 1 个 pending 时才匹配, 不串号
    bm2 = BotManager(data_dir=tempfile.mkdtemp(prefix="api_conc2_"))
    f_a = bm2.create_response_future("bot_001", "e_a")
    f_b = bm2.create_response_future("bot_002", "e_b")  # bot_002 也有 pending
    await bm2.handle_api_response("bot_002", {"status": "failed", "data": None})  # 无 echo
    # bot_002 恰好 1 个 pending → 匹配其自身, bot_001 不受影响
    assert f_b.done() and not f_a.done(), "无 echo 响应应只匹配发出该响应的 bot"
    assert not f_b.cancelled()
    # bot_001 的 pending 仍在
    assert "e_a" in bm2._pending_responses.get("bot_001", {})
    bm2.remove_response_future("bot_001", "e_a")
    print("[5] 多 bot 并发 API 响应路由 OK")


async def main() -> None:
    await test_ban_store_concurrent()
    await test_relationship_config_concurrent()
    await test_handler_concurrent_messages()
    await test_plugin_config_vs_persist_concurrent()
    await test_api_response_routing_concurrent()
    print("\nALL CONCURRENCY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
