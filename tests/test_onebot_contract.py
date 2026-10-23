"""OneBot v11 协议契约测试 — 用真实 socket 连真实 WSServer。

覆盖真实 NapCat/LLOneBot 常见而单元测试难以复现的行为：
多账号连接、未绑定 QQ、非法 JSON、echo 乱序、echo 丢失、响应缺 echo、
连接中途被强杀。全部只连 127.0.0.1，不访问外网。

运行: python tests/_run_all.py / python -m pytest -o asyncio_mode=auto tests/test_onebot_contract.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_onebot import FakeOneBot, group_message  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start_server(tmp: Path, bots: dict[str, int]):
    """在 127.0.0.1 上起真实 WSServer，按 {bot_id: qq} 写绑定配置。"""
    from mohobot.bot_manager import BotManager
    from mohobot.models.config import BotConfig
    from mohobot.ws_server import WSServer

    for bot_id, qq in bots.items():
        d = tmp / "bots" / bot_id
        d.mkdir(parents=True, exist_ok=True)
        BotConfig(bot_id=bot_id, qq=qq, nickname=bot_id).save(d / "config.json")

    bm = BotManager(data_dir=str(tmp))
    port = _free_port()
    ws = WSServer(bot_manager=bm, host="127.0.0.1", port=port, data_dir=str(tmp))
    await ws.start()
    return ws, bm, f"ws://127.0.0.1:{port}"


async def test_bound_bot_receives_event_over_real_socket():
    """已绑定 QQ 连入后，上报事件被解析并交给回调（bot_id 正确）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, url = await _start_server(tmp, {"bot_001": 111})
        seen: list[tuple] = []
        ws.set_event_callback(lambda bot_id, event, raw: _record(seen, bot_id, event, raw))
        client = await FakeOneBot(url, 111).connect()
        try:
            await client.send_event(group_message(1001))
            for _ in range(100):
                if seen:
                    break
                await asyncio.sleep(0.02)
            assert seen, "已绑定 bot 的事件没有被回调"
            bot_id, event_type, raw = seen[0]
            assert bot_id == "bot_001", bot_id
            assert event_type == "group", event_type
            assert raw["message_id"] == 1001
        finally:
            await client.close()
            await ws.stop()


async def _record(sink: list, bot_id, event, raw) -> None:
    sink.append((bot_id, getattr(event, "message_type", "?"), raw))


async def test_unbound_qq_events_are_ignored():
    """未绑定 QQ 连入后，上报事件一律不处理（只接受连接）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, url = await _start_server(tmp, {"bot_001": 111})
        seen: list[tuple] = []
        ws.set_event_callback(lambda bot_id, event, raw: _record(seen, bot_id, event, raw))
        client = await FakeOneBot(url, 999).connect()
        try:
            await client.send_event(group_message(1002, self_id=999))
            await asyncio.sleep(0.3)
            assert seen == [], f"未绑定 QQ 的事件不应处理: {seen}"
            assert bm.get("bot_001") is None, "未绑定连接不得冒充已绑定 bot"
        finally:
            await client.close()
            await ws.stop()


async def test_send_group_msg_payload_contract_and_message_id_recorded():
    """出站 action 形状符合 OneBot v11；echo 回包后 message_id 被记录用于引用检测。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, url = await _start_server(tmp, {"bot_001": 111})
        client = await FakeOneBot(url, 111).connect()
        try:
            for _ in range(100):
                if bm.get("bot_001") is not None:
                    break
                await asyncio.sleep(0.02)
            assert bm.get("bot_001") is not None, "bot 未注册"

            await ws.send_group_msg("bot_001", 20002, "契约测试")
            action = await client.next_action()
            assert action["action"] == "send_group_msg", action
            assert action["params"]["group_id"] == 20002, action["params"]
            assert action["params"]["message"] == "契约测试", action["params"]
            assert str(action["echo"]).startswith("send:group:"), action["echo"]

            await client.reply(action, data={"message_id": 8888})
            inst = bm.get("bot_001")
            for _ in range(100):
                if inst.is_my_message("group", 20002, 8888):
                    break
                await asyncio.sleep(0.02)
            assert inst.is_my_message("group", 20002, 8888), "echo 回包后应记录 message_id"
        finally:
            await client.close()
            await ws.stop()


