"""群邀请审批回归: 协议端 flag 必须原样回传, 审批失败不得假报成功。

缺陷: 审批卡片把服务端下发的 flag 也渲染进文本, 而 ``_sanitize_value`` 会把
所有冒号替换成 ``、``。snowluma 的群邀请 flag 形如
``invite:1102231856:u_nMR0Q-...``, 管理员引用卡片回复 /同意 时, 插件从卡片
文本重建请求, 回传的 flag 已被破坏, 协议端报 invalid group request flag,
邀请永远无法通过。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GROUP_FLAG = "invite:1102231856:u_nMR0Q-RVI1foRyUAGQHT7Q"


def _load_plugin_main():
    path = ROOT / "plugins" / "relationship" / "main.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("relationship_plugin_main_flag", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _WS:
    """记录 OneBot 调用, 模拟协议端返回; get_msg 返回插件发出的审批卡片。"""

    def __init__(self):
        self.calls = []
        self.sent = []
        self.cards = {}
        self.fail_set_group = None

    async def send_to_bot(self, bot_id, action, params=None, **kw):
        self.calls.append((action, params or {}))
        if action == "set_group_add_request" and self.fail_set_group:
            return self.fail_set_group
        if action == "get_msg":
            mid = str((params or {}).get("message_id"))
            text = self.cards.get(mid, "")
            return {"status": "ok", "retcode": 0,
                    "data": {"message": [{"type": "text", "data": {"text": text}}]}}
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": []}
        if action == "get_stranger_info":
            return {"status": "ok", "retcode": 0, "data": {"nickname": "邀请人"}}
        if action == "get_group_info":
            return {"status": "ok", "retcode": 0, "data": {"group_name": "测试群"}}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send_group_msg(self, bot_id, group_id, text):
        self.sent.append((bot_id, group_id, text))

    async def send_private_msg(self, *args, **kw):
        self.sent.append(args)


def _prepare(td, ws):
    module = _load_plugin_main()
    module.Plugin.inject_data_dir(str(td))
    module.Plugin.inject_ws_server(ws)
    module.Plugin.inject_admin_ids(["7777"])
    plugin = module.Plugin()
    plugin.plugin_config = {
        "manage_group": "100",
        "manage_users": [],
        "request": {"auto_agree_group": False, "auto_agree_friend": False},
    }
    return module, plugin


def _approve_event(reply_id: str):
    return SimpleNamespace(
        user_id=7777,
        message=[
            {"type": "reply", "data": {"id": reply_id}},
            {"type": "text", "data": {"text": "/同意"}},
        ],
    )


async def _invite(plugin, ws, group_id="1102231856", flag=GROUP_FLAG):
    raw = {
        "post_type": "request", "request_type": "group", "sub_type": "invite",
        "user_id": 3831097597, "group_id": int(group_id), "flag": flag,
        "comment": "拉群",
    }
    assert await plugin.on_request("bot_001", SimpleNamespace(), raw)
    text = [t for _, _, t in ws.sent if "【群邀请】" in t][-1]
    mid = "543195637"
    ws.cards[mid] = text
    return text, mid


async def test_card_keeps_server_flag_intact():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        _, plugin = _prepare(td, ws)
        text, _ = await _invite(plugin, ws)
        assert f"flag：{GROUP_FLAG}" in text, text


async def test_approve_returns_original_flag_to_protocol():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        _, plugin = _prepare(td, ws)
        _, mid = await _invite(plugin, ws)

        handled, reply = await plugin.on_message("bot_001", _approve_event(mid), {})

        assert handled and reply and reply.startswith("已同意群邀请"), reply
        params = [p for a, p in ws.calls
                  if a == "set_group_add_request"][-1]
        assert params == {"flag": GROUP_FLAG, "sub_type": "invite", "approve": True}, params


async def test_approve_failure_is_reported_not_claimed_success():
    with tempfile.TemporaryDirectory() as td:
        ws = _WS()
        ws.fail_set_group = {"status": "failed", "retcode": 100,
                             "wording": "invalid group request flag"}
        _, plugin = _prepare(td, ws)
        _, mid = await _invite(plugin, ws)

        _, reply = await plugin.on_message("bot_001", _approve_event(mid), {})

        assert reply and reply.startswith("❌ 审批失败"), reply
        assert "invalid group request flag" in reply, reply
        assert "已同意" not in reply, reply
