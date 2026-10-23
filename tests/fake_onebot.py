"""假 OneBot v11 反向 WebSocket 客户端（供协议契约测试使用）。

真实 NapCat/LLOneBot 的一些行为在单元测试里很难复现：echo 乱序、echo 丢失、
响应缺字段、连接中途断开、同一群消息按账号分配不同 message_id 等。
本模块用真实 socket 连真实 WSServer，把这些边界做成可断言的行为。

设计约束：
- 只连 127.0.0.1 上的测试服务端，不访问外网；
- 读取循环把收到的 payload 放进队列，由测试显式选择"回/不回/乱序回"，
  不自动替测试做决定（自动回包会让"echo 丢失"这类用例失去意义）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets


class FakeOneBot:
    """最小 OneBot v11 反向 WS 客户端。"""

    def __init__(
        self,
        url: str,
        self_id: int | str,
        *,
        client_role: str = "Universal",
    ) -> None:
        self.url = url
        self.self_id = self_id
        self.client_role = client_role
        self.actions: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.raw_frames: list[str] = []
        self._ws = None
        self._reader: asyncio.Task | None = None

    async def connect(self) -> "FakeOneBot":
        self._ws = await websockets.connect(
            self.url,
            additional_headers={
                "X-Self-ID": str(self.self_id),
                "X-Client-Role": self.client_role,
            },
            open_timeout=5.0,
        )
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for frame in self._ws:
                self.raw_frames.append(frame)
                try:
                    payload = json.loads(frame)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    await self.actions.put(payload)
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            return

    async def send_event(self, event: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(event, ensure_ascii=False))

    async def send_raw(self, frame: str) -> None:
        assert self._ws is not None
        await self._ws.send(frame)

    async def next_action(self, timeout: float = 3.0) -> dict[str, Any]:
        """取服务端下发的下一个 action；超时则断言失败（而不是静默返回 None）。"""
        return await asyncio.wait_for(self.actions.get(), timeout=timeout)

    async def reply(
        self,
        action: dict[str, Any],
        *,
        status: str = "ok",
        retcode: int = 0,
        data: dict[str, Any] | None = None,
        echo: Any = ...,
        drop_echo: bool = False,
    ) -> None:
        """按收到的 action 回一个 OneBot 响应。

        echo 默认原样回带（正确行为）；drop_echo=True 模拟某些客户端在错误响应里
        省略 echo 的情况。
        """
        payload: dict[str, Any] = {
            "status": status,
            "retcode": retcode,
            "data": data or {},
        }
        if not drop_echo:
            payload["echo"] = action.get("echo") if echo is ... else echo
        assert self._ws is not None
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def abort(self) -> None:
        """模拟进程被杀：不发 close 帧直接断 TCP。"""
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        transport = getattr(self._ws, "transport", None)
        if transport is not None:
            transport.abort()
        else:  # 兜底
            await self.close()


def group_message(
    message_id: int | str,
    *,
    group_id: int = 20002,
    user_id: int = 10001,
    text: str = "你好",
    self_id: int = 111,
    time: int = 1700000000,
) -> dict[str, Any]:
    """构造一条群聊上报事件（可指定被哪个账号收到）。"""
    return {
        "time": time, "self_id": self_id, "post_type": "message",
        "message_type": "group", "sub_type": "normal",
        "message_id": message_id, "group_id": group_id, "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text, "font": 0,
        "sender": {"user_id": user_id, "nickname": "张三", "card": "张三"},
    }
