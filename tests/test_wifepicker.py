"""wifepicker 抽老婆插件测试:
1. 插件加载 + schema + 数据存储(合并 JSON)
2. 活跃池记录(观察钩子) + 每日次数限制
3. /今日老婆 抽取(群成员过滤 + 头像)
4. /强娶 冷却 + @ 解析 + 排除列表
5. 求婚交互: 发起 → 同意 → 双方记录+冷却; 拒绝 → 强娶确认
6. 管理员命令门控
7. 关键词触发开关 + 匹配模式
8. 关系图文本降级(无 playwright)
9. 观察钩子不消费普通消息
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender

BOT = "bot_001"
GROUP = "888888"
MEMBERS = [
    {"user_id": 1001, "card": "阿绫", "nickname": "luo"},
    {"user_id": 1002, "card": "天依", "nickname": "tian"},
    {"user_id": 1003, "card": "", "nickname": "墨清弦"},
]


class FakeWS:
    """mock ws_server: 记录发送 + mock API 响应。"""

    def __init__(self):
        self.sent = []
        self.sent_images = []
        self.bot_qq = 1000

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        if action == "get_group_member_list":
            return {"status": "ok", "retcode": 0, "data": MEMBERS}
        if action == "get_group_info":
            return {"status": "ok", "retcode": 0, "data": {"group_name": "测试群"}}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send_group_msg(self, bot_id, group_id, message):
        self.sent.append(("group", group_id, message))

    async def send_private_msg(self, bot_id, user_id, message):
        self.sent.append(("private", user_id, message))

    async def send_image(self, bot_id, chat_type, chat_id, image_path):
        self.sent_images.append((chat_type, chat_id, image_path))


def make_group_event(user_id, text, self_id=1000):
    return GroupMessageEvent(
        time=0, self_id=self_id, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=int(GROUP),
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def make_group_event_with_at(user_id, text, at_qq):
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=int(GROUP),
        sender=Sender(user_id=user_id),
        message=[
            {"type": "text", "data": {"text": text}},
            {"type": "at", "data": {"qq": str(at_qq)}},
        ],
    )


async def make_plugin(tmp, config_extra=None):
    import sys as _sys
    _sys.path.insert(0, "plugins/wifepicker")
    from main import Plugin

    inst = Plugin()
    inst.__class__.inject_ws_server(FakeWS())
    inst.__class__.inject_data_dir(tmp)
    inst.__class__.inject_admin_ids(["1001"])
    base = {
        "daily_limit": 1,
        "force_marry_cd": 3,
        "propose_cooldown_minutes": 60,
        "max_records": 500,
        "active_user_days": 30,
        "excluded_users": [],
        "force_marry_excluded_users": [],
        "keyword_trigger_enabled": False,
        "keyword_trigger_mode": "exact",
        "allow_marry_bot": False,
        "at_waifu": False,
        "auto_set_other_half": False,
        "whitelist_groups": [],
        "blacklist_groups": [],
        "iterations": 140,
    }
    base.update(config_extra or {})
    inst.plugin_config = base
    return inst


async def test_load_and_store() -> None:
    """插件加载 + 合并 JSON 数据初始化。"""
    from mohobot.interceptors.plugin_system import PluginSystem

    tmp = tempfile.mkdtemp(prefix="wife_")
    ps = PluginSystem(plugins_dir="plugins", data_dir=tmp)
    ps.set_admin_ids([1001])
    await ps.load_plugins()
    meta = next(m for m in ps._plugins if m["name"] == "wifepicker")
    assert meta["loaded"] is True, meta.get("error")

    schema = meta["config_schema"]
    assert schema["daily_limit"]["default"] == 1
    assert schema["propose_cooldown_minutes"]["default"] == 60

    inst = meta["instance"]
    assert set(inst.store.data.keys()) == {
        "records", "active_users", "forced_marriage", "marriage_actions", "rbq_stats",
    }
    # 存档生成
    archive = Path(tmp) / "plugins_config" / "wifepicker.json"
    assert archive.exists()
    print("[1] 插件加载 + schema + 数据初始化 OK")


async def test_observe_records_active_and_passthrough() -> None:
    """观察钩子: 记录活跃但不消费普通消息。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp)

    ev = make_group_event(2001, "大家好啊")
    handled, reply = await inst.on_message_observed(BOT, ev, {})
    assert handled is False and reply is None, "普通消息不应被消费"
    assert inst.store.active_users[GROUP]["2001"] > 0, "应记录活跃"

    # 机器人自己/ID 0 不入池
    ev2 = make_group_event(1000, "hi", self_id=1000)
    await inst.on_message_observed(BOT, ev2, {})
    assert "1000" not in inst.store.active_users[GROUP]
    print("[2] 观察钩子活跃记录 + 普通消息透传 OK")


