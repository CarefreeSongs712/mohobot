"""chat_manager 插件测试 — /查看聊天(读 data/history 归档 + 合并转发) 与 /发送消息(纯文本代发)。

覆盖:
1. 非管理员一律拒绝(两个命令)
2. 群目标自动判定 + 取归档最近 N 条(正序, 署名取群名片/昵称)
3. 归档不足 N 条 → 全量转发
4. 无 group 归档时判定为私聊目标(private 归档)
5. 无本地归档 → 明确报错, 且全程不调历史查询接口
6. 转发到当前会话: 群 → send_group_forward_msg, 私聊 → send_private_forward_msg
7. 分批转发(按 batch_size 切批)
8. /发送消息 群目标(在群列表里) → 纯文本段(不解析 CQ 码)
9. /发送消息 私聊目标(不在群列表) → send_private_msg, 多行内容保留
10. 用法错误: 缺参/非数字目标/非正整数条数
11. get_group_list 失败 → 取消发送(不退化为私聊误发)
12. 非本插件命令/普通消息透传(不消费)
13. global_triggers 声明(群内多 bot 去重依据)
14. bot 自己发言(message_sent)也计入聊天记录
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 目录插件: 插件目录加入 sys.path, core 包才能被绝对导入
_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "chat_manager"
sys.path.insert(0, str(_PLUGIN_DIR))

# 与 PluginSystem 一致的方式加载 main.py(不污染 sys.modules)
_spec = importlib.util.spec_from_file_location("chat_manager_main", _PLUGIN_DIR / "main.py")
_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_main)
Plugin = _main.Plugin

from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent

BOT = "bot_001"
GROUP = 398870315
USER = 1234567
ADMIN = "10001"
CURRENT_GROUP = 999001


# ── 测试替身 ──────────────────────────────────────────────────

class FakeWS:
    """mock ws_server: 记录调用, 可配置群列表与指定失败的 action。"""

    def __init__(self, groups=None, fail_actions=(), raise_on_send=False):
        self.calls = []  # [(action, params)]
        self.groups = [] if groups is None else groups
        self.fail_actions = set(fail_actions)
        self.raise_on_send = raise_on_send

    async def send_to_bot(self, bot_id, action, params=None, wait_response=False, timeout=10.0):
        self.calls.append((action, params or {}))
        if action in self.fail_actions:
            return {"status": "failed", "retcode": 1, "message": "mock failure"}
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": self.groups}
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

    async def send_group_msg(self, bot_id, group_id, message):
        if self.raise_on_send:
            raise RuntimeError("mock send failure")
        self.calls.append(("send_group_msg", {"group_id": group_id, "message": message}))

    async def send_private_msg(self, bot_id, user_id, message):
        if self.raise_on_send:
            raise RuntimeError("mock send failure")
        self.calls.append(("send_private_msg", {"user_id": user_id, "message": message}))

    def actions(self):
        return [action for action, _ in self.calls]

    def params_of(self, action):
        return [params for act, params in self.calls if act == action]


# ── 事件 / 归档构造 ───────────────────────────────────────────

def _group_event(text, *, user_id=ADMIN, group_id=CURRENT_GROUP, card=""):
    return GroupMessageEvent.from_dict({
        "time": 1786185000, "self_id": 2192362623, "post_type": "message",
        "message_type": "group", "sub_type": "normal", "message_id": 1,
        "group_id": group_id, "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "sender": {"user_id": user_id, "nickname": "管理员", "card": card},
    })


def _private_event(text, *, user_id=ADMIN):
    return PrivateMessageEvent.from_dict({
        "time": 1786185000, "self_id": 2192362623, "post_type": "message",
        "message_type": "private", "sub_type": "friend", "message_id": 2,
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "sender": {"user_id": user_id, "nickname": "管理员"},
    })


def _history_event(user_id, text, mid, card="", nickname="昵称"):
    """一条与 data/history 归档一致的原始消息事件(JSONL 一行)。"""
    return {
        "time": 1786185000 + mid, "self_id": 2192362623, "post_type": "message",
        "message_type": "group", "sub_type": "normal", "message_id": mid,
        "group_id": GROUP, "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text, "font": 0,
        "sender": {"user_id": user_id, "nickname": nickname, "card": card},
    }


def _sent_event(text, mid, *, bot_qq=2192362623, nickname="洛天依"):
    """bot 自己发言的归档事件(由 WSServer 出站层写 message_sent)。"""
    return {
        "post_type": "message_sent", "message_type": "group",
        "time": 1786185000 + mid, "self_id": bot_qq, "user_id": bot_qq,
        "message_id": f"mid-{mid}", "group_id": GROUP,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": bot_qq, "nickname": nickname, "card": ""},
    }


def _write_history(data_dir, lines, *, chat_type="group", chat_id=None):
    """写归档文件 history/{BOT}/{group|private}/{id}.jsonl, 返回路径。"""
    target_id = GROUP if chat_id is None else chat_id
    path = Path(data_dir) / "history" / BOT / chat_type / f"{target_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _make_plugin(data_dir, ws, admins=(ADMIN,), config=None):
    """按框架注入契约装配插件实例。"""
    Plugin.inject_ws_server(ws)
    Plugin.inject_data_dir(str(data_dir))
    Plugin.inject_admin_ids(list(admins))
    plugin = Plugin()
    if config:
        plugin.on_config_update(config)
    return plugin


# ── 权限 ──────────────────────────────────────────────────────

async def test_non_admin_rejected():
    """非管理员发两个命令都被拒绝, 且不产生任何 API 调用。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, [_history_event(11, "消息1", 1)])
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws, admins=(ADMIN,))

        handled, reply = await plugin.on_message(
            BOT, _group_event(f"/查看聊天 {GROUP} 1", user_id=999), {})
        assert handled is True and "权限" in str(reply), reply

        handled, reply = await plugin.on_message(
            BOT, _group_event(f"/发送消息 {GROUP} 你好", user_id=999), {})
        assert handled is True and "权限" in str(reply), reply

    assert ws.calls == [], f"拒绝时不应有任何 API 调用: {ws.calls}"
    print("[1] 非管理员拒绝 OK")


