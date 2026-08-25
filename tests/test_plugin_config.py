"""插件配置系统 + 目录插件 + 关系插件测试:
1. 目录插件加载(relationship 包) + schema 解析 + 默认配置生成/存档
2. 插件配置保存热同步(实例 plugin_config 更新 + on_config_update 回调)
3. 管理员注入(inject_admin_ids)
4. on_request 钩子分发(message_handler.request 先给插件)
5. 关系插件命令: 管理员权限门控 / 审批流(引用解析) / 通知决策
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def test_plugin_config_system() -> None:
    """目录插件 + schema 默认值 + 存档 + 保存热同步。"""
    from mohobot.interceptors.plugin_system import PluginSystem

    tmp = tempfile.mkdtemp(prefix="pcfg_")
    ps = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    ps.set_admin_ids([1001])
    await ps.load_plugins()

    meta = next(m for m in ps._plugins if m["name"] == "relationship")
    assert meta["loaded"] is True, meta.get("error")

    # schema 解析
    schema = meta["config_schema"]
    assert set(schema.keys()) == {"manage_group", "manage_users", "check", "request", "notice", "batch_delay_min", "batch_delay_max"}
    assert schema["check"]["items"]["count"]["default"] == 20
    assert schema["notice"]["items"]["max_group_capacity"]["default"] == 100

    # 默认配置生成 + 存档写入
    config = meta["config"]
    assert config["check"]["count"] == 20
    assert config["notice"]["max_ban_days"] == 3
    assert config["request"]["auto_agree_friend"] is False
    assert config["manage_users"] == []
    archive = Path(tmp) / "plugins_config" / "relationship.json"
    assert archive.exists()

    # 保存配置 → 热同步到实例
    ok = await ps.save_plugin_config("relationship", {
        "manage_group": "123456",
        "manage_users": ["2001"],
        "check": {"count": 50},
        "request": {"auto_agree_friend": True},
        "notice": {"max_group_capacity": 200},
    })
    assert ok
    meta2 = next(m for m in ps._plugins if m["name"] == "relationship")
    assert meta2["config"]["check"]["count"] == 50
    assert meta2["config"]["request"]["auto_agree_friend"] is True
    assert meta2["config"]["manage_group"] == "123456"
    # 实例已注入(plugin_config 属性)
    inst = meta2["instance"]
    assert getattr(inst, "plugin_config", {}).get("manage_group") == "123456"
    # 存档已更新
    saved = json.loads(archive.read_text(encoding="utf-8"))
    assert saved["check"]["count"] == 50

    # 未知插件保存 → False
    assert await ps.save_plugin_config("nope", {}) is False
    print("[1] 插件配置系统(目录插件/schema/默认/热同步) OK")


async def test_admin_injection() -> None:
    """管理员注入: inject_admin_ids 类级注入。"""
    from mohobot.interceptors.plugin_system import PluginSystem

    tmp = tempfile.mkdtemp(prefix="padm_")
    ps = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    ps.set_admin_ids([1001, 1002])
    await ps.load_plugins()

    meta = next(m for m in ps._plugins if m["name"] == "relationship")
    inst = meta["instance"]
    assert "1001" in inst._admin_ids and "1002" in inst._admin_ids

    # 热更新
    ps.set_admin_ids([9999])
    assert inst._admin_ids == ["9999"]
    print("[2] 管理员注入 OK")


async def test_relationship_commands() -> None:
    """关系插件命令: 权限门控 + 命令分发。"""
    import sys as _sys
    sys.path.insert(0, "plugins/relationship")
    from mohobot.interceptors.plugin_system import PluginSystem
    from mohobot.models.onebot import GroupMessageEvent, Sender

    def group_event(text: str, user_id: int = 1001, group_id: int = 2001) -> GroupMessageEvent:
        return GroupMessageEvent(
            time=0, self_id=1, post_type="message",
            message_type="group", message_id=1,
            user_id=user_id, group_id=group_id,
            sender=Sender(user_id=user_id),
            message=[{"type": "text", "data": {"text": text}}],
        )

    class FakeWS:
        def __init__(self):
            self.calls = []

        async def send_to_bot(self, bot_id, action, params=None, wait_response=False, timeout=10.0):
            self.calls.append((action, params))
            if action == "get_group_list":
                return {"status": "ok", "retcode": 0,
                        "data": [{"group_id": 111, "group_name": "群A"}, {"group_id": 222, "group_name": "群B"}]}
            if action == "get_friend_list":
                return {"status": "ok", "retcode": 0,
                        "data": [{"user_id": 333, "nickname": "好友C"}]}
            if action == "get_group_info":
                return {"status": "ok", "retcode": 0, "data": {"group_name": "群A", "member_count": 100}}
            if action == "get_stranger_info":
                return {"status": "ok", "retcode": 0, "data": {"nickname": "路人"}}
            return {"status": "ok", "retcode": 0, "data": {}}

        async def send_group_msg(self, bot_id, group_id, message):
            self.calls.append(("send_group_msg", (group_id, message)))

        async def send_private_msg(self, bot_id, user_id, message):
            self.calls.append(("send_private_msg", (user_id, message)))

    tmp = tempfile.mkdtemp(prefix="rcmd_")
    ps = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    ps.set_admin_ids([1001])
    await ps.load_plugins()
    meta = next(m for m in ps._plugins if m["name"] == "relationship")
    inst = meta["instance"]
    # 通过实例的类注入 ws_server(与 apply_injections 一致, 避免重复 import)
    fake_ws = FakeWS()
    inst.__class__.inject_ws_server(fake_ws)

    # 非管理员 → 拒绝
    handled, reply = await inst.on_message("bot_001", group_event("/群列表", user_id=9999), {})
    assert handled and "没有权限" in reply

    # 管理员 /群列表
    handled, reply = await inst.on_message("bot_001", group_event("/群列表", user_id=1001), {})
    assert handled and "群A" in reply and "群B" in reply

    # 管理员 /好友列表
    handled, reply = await inst.on_message("bot_001", group_event("/好友列表", user_id=1001), {})
    assert handled and "好友C" in reply

    # /退群 111
    handled, reply = await inst.on_message("bot_001", group_event("/退群 111", user_id=1001), {})
    assert handled and "已退出群聊" in reply

    # 未知命令 → 不处理
    handled, reply = await inst.on_message("bot_001", group_event("/随便什么"), {})
    assert handled is False

    # 非命令 → 不处理
    handled, reply = await inst.on_message("bot_001", group_event("你好"), {})
    assert handled is False
    print("[3] 关系插件命令 + 权限门控 OK")


async def test_request_dispatch() -> None:
    """message_handler._handle_request 先交给插件。"""
    import mohobot.message_handler as mh
    from mohobot.message_handler import MessageHandler
    from mohobot.context_manager import ContextManager

    class FakePlugins:
        def __init__(self):
            self.called = False

        async def dispatch_request(self, bot_id, event, raw):
            self.called = True
            return True  # 插件接管

    class FakeWS:
        async def send_to_bot(self, *a, **kw):
            raise AssertionError("插件接管后框架不应自动同意")

    plugins = FakePlugins()
    handler = MessageHandler(
        ws_server=FakeWS(),
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None,
        plugin_system=plugins,
        data_dir=tempfile.mkdtemp(),
    )
    from mohobot.models.onebot import RequestEvent
    ev = RequestEvent(time=0, self_id=1, post_type="request",
                      request_type="friend", user_id=123, flag="f1")
    await handler._handle_request("bot_001", ev, {"post_type": "request"})
    assert plugins.called, "应先把 request 交给插件"

    # 插件未处理 → 框架静默不处理(不自动同意, 申请留在那)
    class FakeWS2:
        def __init__(self):
            self.sent = []

        async def send_to_bot(self, bot_id, action, params=None, wait_response=False, timeout=10.0):
            self.sent.append((action, params))
            return None

    ws2 = FakeWS2()
    plugins2 = FakePlugins()
    plugins2.called = False
    plugins2_result = type("P", (), {})()
    plugins2_result.called = False

    async def dispatch_no(bot_id, event, raw):
        plugins2_result.called = True
        return False

    plugins2.dispatch_request = dispatch_no
    handler2 = MessageHandler(
        ws_server=ws2,
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None,
        plugin_system=plugins2,
        data_dir=tempfile.mkdtemp(),
    )
    await handler2._handle_request("bot_001", ev, {})
    assert plugins2_result.called, "应先 dispatch 插件"
    assert not ws2.sent, "插件未接管时框架应静默, 不发送任何 approve/拒绝"
    print("[4] request 事件先 dispatch 插件, 未处理则静默不处理 OK")


async def test_schema_coercion() -> None:
    """save 时 schema 类型强转: 面板提交字符串/错误类型不崩。"""
    from mohobot.interceptors.plugin_system import PluginSystem

    tmp = tempfile.mkdtemp(prefix="pcoerce_")
    ps = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    await ps.load_plugins()

    # 面板提交错误类型: int 字段给字符串, bool 给字符串, list 给逗号串
    ok = await ps.save_plugin_config("relationship", {
        "check": {"count": "50", "batch_size": "abc", "delay": "10"},
        "request": {"auto_agree_friend": "true", "auto_reject_group": 1},
        "manage_users": "2001,2002",
    })
    assert ok
    cfg = ps.get_plugin_config("relationship")
    assert cfg["check"]["count"] == 50, cfg["check"]["count"]
    assert cfg["check"]["batch_size"] == 40, "非法 int 回退默认值"
    assert cfg["check"]["delay"] == 10
    assert cfg["request"]["auto_agree_friend"] is True
    assert cfg["request"]["auto_reject_group"] is True
    assert cfg["manage_users"] == ["2001", "2002"]

    # 插件实例热同步 + on_config_update 后 cfg 重建
    meta = next(m for m in ps._plugins if m["name"] == "relationship")
    inst = meta["instance"]
    assert getattr(inst, "plugin_config", {}).get("manage_users") == ["2001", "2002"]
    assert inst._cfg is None, "on_config_update 后应失效重建"

    # 存档脏值(手工编辑)启动也不崩
    import json as _json
    archive = Path(tmp) / "plugins_config" / "relationship.json"
    archive.write_text(_json.dumps({
        "check": {"count": "abc", "batch_size": "55"},
        "notice": {"max_group_capacity": "abc"},
    }), encoding="utf-8")
    ps2 = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    await ps2.load_plugins()
    meta2 = next(m for m in ps2._plugins if m["name"] == "relationship")
    cfg2 = meta2["config"]
    assert cfg2["check"]["count"] == 20, "脏 int 回退默认"
    assert cfg2["check"]["batch_size"] == 55, "合法数字字符串应转换"
    assert cfg2["notice"]["max_group_capacity"] == 100
    print("[5] schema 类型强转 + 存档脏值防护 OK")


async def test_admin_hot_update() -> None:
    """admin 热更新即时生效(无需重启/重载插件)。"""
    import sys as _sys
    sys.path.insert(0, "plugins/relationship")
    from mohobot.interceptors.plugin_system import PluginSystem

    tmp = tempfile.mkdtemp(prefix="padm2_")
    ps = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    ps.set_admin_ids([1001])
    await ps.load_plugins()
    meta = next(m for m in ps._plugins if m["name"] == "relationship")
    inst = meta["instance"]

    # 触发一次 handler 构造
    inst._ensure_handlers()
    assert "1001" in inst._cfg.manage_users, "管理员应为审批员"

    # 面板热更新 admins
    ps.set_admin_ids([1001, 2002])
    assert set(inst._admin_ids) == {"1001", "2002"}

    # 再次入口 → 重新构造 cfg, 新管理员生效
    inst._ensure_handlers()
    assert "2002" in inst._cfg.manage_users, "热更新后的管理员应进入审批员"
    print("[6] admin 热更新即时生效 OK")


async def test_api_call_retcode_string() -> None:
    """api_call 兼容字符串 retcode("0") 与缺失 status。"""
    import sys as _sys
    sys.path.insert(0, "plugins/relationship")
    from relationship_core.utils import api_call

    class WS:
        def __init__(self, resp):
            self.resp = resp

        async def send_to_bot(self, *a, **kw):
            return self.resp

    # retcode 为字符串 "0"
    r = await api_call(WS({"status": "ok", "retcode": "0", "data": {"x": 1}}), "b", "get_group_info")
    assert r == {"x": 1}

    # 无 status 字段
    r = await api_call(WS({"retcode": 0, "data": [1, 2]}), "b", "get_group_list")
    assert r == [1, 2]

    # 失败
    r = await api_call(WS({"status": "ok", "retcode": 1, "wording": "failed"}), "b", "x")
    assert r is None

    # 非 dict 响应
    r = await api_call(WS(None), "b", "x")
    assert r is None
    print("[7] api_call retcode 兼容 OK")


async def test_empty_command_no_crash() -> None:
    """单独 "/" 不崩溃。"""
    import sys as _sys
    sys.path.insert(0, "plugins/relationship")
    from mohobot.interceptors.plugin_system import PluginSystem
    from mohobot.models.onebot import GroupMessageEvent, Sender

    tmp = tempfile.mkdtemp(prefix="pempty_")
    ps = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    await ps.load_plugins()
    meta = next(m for m in ps._plugins if m["name"] == "relationship")
    inst = meta["instance"]

    ev = GroupMessageEvent(
        time=0, self_id=1, post_type="message", message_type="group", message_id=1,
        user_id=1001, group_id=2001, sender=Sender(user_id=1001),
        message=[{"type": "text", "data": {"text": "/"}}],
    )
    handled, reply = await inst.on_message("bot_001", ev, {})
    assert handled is False, "单独 / 不应命中任何命令"
    print("[8] 空命令防护 OK")


async def main() -> None:
    await test_plugin_config_system()
    await test_admin_injection()
    await test_relationship_commands()
    await test_request_dispatch()
    await test_schema_coercion()
    await test_admin_hot_update()
    await test_api_call_retcode_string()
    await test_empty_command_no_crash()
    print("\nALL PLUGIN CONFIG / RELATIONSHIP TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