async def test_draw_wife_daily_limit() -> None:
    """/今日老婆: 抽取 + 每日次数限制 + 头像。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp)
    ws = inst._ws_server

    # 活跃池: 1001/1002/1003 发言
    for uid in (1001, 1002, 1003):
        ev = make_group_event(uid, "发言")
        await inst.on_message_observed(BOT, ev, {})

    # user 2001 抽老婆
    ev = make_group_event(2001, "/今日老婆")
    handled, reply = await inst.on_message(BOT, ev, {})
    assert handled is True
    assert isinstance(reply, list), f"应返回消息段列表: {reply}"
    text = "".join(s.get("data", {}).get("text", "") for s in reply if s["type"] == "text")
    assert "你的今日老婆是" in text, text
    # 头像图片段
    images = [s for s in reply if s["type"] == "image"]
    assert images and "q4.qlogo.cn" in images[0]["data"]["file"]

    # 记录已保存(抽取者记录)
    assert len(inst.store.records["groups"][GROUP]["records"]) == 1

    # 同一用户再抽 → 达上限提示
    ev2 = make_group_event(2001, "/今日老婆")
    handled2, reply2 = await inst.on_message(BOT, ev2, {})
    assert handled2 and isinstance(reply2, list)
    text2 = "".join(s.get("data", {}).get("text", "") for s in reply2 if s["type"] == "text")
    assert "已经有老婆了" in text2, text2
    print("[3] 抽取 + 每日限制 + 头像 OK")


async def test_force_marry() -> None:
    """/强娶: @ 解析 + 冷却 + 排除列表。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp)
    ws = inst._ws_server

    # 强娶 @1002(非管理员用户 2001)
    ev = make_group_event_with_at(2001, "/强娶", 1002)
    handled, reply = await inst.on_message(BOT, ev, {})
    assert handled is True
    text = "".join(s.get("data", {}).get("text", "") for s in reply if s["type"] == "text")
    assert "强娶了" in text and "天依" in text, text
    assert len(inst.store.records["groups"][GROUP]["records"]) == 1
    assert inst.store.rbq_stats[GROUP]["1002"], "rbq 统计应记录"

    # 立即再强娶 → 冷却
    ev2 = make_group_event_with_at(2001, "/强娶", 1003)
    handled2, reply2 = await inst.on_message(BOT, ev2, {})
    text2 = reply2 if isinstance(reply2, str) else ""
    assert "强娶过" in text2, text2

    # 娶自己(新用户 3001, 无冷却)
    ev3 = make_group_event_with_at(3001, "/强娶", 3001)
    _, reply3 = await inst.on_message(BOT, ev3, {})
    assert "不能娶自己" in (reply3 or "")

    # 排除列表(新用户 4001, 无冷却)
    inst.plugin_config["force_marry_excluded_users"] = ["1002"]
    ev4 = make_group_event_with_at(4001, "/强娶", 1002)
    _, reply4 = await inst.on_message(BOT, ev4, {})
    assert "排除列表" in (reply4 or "")
    print("[4] 强娶 + 冷却 + 排除 OK")


