"""WSServer bot 发言归档测试 — message_sent 进 history / base64 净化 / 失败不归档 / 本地 id 兜底。

运行: python tests/_run_all.py (从仓库根)
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeSock:
    """替代真实 WebSocket, 记录发往 OneBot 客户端的 payload。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


async def _make_ws(tmp, echo_timeout: float = 0.2):
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.models.config import BotConfig
    from mohobot.ws_server import WSServer

    bm = BotManager(data_dir=str(tmp))
    sock = _FakeSock()
    inst = BotInstance("bot_001", sock,
                       BotConfig(bot_id="bot_001", qq=111, nickname="天依"))
    bm._bots["bot_001"] = inst
    ws = WSServer(bot_manager=bm, data_dir=str(tmp))
    ws._ARCHIVE_ECHO_TIMEOUT = echo_timeout  # 缩短超时, 测试里模拟"echo 丢失"
    return ws, bm, inst


async def _next_payload(inst, after: int, tries: int = 80) -> dict:
    """等出站 worker 送达第 after+1 条 payload(只认新增, 避免拿到 stale)。"""
    for _ in range(tries):
        if len(inst.ws.sent) > after:
            return json.loads(inst.ws.sent[-1])
        await asyncio.sleep(0.02)
    raise AssertionError("payload 未送达")


def _history_lines(tmp: Path, chat_type: str, chat_id: str) -> list[dict]:
    owner = "_merged" if chat_type == "group" else "bot_001"
    path = tmp / "history" / owner / chat_type / f"{chat_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


async def _deliver(bm, inst, after: int, message_id, retcode: int = 0) -> None:
    """后台任务: 等新 payload 送达后, 按 echo 回一个成功/失败响应。"""
    payload = await _next_payload(inst, after)
    await bm.handle_api_response("bot_001", {
        "status": "ok" if retcode == 0 else "failed",
        "retcode": retcode,
        "data": {"message_id": message_id} if message_id is not None else {},
        "echo": payload["echo"],
    })


async def test_archive_on_echo():
    """echo 返回 message_id → 归档一条 message_sent, 身份用该 id。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, inst = await _make_ws(tmp)
        try:
            task = asyncio.create_task(_deliver(bm, inst, 0, 555))
            await ws.send_group_msg("bot_001", 20002,
                                    [{"type": "text", "data": {"text": "大家好"}}])
            await task
            await asyncio.sleep(0.05)
            lines = _history_lines(tmp, "group", "20002")
            assert len(lines) == 1, lines
            ev = lines[0]
            assert ev["post_type"] == "message_sent" and ev["message_type"] == "group"
            assert ev["group_id"] == 20002 and ev["self_id"] == 111
            assert ev["message_id"] == "555"
            assert ev["sender"]["nickname"] == "天依"
            assert ev["message"][0]["data"]["text"] == "大家好"
            # BotInstance 的发送追踪(引用回复检测)同时生效
            assert inst.is_my_message("group", 20002, 555)
            print("[1] echo message_id 归档 OK")
        finally:
            await ws.stop()


async def test_send_to_bot_message_action():
    """send_to_bot 发送类 action 也归档, 且 wait_response 语义不变。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, inst = await _make_ws(tmp)
        try:
            task = asyncio.create_task(_deliver(bm, inst, 0, 777))
            resp = await ws.send_to_bot(
                "bot_001", "send_private_msg",
                {"user_id": 10001, "message": "私聊回复"},
                wait_response=True, timeout=3.0,
            )
            await task
            assert resp is not None and resp["data"]["message_id"] == 777
            await asyncio.sleep(0.05)
            lines = _history_lines(tmp, "private", "10001")
            assert len(lines) == 1 and lines[0]["message_id"] == "777"
            assert lines[0]["message"][0]["data"]["text"] == "私聊回复"
            print("[2] send_to_bot 发送类 action 归档 + 响应语义 OK")
        finally:
            await ws.stop()


async def test_sanitize_base64_and_forward():
    """图片/语音 base64 大字段替换为占位; 合并转发展平为署名文本。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, inst = await _make_ws(tmp)
        try:
            b64 = base64.b64encode(b"\x89PNGfakedata").decode()
            task = asyncio.create_task(_deliver(bm, inst, 0, 888))
            await ws.send_private_msg("bot_001", 10001,
                                      [{"type": "image", "data": {"file": f"base64://{b64}"}}])
            await task
            await asyncio.sleep(0.05)
            lines = _history_lines(tmp, "private", "10001")
            assert len(lines) == 1
            raw = json.dumps(lines[0], ensure_ascii=False)
            assert b64 not in raw, "base64 大字段不得写入 history"
            assert lines[0]["message"][0]["data"]["file"] == "base64://…"

            # 合并转发 → 展平为带署名文本(注意出站限速间隔, 等下一条 payload)
            task2 = asyncio.create_task(
                _deliver(bm, inst, len(inst.ws.sent), 889))
            await ws.send_group_forward_msg("bot_001", 20002, [
                {"type": "node", "data": {"user_id": "111", "nickname": "天依",
                                          "content": [{"type": "text", "data": {"text": "甲的回复"}}]}},
                {"type": "node", "data": {"user_id": "222", "nickname": "乙",
                                          "content": [{"type": "text", "data": {"text": "乙的回复"}}]}},
            ])
            await task2
            await asyncio.sleep(0.05)
            lines2 = _history_lines(tmp, "group", "20002")
            assert len(lines2) == 1 and lines2[0]["message_id"] == "889", lines2
            text = lines2[0]["message"][0]["data"]["text"]
            assert text.startswith("【合并转发】") and "天依: 甲的回复" in text
            assert "乙的回复" in text
            print("[3] base64 净化 + 合并转发展平 OK")
        finally:
            await ws.stop()


async def test_no_archive_on_failure():
    """客户端明确失败(retcode≠0) → 不归档。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, inst = await _make_ws(tmp)
        try:
            task = asyncio.create_task(_deliver(bm, inst, 0, None, retcode=1200))
            await ws.send_group_msg("bot_001", 20002, "不会发出去")
            await task
            await asyncio.sleep(0.05)
            assert _history_lines(tmp, "group", "20002") == []
            print("[4] 发送失败不归档 OK")
        finally:
            await ws.stop()


async def test_local_id_on_echo_lost():
    """echo 丢失(超时) → 用本地标记身份归档, bot 发言不丢。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, inst = await _make_ws(tmp, echo_timeout=0.15)
        try:
            await ws.send_group_msg("bot_001", 20002, "echo 丢失的回复")
            await asyncio.sleep(0.4)  # 等 0.15s 超时 + 归档任务完成
            lines = _history_lines(tmp, "group", "20002")
            assert len(lines) == 1
            assert lines[0]["message_id"].startswith("local:")
            assert lines[0]["message"][0]["data"]["text"] == "echo 丢失的回复"
            print("[5] echo 丢失 → 本地 id 兜底归档 OK")
        finally:
            await ws.stop()


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and asyncio.iscoroutinefunction(_fn):
            try:
                asyncio.run(_fn())
                print(f"PASS {_name}")
            except Exception as e:  # noqa: BLE001
                _failed += 1
                print(f"FAIL {_name}: {e}")
                import traceback
                traceback.print_exc()
    print("---")
    assert _failed == 0, f"{_failed} 个测试失败"
    print("ALL HISTORY ARCHIVE TESTS PASSED")
