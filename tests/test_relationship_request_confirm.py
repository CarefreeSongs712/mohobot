"""Issue #151 回归: 审批结果确认。

缺陷:
1. 自动审批(好友/群)拿到失败结果后只记日志, 仍向申请人发送 decision 预置的
   "已自动同意/拒绝" 完成文案, 也不会转人工审批。
2. ``api_call_ex`` 只排除 failed/error/fail 三个 status; retcode 缺失或无法解析
   时默认 0, ``int()`` 还会截断小数 —— 畸形回执被判成功, /拉黑 显示完成并写
   本地名单。
3. 超时/断连返回 None 被写成 "响应格式异常", 与"回执非法"无法区分。

修复后: 三态结果(确认成功/明确失败/未取得确认), 自动审批失败转人工且不补发
完成文案, 手动审批区分"审批失败"与"审批未确认"。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins" / "relationship"))

from relationship_core.utils import (  # noqa: E402
    ERROR_TEXT_LIMIT,
    OUTCOME_FAILED,
    OUTCOME_OK,
    OUTCOME_UNCONFIRMED,
    api_call,
    api_call_checked,
    summarize_error,
)

GROUP_FLAG = "invite:1102231856:u_nMR0Q-RVI1foRyUAGQHT7Q"
FRIEND_FLAG = "friend:u_test"


def _load_plugin_main():
    path = ROOT / "plugins" / "relationship" / "main.py"
    spec = importlib.util.spec_from_file_location("relationship_plugin_main_confirm", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _WS:
    """记录 OneBot 调用, set_*_add_request 的返回可由测试指定。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[tuple] = []
        self.cards: dict[str, str] = {}
        self.approve_response = {"status": "ok", "retcode": 0, "data": None}

    async def send_to_bot(self, bot_id, action, params=None, **kw):
        self.calls.append((action, params or {}))
        if action in ("set_friend_add_request", "set_group_add_request"):
            return self.approve_response
        if action == "get_msg":
            mid = str((params or {}).get("message_id"))
            return {
                "status": "ok", "retcode": 0,
                "data": {"message": [{"type": "text", "data": {"text": self.cards.get(mid, "")}}]},
            }
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": []}
        if action == "get_friend_list":
            return {"status": "ok", "retcode": 0, "data": []}
        if action == "get_stranger_info":
            return {"status": "ok", "retcode": 0, "data": {"nickname": "申请人"}}
        if action == "get_group_info":
            return {"status": "ok", "retcode": 0, "data": {"group_name": "测试群"}}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send_group_msg(self, bot_id, group_id, text):
        self.sent.append(("group", str(group_id), text))

    async def send_private_msg(self, bot_id, user_id, text):
        self.sent.append(("private", str(user_id), text))


def _prepare(td, ws, request_cfg=None):
    module = _load_plugin_main()
    module.Plugin.inject_data_dir(str(td))
    module.Plugin.inject_ws_server(ws)
    module.Plugin.inject_admin_ids(["7777"])
    plugin = module.Plugin()
    plugin.plugin_config = {
        "manage_group": "100",
        "manage_users": [],
        "request": dict(request_cfg or {}),
    }
    return module, plugin


def _approve_event(reply_id: str, text: str = "/同意"):
    return SimpleNamespace(
        user_id=7777,
        message=[
            {"type": "reply", "data": {"id": reply_id}},
            {"type": "text", "data": {"text": text}},
        ],
    )


async def _friend_request(plugin):
    raw = {
        "post_type": "request", "request_type": "friend", "user_id": 2002,
        "flag": FRIEND_FLAG, "comment": "求好友",
    }
    assert await plugin.on_request("bot_001", SimpleNamespace(), raw)


async def _group_invite(plugin, ws):
    raw = {
        "post_type": "request", "request_type": "group", "sub_type": "invite",
        "user_id": 3831097597, "group_id": 1102231856, "flag": GROUP_FLAG,
        "comment": "拉群",
    }
    assert await plugin.on_request("bot_001", SimpleNamespace(), raw)
    card = [t for _, _, t in ws.sent if "【群邀请】" in t][-1]
    ws.cards["543195637"] = card
    return card, "543195637"


def _sent_texts(ws, kind=None):
    return [t for (k, _, t) in ws.sent if kind is None or k == kind]


# ── 1. 自动审批结果确认 ─────────────────────────────────────

async def test_auto_friend_agree_failure_does_not_claim_success():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        ws.approve_response = {"status": "failed", "retcode": 100, "wording": "权限不足"}
        _, plugin = _prepare(td, ws, {"auto_agree_friend": True})
        await _friend_request(plugin)

        private = _sent_texts(ws, "private")
        assert any("自动处理失败" in t for t in private), private
        assert not any("已自动同意" in t for t in private), private
        # 失败要转人工: 管理员侧必须看到失败原因与人工复核提示
        admin = _sent_texts(ws, "group")
        assert any("自动处理失败" in t and "人工" in t for t in admin), admin
        assert not any("自动处理：已自动同意" in t for t in admin), admin


async def test_auto_friend_agree_unconfirmed_is_not_success():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        ws.approve_response = None  # 超时/断连: 宿主返回 None
        _, plugin = _prepare(td, ws, {"auto_agree_friend": True})
        await _friend_request(plugin)

        private = " ".join(_sent_texts(ws, "private"))
        assert "未确认" in private, private
        assert "已自动同意" not in private, private
        admin = " ".join(_sent_texts(ws, "group"))
        assert "未确认" in admin and "人工" in admin, admin