async def test_propose_flow() -> None:
    """求婚: 发起 → 同意 → 双方记录 + 冷却。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp)

    # 2001 向 1002 求婚
    ev = make_group_event_with_at(2001, "/求婚", 1002)
    handled, reply = await inst.on_message(BOT, ev, {})
    assert handled and "发起了求婚" in reply, reply
    assert GROUP in inst._propose_requests and "1002" in inst._propose_requests[GROUP]

    # 1002 回复"同意"(观察钩子捕获, 无需 @)
    ev2 = make_group_event(1002, "同意")
    handled2, reply2 = await inst.on_message_observed(BOT, ev2, {})
    assert handled2 is True, "同意应被消费"
    assert "结为夫妻" in reply2, reply2
    # 双方都有记录
    recs = inst.store.records["groups"][GROUP]["records"]
    uids = {r["user_id"] for r in recs}
    assert uids == {"2001", "1002"}
    # 双方都在求婚冷却中
    assert inst.store.marriage_actions[GROUP].get("2001")
    assert inst.store.marriage_actions[GROUP].get("1002")
    # 请求已清除
    assert not inst._propose_requests.get(GROUP)
    print("[5] 求婚发起→同意→记录+冷却 OK")


async def test_propose_refuse_then_force() -> None:
    """求婚: 拒绝 → 强娶确认 → 强娶。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp)

    # 2001 向 1002 求婚
    ev = make_group_event_with_at(2001, "/求婚", 1002)
    await inst.on_message(BOT, ev, {})

    # 1002 拒绝 → 确认强娶提示
    ev2 = make_group_event(1002, "拒绝")
    handled2, reply2 = await inst.on_message_observed(BOT, ev2, {})
    assert handled2 and "强娶" in str(reply2), reply2
    assert GROUP in inst._force_confirm_requests and "2001" in inst._force_confirm_requests[GROUP]

    # 2001 回复"是" → 执行强娶(目标 1002)
    ev3 = make_group_event(2001, "是")
    handled3, reply3 = await inst.on_message_observed(BOT, ev3, {})
    assert handled3 is True, "强娶确认应被消费"
    text3 = "".join(s.get("data", {}).get("text", "") for s in reply3) if isinstance(reply3, list) else str(reply3)
    assert "强娶了" in text3, text3
    recs = inst.store.records["groups"][GROUP]["records"]
    assert recs[0]["wife_id"] == "1002"
    assert not inst._force_confirm_requests.get(GROUP)
    print("[6] 求婚拒绝→强娶确认 OK")


async def test_admin_commands() -> None:
    """管理员命令门控。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp)

    # 非管理员 → 拒绝
    ev = make_group_event(2001, "/重置记录")
    handled, reply = await inst.on_message(BOT, ev, {})
    assert handled and "没有权限" in reply

    # 管理员 1001 → 执行
    ev2 = make_group_event(1001, "/重置记录")
    handled2, reply2 = await inst.on_message(BOT, ev2, {})
    assert handled2 and "已重置" in reply2
    assert inst.store.records["groups"] == {}

    # 重置强娶时间(空群 → 提示)
    ev3 = make_group_event(1001, "/重置强娶时间")
    _, reply3 = await inst.on_message(BOT, ev3, {})
    assert "没有人在冷却" in reply3

    # 未知 / 命令不消费
    ev4 = make_group_event(1001, "/不存在命令")
    handled4, _ = await inst.on_message(BOT, ev4, {})
    assert handled4 is False
    print("[7] 管理员命令门控 OK")


async def test_keyword_trigger() -> None:
    """关键词触发开关 + 匹配模式。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp, {"keyword_trigger_enabled": True})
    ws = inst._ws_server

    for uid in (1001, 1002, 1003):
        await inst.on_message_observed(BOT, make_group_event(uid, "发言"), {})

    # 直接发 jrlp(无 / 前缀) → 触发抽老婆
    ev = make_group_event(2001, "jrlp")
    handled, reply = await inst.on_message_observed(BOT, ev, {})
    assert handled is True, "关键词应触发"
    assert isinstance(reply, list)

    # 中文关键词
    ev2 = make_group_event(2001, "抽老婆")
    handled2, reply2 = await inst.on_message_observed(BOT, ev2, {})
    assert handled2 and "已经有老婆了" in str(reply2)

    # 开关关闭 → 不触发
    inst.plugin_config["keyword_trigger_enabled"] = False
    ev3 = make_group_event(2001, "jrlp")
    handled3, _ = await inst.on_message_observed(BOT, ev3, {})
    assert handled3 is False
    print("[8] 关键词触发 OK")