# ── /查看聊天 ─────────────────────────────────────────────────

async def test_view_group_recent_uses_card_and_order():
    """群目标: 取最近 N 条正序转发到当前会话, 署名取群名片(空则昵称)。"""
    lines = [_history_event(11, f"消息{i}", i, card="") for i in range(1, 6)]
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, lines)
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws)
        handled, reply = await plugin.on_message(
            BOT, _group_event(f"/查看聊天 {GROUP} 2"), {})

    assert handled is True
    assert reply is None, f"转发即回复, 成功时不应补文字: {reply!r}"
    forwarded = ws.params_of("send_group_forward_msg")
    assert len(forwarded) == 1, f"应只调一次转发, 实际 {ws.actions()}"
    assert forwarded[0]["group_id"] == CURRENT_GROUP, "应转发到当前会话"
    nodes = forwarded[0]["messages"]
    assert len(nodes) == 2, "count=2 只转发最近 2 条"
    assert nodes[-1]["data"]["content"] == [{"type": "text", "data": {"text": "消息5"}}]
    assert nodes[0]["data"]["content"] == [{"type": "text", "data": {"text": "消息4"}}]
    assert nodes[-1]["data"]["uin"] == 11
    assert nodes[-1]["data"]["user_id"] == "11"
    assert nodes[-1]["data"]["name"] == "昵称", "card 为空应回退昵称"
    assert "get_group_msg_history" not in ws.actions()
    print("[2] 群目标最近 N 条 + 署名 OK")


async def test_view_count_exceeds_archive_returns_all():
    """归档不足 N 条 → 全量转发。"""
    lines = [_history_event(11, f"消息{i}", i) for i in range(1, 4)]
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, lines)
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws)
        await plugin.on_message(BOT, _group_event(f"/查看聊天 {GROUP} 99"), {})

    nodes = ws.params_of("send_group_forward_msg")[0]["messages"]
    assert len(nodes) == 3, f"归档仅 3 条应全量转发, 实际 {len(nodes)}"
    print("[3] 归档不足全量转发 OK")


async def test_view_private_archive_target():
    """无 group 归档 → 判定为私聊目标, 读 private 归档。"""
    lines = [_history_event(22, f"私聊{i}", i) for i in range(1, 4)]
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, lines, chat_type="private", chat_id=USER)
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws)
        await plugin.on_message(BOT, _group_event(f"/查看聊天 {USER} 5"), {})

    nodes = ws.params_of("send_group_forward_msg")[0]["messages"]
    assert len(nodes) == 3
    assert nodes[0]["data"]["content"] == [{"type": "text", "data": {"text": "私聊1"}}]
    print("[4] 私聊归档目标判定 OK")