async def test_out_of_order_echo_resolves_correct_futures():
    """两请求并发、乱序回包 → 各自拿到自己的响应（不得串包）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, url = await _start_server(tmp, {"bot_001": 111})
        client = await FakeOneBot(url, 111).connect()
        try:
            for _ in range(100):
                if bm.get("bot_001") is not None:
                    break
                await asyncio.sleep(0.02)

            async def call(tag: str):
                return await ws.send_to_bot(
                    "bot_001", "get_group_info",
                    {"group_id": 20002, "tag": tag}, wait_response=True, timeout=3.0,
                )

            t1 = asyncio.create_task(call("A"))
            t2 = asyncio.create_task(call("B"))
            first = await client.next_action()
            second = await client.next_action()
            # 反序回包
            await client.reply(second, data={"for": second["params"]["tag"]})
            await client.reply(first, data={"for": first["params"]["tag"]})
            r1, r2 = await asyncio.gather(t1, t2)
            assert r1 is not None and r2 is not None
            assert r1["data"]["for"] == "A", r1
            assert r2["data"]["for"] == "B", r2
        finally:
            await client.close()
            await ws.stop()


async def test_echo_lost_times_out_without_hanging():
    """echo 丢失（客户端不回）→ 超时返回 None，且不残留 pending。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, url = await _start_server(tmp, {"bot_001": 111})
        client = await FakeOneBot(url, 111).connect()
        try:
            for _ in range(100):
                if bm.get("bot_001") is not None:
                    break
                await asyncio.sleep(0.02)
            resp = await ws.send_to_bot(
                "bot_001", "get_group_info", {"group_id": 20002},
                wait_response=True, timeout=0.25,
            )
            assert resp is None, resp
            pending = bm._pending_responses.get("bot_001") or {}
            assert not pending, f"超时后不应残留 pending: {pending}"
        finally:
            await client.close()
            await ws.stop()


async def test_response_without_echo_still_resolves_single_pending():
    """部分客户端错误响应省略 echo；单个 pending 时应被匹配（不得永久挂起）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, url = await _start_server(tmp, {"bot_001": 111})
        client = await FakeOneBot(url, 111).connect()
        try:
            for _ in range(100):
                if bm.get("bot_001") is not None:
                    break
                await asyncio.sleep(0.02)

            async def call():
                return await ws.send_to_bot(
                    "bot_001", "get_group_info", {"group_id": 20002},
                    wait_response=True, timeout=3.0,
                )

            task = asyncio.create_task(call())
            action = await client.next_action()
            await client.reply(action, status="failed", retcode=100, drop_echo=True)
            resp = await task
            assert resp is not None, "缺 echo 的单一 pending 应被匹配"
            assert resp["retcode"] == 100, resp
        finally:
            await client.close()
            await ws.stop()


async def test_invalid_json_does_not_kill_connection():
    """收到非法 JSON 后连接仍可用，后续合法事件照常处理。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, url = await _start_server(tmp, {"bot_001": 111})
        seen: list[tuple] = []
        ws.set_event_callback(lambda bot_id, event, raw: _record(seen, bot_id, event, raw))
        client = await FakeOneBot(url, 111).connect()
        try:
            await client.send_raw("这不是 JSON {{{")
            await asyncio.sleep(0.1)
            await client.send_event(group_message(2001))
            for _ in range(100):
                if seen:
                    break
                await asyncio.sleep(0.02)
            assert seen, "非法 JSON 之后连接应仍能处理合法事件"
        finally:
            await client.close()
            await ws.stop()


async def test_abrupt_disconnect_unregisters_bot():
    """连接被强制断开（不发 close 帧）→ bot 被注销，不残留实例。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, url = await _start_server(tmp, {"bot_001": 111})
        client = await FakeOneBot(url, 111).connect()
        try:
            for _ in range(100):
                if bm.get("bot_001") is not None:
                    break
                await asyncio.sleep(0.02)
            assert bm.get("bot_001") is not None
            await client.abort()
            for _ in range(150):
                if bm.get("bot_001") is None:
                    break
                await asyncio.sleep(0.02)
            assert bm.get("bot_001") is None, "断开后 bot 实例应被注销"
            # 断开后发送不应抛异常，只返回 None
            assert await ws.send_group_msg("bot_001", 20002, "x") is None
        finally:
            await ws.stop()


async def test_two_bots_same_event_different_message_ids():
    """两个账号各自上报"同一条"群消息（message_id 不同）→ 两个 bot 都收到事件。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws, bm, url = await _start_server(tmp, {"bot_001": 111, "bot_002": 222})
        seen: list[tuple] = []
        ws.set_event_callback(lambda bot_id, event, raw: _record(seen, bot_id, event, raw))
        c1 = await FakeOneBot(url, 111).connect()
        c2 = await FakeOneBot(url, 222).connect()
        try:
            for _ in range(150):
                if bm.get("bot_001") and bm.get("bot_002"):
                    break
                await asyncio.sleep(0.02)
            await c1.send_event(group_message(7001, self_id=111, time=1700000100))
            await c2.send_event(group_message(7002, self_id=222, time=1700000100))
            for _ in range(150):
                if len(seen) >= 2:
                    break
                await asyncio.sleep(0.02)
            bot_ids = sorted(s[0] for s in seen)
            assert bot_ids == ["bot_001", "bot_002"], f"两个账号都应收到事件: {seen}"
            mids = sorted(s[2]["message_id"] for s in seen)
            assert mids == [7001, 7002], mids
        finally:
            await c1.close()
            await c2.close()
            await ws.stop()


async def main() -> None:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in tests:
        await fn()
        print(f"PASS {fn.__name__}")
    print("\nALL ONEBOT CONTRACT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