async def test_graph_text_fallback() -> None:
    """关系图: 无记录提示 + 文本降级(无 playwright 环境)。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp)

    # 无记录
    ev = make_group_event(2001, "/关系图")
    _, reply = await inst.on_message(BOT, ev, {})
    assert "还没有任何老婆记录" in reply

    # 造两条记录
    recs = inst.store.records.setdefault("groups", {}).setdefault(GROUP, {}).setdefault("records", [])
    recs.append({"user_id": "2001", "wife_id": "1002", "wife_name": "天依", "timestamp": "2026-08-09T12:00:00", "forced": True})
    recs.append({"user_id": "2002", "wife_id": "1003", "wife_name": "墨清弦", "timestamp": "2026-08-09T12:01:00"})
    await inst.store.flush(force=True)

    ev2 = make_group_event(2001, "/关系图")
    _, reply2 = await inst.on_message(BOT, ev2, {})
    # 无 playwright → 文本降级
    from wifepicker_core.renderer import available
    if not available():
        assert isinstance(reply2, str) and "文本版" in reply2, reply2
        assert "天依" in reply2 and "强娶" in reply2
    print("[9] 关系图文本降级 OK")


async def test_propose_private_rejected() -> None:
    """私聊不发求婚。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp)

    ev = PrivateMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="private",
        message_id=1, user_id=2001, sender=Sender(user_id=2001),
        message=[{"type": "text", "data": {"text": "/求婚 @1002"}}],
    )
    handled, reply = await inst.on_message(BOT, ev, {})
    assert handled and "群聊" in reply
    print("[10] 私聊求婚拒绝 OK")


async def test_concurrent_commands() -> None:
    """6 用户并发抽老婆/强娶 — 不丢记录不崩溃。"""
    tmp = tempfile.mkdtemp(prefix="wife_")
    inst = await make_plugin(tmp)

    # 活跃池(6 个候选)
    for uid in (1001, 1002, 1003, 5001, 5002, 5003):
        await inst.on_message_observed(BOT, make_group_event(uid, "发言"), {})

    async def draw(i: int) -> None:
        uid = 7000 + i
        ev = make_group_event(uid, "/今日老婆")
        handled, reply = await inst.on_message(BOT, ev, {})
        assert handled is True
        return uid

    async def marry(i: int) -> None:
        uid = 8000 + i
        target = 1001 + i % 3
        ev = make_group_event_with_at(uid, "/强娶", target)
        handled, reply = await inst.on_message(BOT, ev, {})
        assert handled is True
        text = "".join(s.get("data", {}).get("text", "") for s in reply) if isinstance(reply, list) else str(reply)
        assert "强娶了" in text, f"用户{uid} 强娶失败: {text}"
        return uid

    results = await asyncio.gather(
        *[draw(i) for i in range(6)],
        *[marry(i) for i in range(6)],
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"并发异常: {errors}"

    # 6 人抽取记录 + 6 人强娶记录, 无丢失
    recs = inst.store.records["groups"][GROUP]["records"]
    assert len(recs) == 12, f"应 12 条记录, 实际 {len(recs)}"
    draw_uids = {str(r["user_id"]) for r in recs if r.get("forced")}
    marry_uids = {str(r["user_id"]) for r in recs if not r.get("forced")}
    assert len(draw_uids) == 6 and len(marry_uids) == 6, f"记录错乱: {recs}"
    # 数据落盘且合法
    saved = json.loads((Path(tmp) / "plugins_data" / "wifepicker" / "data.json").read_text(encoding="utf-8"))
    assert len(saved["records"]["groups"][GROUP]["records"]) == 12
    print("[11] 6 bot 并发抽老婆/强娶 OK")


async def test_all() -> None:
    await test_load_and_store()
    await test_observe_records_active_and_passthrough()
    await test_draw_wife_daily_limit()
    await test_force_marry()
    await test_propose_flow()
    await test_propose_refuse_then_force()
    await test_admin_commands()
    await test_keyword_trigger()
    await test_graph_text_fallback()
    await test_propose_private_rejected()
    await test_concurrent_commands()
    print("\nALL WIFEPICKER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(test_all())