async def test_auto_group_reject_failure_does_not_claim_success():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        ws.approve_response = {"status": "failed", "retcode": 0, "message": "协议端拒绝"}
        _, plugin = _prepare(td, ws, {"auto_reject_group": True})
        raw = {
            "post_type": "request", "request_type": "group", "sub_type": "invite",
            "user_id": 3831097597, "group_id": 1102231856, "flag": GROUP_FLAG,
            "comment": "拉群",
        }
        assert await plugin.on_request("bot_001", SimpleNamespace(), raw)

        private = " ".join(_sent_texts(ws, "private"))
        assert "自动处理失败" in private, private
        assert "已自动拒绝" not in private, private


async def test_auto_approve_confirmed_still_sends_completion():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        _, plugin = _prepare(td, ws, {"auto_agree_friend": True})
        await _friend_request(plugin)

        private = " ".join(_sent_texts(ws, "private"))
        assert "已自动同意好友请求" in private, private
        assert "失败" not in private, private


# ── 2. 手动审批回执判定 ─────────────────────────────────────

async def test_manual_malformed_receipt_is_unconfirmed_and_no_blacklist():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        _, plugin = _prepare(td, ws)
        _, mid = await _group_invite(plugin, ws)
        ws.approve_response = {"status": "unknown", "retcode": "invalid"}

        _, reply = await plugin.on_message("bot_001", _approve_event(mid, "/拉黑"), {})

        # 畸形回执不能断定协议端拒绝(可能已执行), 只能要求人工确认(#151 review)。
        assert reply and reply.startswith("❌ 审批未确认"), reply
        assert "回执异常" in reply, reply
        assert "已拉黑" not in reply, reply
        data = Path(td) / "plugins_config" / "relationship.json"
        assert not data.exists() or "1102231856" not in data.read_text(encoding="utf-8")


async def test_manual_no_receipt_reports_unconfirmed_not_format_error():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        _, plugin = _prepare(td, ws)
        _, mid = await _group_invite(plugin, ws)
        ws.approve_response = None

        _, reply = await plugin.on_message("bot_001", _approve_event(mid, "/同意"), {})

        assert reply and reply.startswith("❌ 审批未确认"), reply
        assert "未取得协议端确认" in reply, reply
        assert "响应格式异常" not in reply, reply
        assert "已同意" not in reply, reply


async def test_manual_valid_receipt_succeeds():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        _, plugin = _prepare(td, ws)
        _, mid = await _group_invite(plugin, ws)

        _, reply = await plugin.on_message("bot_001", _approve_event(mid, "/同意"), {})

        assert reply and reply.startswith("已同意群邀请"), reply


# ── 3. api_call_checked 严格判定 ────────────────────────────

class _StaticWS:
    def __init__(self, resp):
        self.resp = resp

    async def send_to_bot(self, *a, **kw):
        return self.resp


async def test_retcode_valid_forms_are_ok():
    # 不用 pytest 参数化, 兼容 tests/_run_all.py 的无 fixture 执行器。
    for retcode in (0, "0", "+0"):
        ws = _StaticWS({"status": "ok", "retcode": retcode, "data": {"x": 1}})
        result = await api_call_checked(ws, "b", "get_group_info")
        assert result.ok and result.outcome == OUTCOME_OK, retcode
        assert result.data == {"x": 1}, retcode


async def test_missing_status_is_compatible():
    ws = _StaticWS({"retcode": 0, "data": [1, 2]})
    result = await api_call_checked(ws, "b", "get_group_list")
    assert result.ok and result.data == [1, 2]
    assert await api_call(ws, "b", "get_group_list") == [1, 2]


MALFORMED_RECEIPTS = [
    {"status": "ok", "data": {}},            # 缺 retcode
    {"status": "ok", "retcode": "abc"},      # 无法解析
    {"status": "ok", "retcode": None},       # 无值
    {"status": "ok", "retcode": 0.0},        # 小数即使为 0 也不接受
    {"status": "ok", "retcode": True},       # bool 不是合法 retcode
    {"status": "unknown", "retcode": 0},     # 未知 status
    {"status": "ok", "retcode": "0.5"},      # 数字串但非整数
]


async def test_malformed_receipts_are_unconfirmed():
    for resp in MALFORMED_RECEIPTS:
        result = await api_call_checked(_StaticWS(resp), "b", "x")
        assert not result.ok, resp
        assert result.outcome == OUTCOME_UNCONFIRMED, resp
        assert result.error, resp


async def test_explicit_rejection_is_failed_not_unconfirmed():
    ws = _StaticWS({"status": "failed", "retcode": 100, "wording": "invalid flag"})
    result = await api_call_checked(ws, "b", "x")
    assert not result.ok
    assert result.outcome == OUTCOME_FAILED
    assert "invalid flag" in result.error


async def test_timeout_and_missing_ws_are_unconfirmed():
    assert (await api_call_checked(None, "b", "x")).outcome == OUTCOME_UNCONFIRMED
    result = await api_call_checked(_StaticWS(None), "b", "x")
    assert result.outcome == OUTCOME_UNCONFIRMED
    assert "未取得协议端确认" in result.error


async def test_non_dict_receipt_is_unconfirmed():
    result = await api_call_checked(_StaticWS([1, 2]), "b", "x")
    assert result.outcome == OUTCOME_UNCONFIRMED
    assert "回执异常" in result.error


def test_error_text_is_limited_and_single_line():
    text = "第一行\n第二行 " + "x" * (ERROR_TEXT_LIMIT * 2)
    summary = summarize_error(text)
    assert "\n" not in summary
    assert len(summary) <= ERROR_TEXT_LIMIT
