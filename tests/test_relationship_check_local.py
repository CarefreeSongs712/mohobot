"""relationship 插件抽查消息(本地 data/history)测试:

抽查(/抽查)改为读取本地归档 data/history/{bot_id}/ 后:
1. 群抽查: 按群号读本地 jsonl, 转发最近 count 条(节点署名取群名片)
2. @用户抽查: 读 private 私聊归档, 转发到当前会话(群或私聊)
3. count 超过归档条数 → 全量转发
4. 群号用序号指定(1 = 本地有记录的群的排序首项)
5. 目标无本地归档 → 提示错误(不静默)
6. 不依赖 get_group_msg_history/get_friend_msg_history API
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# 目录插件: relationship_core 需 plugins/relationship 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "relationship"))

from relationship_core.forward import ForwardTool

BOT = "bot_001"
GROUP = 398870315
USER = 1234567


def _event(user_id, text, mid, card="", nickname="昵称"):
    """构造一条与 data/history 归档一致的原始消息事件(JSONL 一行)。"""
    return {
        "time": 1786185000 + mid,
        "self_id": 2192362623,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": mid,
        "group_id": GROUP,
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": user_id, "nickname": nickname, "card": card},
    }


def _write_history(data_dir, lines, *, private_user=None):
    """写归档文件 group/{GROUP}.jsonl 或 private/{user}.jsonl, 返回路径。"""
    if private_user is not None:
        p = Path(data_dir) / "history" / BOT / "private" / f"{private_user}.jsonl"
    else:
        p = Path(data_dir) / "history" / BOT / "group" / f"{GROUP}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in lines) + "\n",
        encoding="utf-8",
    )
    return p


class FakeWS:
    """mock ws_server: 记录 send_to_bot 调用, 不真正联网。"""

    def __init__(self):
        self.calls = []  # (action, params)

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        self.calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": None}


async def _check(*, at_ids=(), target_arg="", count=2, data_dir, ws=None,
                 reply_group=999001, reply_user=100):
    """直连 ForwardTool.check_messages 抽查并返回使用的转发 API 调用参数。"""
    ws = ws or FakeWS()
    await ForwardTool.check_messages(
        ws_server=ws, bot_id=BOT,
        at_ids=list(at_ids), target_arg=target_arg, count=count,
        reply_group_id=reply_group, reply_user_id=reply_user,
        data_dir=data_dir, batch_size=0,
    )
    return ws.calls


async def test_group_check_reads_local_tail_and_uses_card():
    """群抽查: 读本地归档最近 count 条 + 节点名取群名片(card)。"""
    lines = [
        _event(11, f"消息{i}", i, card="") for i in range(1, 6)
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, lines)
        calls = await _check(target_arg=str(GROUP), count=2, data_dir=tmp)

    actions = [a for a, _ in calls]
    assert actions == ["send_group_forward_msg"], f"应只调转发 API, 实际 {actions}"
    params = calls[0][1]
    assert params["group_id"] == 999001
    nodes = params["messages"]
    assert len(nodes) == 2, "count=2 只转发最近 2 条"
    # 尾部两条: 消息4、消息5
    assert nodes[-1]["data"]["content"] == [
        {"type": "text", "data": {"text": "消息5"}}
    ]
    assert nodes[-1]["data"]["uin"] == 11


async def test_no_history_api_used():
    """抽查全程不调用 get_group_msg_history / get_friend_msg_history。"""
    lines = [_event(11, "消息1", 1)]
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, lines)
        ws = FakeWS()
        await _check(target_arg=str(GROUP), count=2, data_dir=tmp, ws=ws)
        actions = [a for a, _ in ws.calls]
        assert "get_group_msg_history" not in actions
        assert "get_friend_msg_history" not in actions


async def test_check_at_user_reads_private_archive():
    """@用户 → 读 private/{user}.jsonl 归档(转发到当前群)。"""
    lines = [
        _event(22, f"私聊{i}", i) for i in range(1, 4)
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, lines, private_user=USER)
        calls = await _check(at_ids=[str(USER)], count=5, data_dir=tmp)

    actions = [a for a, _ in calls]
    assert actions == ["send_group_forward_msg"], f"实际 {actions}"
    nodes = calls[0][1]["messages"]
    assert len(nodes) == 3, "归档只有 3 条, count=5 应全量转发"
    # 内容来自私聊归档
    assert nodes[0]["data"]["content"] == [
        {"type": "text", "data": {"text": "私聊1"}}
    ]


async def test_forward_to_private_when_reply_is_private():
    """在私聊中抽查群记录 → 转发 API 目标是 send_private_forward_msg。"""
    lines = [_event(11, "消息1", 1)]
    with tempfile.TemporaryDirectory() as tmp:
        _write_history(tmp, lines)
        calls = await _check(target_arg=str(GROUP), count=1, data_dir=tmp,
                             reply_group=0, reply_user=100)

    actions = [a for a, _ in calls]
    assert actions == ["send_private_forward_msg"], f"实际 {actions}"
    params = calls[0][1]
    assert params["user_id"] == 100


async def test_check_by_local_index():
    """序号抽查: 1 = 本地有记录的群排序首项。"""
    lines = [_event(11, "消息1", 1)]
    with tempfile.TemporaryDirectory() as tmp:
        # 建两个有归档的群, 抽查序号 2 命中排序第二个
        p1 = Path(tmp) / "history" / BOT / "group" / f"{GROUP}.jsonl"
        p2 = Path(tmp) / "history" / BOT / "group" / "999888.jsonl"
        p1.parent.mkdir(parents=True, exist_ok=True)
        p2.parent.mkdir(parents=True, exist_ok=True)
        p1.write_text(json.dumps(_event(11, "a", 1)) + "\n", encoding="utf-8")
        p2.write_text(json.dumps(_event(33, "b", 1)) + "\n", encoding="utf-8")

        calls = await _check(target_arg="2", count=1, data_dir=tmp)
        params = calls[0][1]
        nodes = params["messages"]
        assert nodes[0]["data"]["content"] == [
            {"type": "text", "data": {"text": "b"}}
        ], "序号 2 应命中 999888 群"


async def test_check_missing_archive_raises():
    """目标无本地归档 → 明确报错(不静默不查 API)。"""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            await _check(target_arg="777777", count=2, data_dir=tmp)
        except RuntimeError as e:
            assert "本地没有" in str(e) or "没有可抽查" in str(e)
        else:
            raise AssertionError("无归档时应抛 RuntimeError")


async def test_check_empty_history_no_candidates():
    """完全没有本地记录 → 提示无候选(原: 随机群失败报错)。"""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            await _check(count=2, data_dir=tmp)
        except RuntimeError as e:
            assert "本地没有可抽查" in str(e)
        else:
            raise AssertionError("无本地记录时应抛 RuntimeError")