async def test_view_group_archive_wins_over_private():
    """群号与 QQ 号撞号时群归档优先。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, [_history_event(11, "群消息", 1)])
        _write_history(tmp, [_history_event(22, "私聊消息", 1)],
                       chat_type="private", chat_id=GROUP)
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws)
        await plugin.on_message(BOT, _group_event(f"/查看聊天 {GROUP} 1"), {})

    nodes = ws.params_of("send_group_forward_msg")[0]["messages"]
    assert nodes[0]["data"]["content"] == [{"type": "text", "data": {"text": "群消息"}}]
    print("[5] 群归档优先 OK")


async def test_view_missing_archive_reports_error():
    """无本地归档 → 明确报错, 不转发也不调历史查询接口。"""
    with tempfile.TemporaryDirectory() as tmp:
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws)
        handled, reply = await plugin.on_message(
            BOT, _group_event("/查看聊天 777777"), {})

    assert handled is True
    assert "本地没有" in str(reply), reply
    assert ws.calls == [], f"不应有任何 API 调用: {ws.actions()}"
    print("[6] 无归档报错 OK")


async def test_view_in_private_uses_private_forward():
    """在私聊里查看 → 走 send_private_forward_msg 发回该私聊。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, [_history_event(11, "消息1", 1)])
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws)
        await plugin.on_message(BOT, _private_event(f"/查看聊天 {GROUP} 1"), {})

    forwarded = ws.params_of("send_private_forward_msg")
    assert len(forwarded) == 1, f"实际 {ws.actions()}"
    assert forwarded[0]["user_id"] == int(ADMIN)
    assert "send_group_forward_msg" not in ws.actions()
    print("[7] 私聊会话转发 OK")


async def test_view_batches_by_batch_size():
    """条数超过 batch_size → 分批转发。"""
    lines = [_history_event(11, f"消息{i}", i) for i in range(1, 6)]
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, lines)
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws, config={"batch_size": 2})
        await plugin.on_message(BOT, _group_event(f"/查看聊天 {GROUP} 5"), {})

    batches = ws.params_of("send_group_forward_msg")
    assert [len(b["messages"]) for b in batches] == [2, 2, 1], \
        f"5 条按每批 2 条应切成 2/2/1, 实际 {[len(b['messages']) for b in batches]}"
    print("[8] 分批转发 OK")


async def test_view_forward_failure_reports_error():
    """转发接口失败 → 回错误提示(不静默)。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, [_history_event(11, "消息1", 1)])
        ws = FakeWS(fail_actions=("send_group_forward_msg",))
        plugin = _make_plugin(tmp, ws)
        handled, reply = await plugin.on_message(
            BOT, _group_event(f"/查看聊天 {GROUP} 1"), {})

    assert handled is True
    assert "转发失败" in str(reply), reply
    print("[9] 转发失败报错 OK")


# ── /发送消息 ─────────────────────────────────────────────────

async def test_send_to_group_uses_plain_text_segment():
    """群目标(在群列表里) → send_group_msg, 内容是文本段(不解析 CQ 码)。"""
    with tempfile.TemporaryDirectory() as tmp:
        ws = FakeWS(groups=[{"group_id": GROUP, "group_name": "测试群"}])
        plugin = _make_plugin(tmp, ws)
        content = "大家好[CQ:face,id=1]"
        handled, reply = await plugin.on_message(
            BOT, _group_event(f"/发送消息 {GROUP} {content}"), {})

    assert handled is True
    assert "✅" in str(reply), reply
    sends = ws.params_of("send_group_msg")
    assert len(sends) == 1, f"实际 {ws.actions()}"
    assert sends[0]["group_id"] == GROUP
    assert sends[0]["message"] == [
        {"type": "text", "data": {"text": content}}
    ], "必须以文本段发送, 避免协议端把 [CQ:..] 当 CQ 码解析"
    print("[10] 群目标纯文本段发送 OK")


async def test_send_to_private_when_not_in_group_list():
    """不在群列表里 → 判定为私聊, 走 send_private_msg; 多行内容保留。"""
    with tempfile.TemporaryDirectory() as tmp:
        ws = FakeWS(groups=[{"group_id": 111111}])
        plugin = _make_plugin(tmp, ws)
        content = "第一行\n第二行"
        handled, reply = await plugin.on_message(
            BOT, _group_event(f"/发送消息 {USER} {content}"), {})

    assert handled is True and "✅" in str(reply), reply
    sends = ws.params_of("send_private_msg")
    assert len(sends) == 1, f"实际 {ws.actions()}"
    assert sends[0]["user_id"] == USER
    assert sends[0]["message"][0]["data"]["text"] == content, "内部换行应保留"
    print("[11] 私聊目标 + 多行内容 OK")


async def test_send_group_list_failure_aborts():
    """群列表接口失败 → 取消发送, 不误发私聊。"""
    with tempfile.TemporaryDirectory() as tmp:
        ws = FakeWS(fail_actions=("get_group_list",))
        plugin = _make_plugin(tmp, ws)
        handled, reply = await plugin.on_message(
            BOT, _group_event(f"/发送消息 {USER} 你好"), {})

    assert handled is True
    assert "取消发送" in str(reply), reply
    assert "send_group_msg" not in ws.actions()
    assert "send_private_msg" not in ws.actions()
    print("[12] 群列表失败取消发送 OK")


async def test_send_failure_reports_error():
    """发送抛错 → 回错误提示。"""
    with tempfile.TemporaryDirectory() as tmp:
        ws = FakeWS(groups=[], raise_on_send=True)
        plugin = _make_plugin(tmp, ws)
        handled, reply = await plugin.on_message(
            BOT, _group_event(f"/发送消息 {USER} 你好"), {})

    assert handled is True
    assert "发送失败" in str(reply), reply
    print("[13] 发送失败报错 OK")


# ── 参数校验与透传 ────────────────────────────────────────────

async def test_usage_errors():
    """缺参/非数字目标/非正整数条数 → 明确提示, 无副作用。"""
    cases = [
        (f"/查看聊天", "用法"),
        (f"/查看聊天 {GROUP} abc", "正整数"),
        (f"/查看聊天 {GROUP} 0", "正整数"),
        (f"/查看聊天 abc", "纯数字"),
        (f"/查看聊天 {GROUP} 1 extra", "参数过多"),
        (f"/发送消息", "用法"),
        (f"/发送消息 {USER}", "用法"),
        (f"/发送消息 abc 你好", "纯数字"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for text, expect in cases:
            ws = FakeWS()
            plugin = _make_plugin(tmp, ws)
            handled, reply = await plugin.on_message(BOT, _group_event(text), {})
            assert handled is True, f"{text!r} 应被消费"
            assert expect in str(reply), f"{text!r} → {reply!r} (期望含 {expect!r})"
            assert ws.calls == [], f"{text!r} 不应有 API 调用: {ws.actions()}"
    print("[14] 参数校验 OK")


async def test_other_commands_pass_through():
    """非本插件命令与普通消息 → 不消费(交给后续拦截器/LLM)。"""
    with tempfile.TemporaryDirectory() as tmp:
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws)
        for text in ("/群列表", "/查看记录 1", "你好", ""):
            handled, reply = await plugin.on_message(BOT, _group_event(text), {})
            assert handled is False, f"{text!r} 不应被 chat_manager 消费"
            assert reply is None
    print("[15] 非本插件命令透传 OK")


async def test_global_triggers_declared():
    """两个命令都声明为全局指令(群内多 bot 去重依据)。"""
    assert set(Plugin.global_triggers) == {"查看聊天", "发送消息"}
    print("[16] global_triggers 声明 OK")


async def test_view_includes_bot_sent_messages():
    """bot 自己发言(message_sent)也计入记录, 署名取 bot 昵称。"""
    lines = [
        _history_event(11, "用户说", 1),
        _sent_event("bot 答", 2),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, lines)
        ws = FakeWS()
        plugin = _make_plugin(tmp, ws)
        await plugin.on_message(BOT, _group_event(f"/查看聊天 {GROUP} 5"), {})

    nodes = ws.params_of("send_group_forward_msg")[0]["messages"]
    assert len(nodes) == 2, f"收到 + bot 发言都应计入, 实际 {len(nodes)}"
    assert nodes[0]["data"]["content"] == [{"type": "text", "data": {"text": "用户说"}}]
    assert nodes[1]["data"]["content"] == [{"type": "text", "data": {"text": "bot 答"}}]
    assert nodes[1]["data"]["name"] == "洛天依", "bot 发言署名应取昵称(card 为空)"
    print("[17] bot 自己发言计入记录 OK")


# ── 独立运行 ──────────────────────────────────────────────────

async def main():
    await test_non_admin_rejected()
    await test_view_group_recent_uses_card_and_order()
    await test_view_count_exceeds_archive_returns_all()
    await test_view_private_archive_target()
    await test_view_group_archive_wins_over_private()
    await test_view_missing_archive_reports_error()
    await test_view_in_private_uses_private_forward()
    await test_view_batches_by_batch_size()
    await test_view_forward_failure_reports_error()
    await test_send_to_group_uses_plain_text_segment()
    await test_send_to_private_when_not_in_group_list()
    await test_send_group_list_failure_aborts()
    await test_send_failure_reports_error()
    await test_usage_errors()
    await test_other_commands_pass_through()
    await test_global_triggers_declared()
    await test_view_includes_bot_sent_messages()
    print("\nALL CHAT_MANAGER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
