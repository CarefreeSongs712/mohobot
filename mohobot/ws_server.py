"""Reverse WebSocket server (Universal Client mode).

Listens for incoming OneBot v11 connections on a configurable port.
Each connecting OneBot instance sends X-Self-ID and X-Client-Role headers.
This server handles Universal clients — both event push and API calls
are multiplexed on the same connection per bot.
"""

from __future__ import annotations

import asyncio
import json
import time as time_module
from pathlib import Path
from typing import Any, Callable, Awaitable

import websockets
import websockets.asyncio.server
from loguru import logger

from mohobot.models.onebot import Event
from mohobot.bot_manager import BotManager
from mohobot.file_store import JSONLWriter
from mohobot.services.outbound import ChatAddress, OutboundScheduler, ReplySender
from mohobot.services.task_supervisor import TaskSupervisor, TaskSupervisorClosed
from mohobot.utils.cq_code import extract_plain_text

# Type alias for the event callback
EventCallback = Callable[[str, Event, dict[str, Any]], Awaitable[None]]

# 归档进 history 的消息发送类 action(其余 action 是查询/管理类, 不归档)
_MESSAGE_ACTIONS = {
    "send_group_msg": "group",
    "send_private_msg": "private",
    "send_group_forward_msg": "group",
}


class WSServer:
    """Reverse WebSocket server for OneBot v11 Universal Client connections."""

    def __init__(
        self,
        bot_manager: BotManager,
        host: str = "0.0.0.0",
        port: int = 8080,
        max_size: int = 10 * 1024 * 1024,
        task_supervisor: TaskSupervisor | None = None,
        outbound_scheduler: OutboundScheduler | None = None,
        reply_sender: ReplySender | None = None,
        outbound_interval: float = 0.5,
        outbound_maxsize: int = 100,
        outbound_enqueue_timeout: float = 2.0,
        data_dir: str = "./data",
    ):
        self._host = host
        self._port = port
        self._max_size = max_size
        self._bot_manager = bot_manager
        self._task_supervisor = task_supervisor
        self._data_dir = data_dir
        # bot 发言归档: bot 发出的消息以 message_sent 事件写入 history JSONL
        # (与收到的消息同一目录/文件), 供审核面板识别"bot 说过什么"。
        self._archive_writers: dict[str, JSONLWriter] = {}
        self._outbound_scheduler = outbound_scheduler or OutboundScheduler(
            send_interval_sec=outbound_interval,
            queue_maxsize=outbound_maxsize,
            enqueue_timeout_sec=outbound_enqueue_timeout,
        )
        self._reply_sender = reply_sender or ReplySender(self._outbound_scheduler)
        if self._reply_sender.scheduler is not self._outbound_scheduler:
            self._outbound_scheduler = self._reply_sender.scheduler
        self._on_event: EventCallback | None = None
        self._server: websockets.asyncio.server.Server | None = None
        self._heartbeat_interval: float = 30.0  # seconds
        self._nickname_cache: dict[str, tuple[str, float]] = {}  # get_nickname 缓存(key -> (名字, 过期时间))

    def set_event_callback(self, callback: EventCallback) -> None:
        """Set the callback invoked for every received event."""
        self._on_event = callback

    async def start(self) -> None:
        """Start the WebSocket server."""
        self._server = await websockets.asyncio.server.serve(
            self._handle_connection,
            self._host,
            self._port,
            max_size=self._max_size,
            ping_interval=self._heartbeat_interval,
            ping_timeout=10.0,
        )
        logger.info(f"WebSocket server listening on ws://{self._host}:{self._port}")

    async def stop(self) -> None:
        """Gracefully stop the WebSocket server and outbound workers."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("WebSocket server stopped")
        await self._outbound_scheduler.close()
        for writer in self._archive_writers.values():
            await writer.close()
        self._archive_writers.clear()

    async def close(self) -> None:
        """Alias for lifecycle integrations that expose close()."""
        await self.stop()

    async def _handle_connection(
        self, websocket: websockets.asyncio.server.ServerConnection
    ) -> None:
        """Handle an incoming WebSocket connection from a OneBot instance.

        X-Self-ID = QQ 号 → 按绑定关系注册为 bot 实例或未绑定连接。
        """
        # Extract headers
        headers = dict(websocket.request.headers)
        qq = headers.get("x-self-id", headers.get("X-Self-ID", ""))
        client_role = headers.get(
            "x-client-role", headers.get("X-Client-Role", "Universal")
        )

        logger.info(
            f"New connection: qq={qq}, role={client_role}, "
            f"remote={websocket.remote_address}"
        )

        # Register bot (按 QQ 查找绑定; 未绑定则接受但不处理)
        instance = self._bot_manager.register(qq, websocket)

        try:
            async for raw_message in websocket:
                try:
                    data = json.loads(raw_message)
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON from {instance.bot_id or f'QQ{instance.qq}'}: {e}")
                    continue

                await self._dispatch(instance, data)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"{instance.bot_id or f'QQ{instance.qq}'} disconnected: {e.code} {e.reason}")
        except Exception as e:
            logger.error(f"Connection error for {instance.bot_id or f'QQ{instance.qq}'}: {e}")
        finally:
            # 传实例: 若期间同 QQ 已建立新连接,不要误删新实例
            self._bot_manager.unregister(instance)

    async def _dispatch(self, instance, data: dict[str, Any]) -> None:
        """Dispatch an incoming message — event or API response.

        事件处理用 create_task 异步派发: 一条消息处理慢(如 API 超时)不会
        阻塞同一连接上后续消息/API 响应(否则一个指令卡住全 bot 都卡)。
        """
        # 未绑定连接: 接受但不处理任何消息
        if not instance.bound:
            logger.debug(f"Ignoring message from unbound connection QQ {instance.qq}: {data.get('post_type') or data.get('action')}")
            return

        bot_id = instance.bot_id

        # API response: has 'status' field (echo may be absent if the request
        # didn't carry one, e.g. some clients omit echo on error responses).
        if "status" in data:
            await self._bot_manager.handle_api_response(bot_id, data)
            return

        # It's an event (has 'post_type') — 异步派发, 不阻塞连接
        if "post_type" in data:
            event = Event.from_dict(data)
            if self._on_event:
                coro = self._on_event(bot_id, event, data)
                try:
                    if self._task_supervisor is not None:
                        self._task_supervisor.create_task(
                            coro, name=f"event:{bot_id}", owner="events"
                        )
                    else:
                        asyncio.create_task(coro, name=f"event:{bot_id}")
                except TaskSupervisorClosed:
                    logger.debug("Ignoring event after task supervisor shutdown")
            return

        # Unknown message type (诊断: 客户端回的非标准消息)
        preview = str(data)[:300]
        logger.warning(f"Unknown message from bot {bot_id}: {preview}")

    async def send_to_bot(
        self,
        bot_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        wait_response: bool = False,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Send an API call to a specific bot.

        If wait_response=True, waits for the OneBot client's response
        (via echo) and returns it; otherwise returns None.
        """
        params = params or {}

        # 消息发送类 action 统一走 _send_tracked(带 bot 发言归档), 保留响应语义
        if action in _MESSAGE_ACTIONS:
            chat_type = _MESSAGE_ACTIONS[action]
            chat_id = params.get("group_id") if chat_type == "group" else params.get("user_id")
            if chat_id is None:
                logger.warning(f"send_to_bot({action}) 缺少目标 id, bot {bot_id}")
                return None
            return await self._send_tracked(
                bot_id, action, params, chat_type, chat_id,
                wait_response=wait_response, timeout=timeout,
            )

        instance = self._bot_manager.get(bot_id)
        if not instance:
            logger.warning(f"Cannot send to bot {bot_id}: not connected")
            return None

        payload = {"action": action, "params": params or {}}

        if not wait_response:
            await self._reply_sender.call(
                bot_id, lambda: instance.send(payload), label=action
            )
            return None

        # Generate unique echo and register the future before queueing the send.
        import uuid
        echo = f"api_{uuid.uuid4().hex}"
        payload["echo"] = echo
        future = self._bot_manager.create_response_future(bot_id, echo)
        try:
            await self._reply_sender.call(
                bot_id, lambda: instance.send(payload), label=action
            )
        except Exception:
            self._bot_manager.remove_response_future(bot_id, echo)
            raise
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"API response timeout for {action} (bot {bot_id})")
            return None
        finally:
            self._bot_manager.remove_response_future(bot_id, echo)

    async def _send_tracked(
        self, bot_id: str, action: str, params: dict[str, Any],
        chat_type: str, chat_id: int | str,
        wait_response: bool = False, timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Send a message with echo tracking + bot speech archival.

        - echo 注册后, OneBot 客户端响应会:
            1) 记录 message_id 到 BotInstance(引用回复检测);
            2) resolve 归档 future → 后台把这条 bot 发言以 message_sent
               事件写入 history JSONL(审核数据源)。
        - wait_response=False(流式分段路径): 不等待, 保持发送速度;
        - wait_response=True: 等待并返回响应 dict(查询语义不变)。
        """
        import uuid
        instance = self._bot_manager.get(bot_id)
        if not instance:
            logger.warning(f"Cannot send to bot {bot_id}: not connected")
            return None
        echo = f"send:{chat_type}:{chat_id}:{uuid.uuid4().hex}"
        self._bot_manager._pending_sent[echo] = (bot_id, chat_type, str(chat_id))
        future = self._bot_manager.create_response_future(bot_id, echo)
        payload = {"action": action, "params": params, "echo": echo}
        try:
            await self._reply_sender.send(
                ChatAddress(bot_id, chat_type, chat_id),
                lambda: instance.send(payload),
                label=action,
            )
        except Exception:
            # Send failed — don't leave the tracked entry dangling
            self._bot_manager.drop_pending_sent(echo)
            self._bot_manager.remove_response_future(bot_id, echo)
            raise
        self._spawn_archive_task(self._archive_sent_message(
            bot_id, chat_type, chat_id, action, params, future, echo,
        ))
        if not wait_response:
            return None
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"API response timeout for {action} (bot {bot_id})")
            return None
        finally:
            self._bot_manager.remove_response_future(bot_id, echo)

    def _spawn_archive_task(self, coro) -> None:
        """派发归档后台任务(supervisor 优先, 关闭中回退裸 task)。"""
        if self._task_supervisor is not None:
            try:
                self._task_supervisor.create_task(
                    coro, name="archive-send", owner="archive",
                )
                return
            except TaskSupervisorClosed:
                pass
        asyncio.create_task(coro)

    # 归档等待 echo 响应的超时(秒); 超时用本地标记 id 兜底
    _ARCHIVE_ECHO_TIMEOUT = 8.0

    async def _archive_sent_message(
        self, bot_id: str, chat_type: str, chat_id: int | str,
        action: str, params: dict[str, Any],
        future: asyncio.Future, echo: str,
    ) -> None:
        """把一条 bot 发言以 message_sent 事件写入 history JSONL。

        - message_id 优先取 echo 响应返回值(稳定身份, 审核结论不失联);
          超时/未返回时用本地标记兜底(仍唯一, 但不会被"引用 bot 消息"命中)。
        - 客户端明确返回失败(retcode 非成功)说明消息未发出, 不归档。
        """
        try:
            resp = await asyncio.wait_for(
                future, timeout=self._ARCHIVE_ECHO_TIMEOUT,
            )
        except asyncio.TimeoutError:
            resp = None
        except Exception:
            return  # 发送链路失败(连接断开等), 不归档
        finally:
            self._bot_manager.remove_response_future(bot_id, echo)
            self._bot_manager.drop_pending_sent(echo)

        if resp is not None:
            status = str(resp.get("status", "") or "").lower()
            try:
                retcode = int(resp.get("retcode", 0) or 0)
            except (TypeError, ValueError):
                retcode = 0
            if retcode != 0 or status not in ("", "ok", "async"):
                return  # 客户端明确失败, 消息未发出

        data = (resp or {}).get("data") or {}
        mid = data.get("message_id")
        if mid is not None and str(mid).strip():
            message_id = str(mid)
        else:
            import uuid
            message_id = f"local:{uuid.uuid4().hex}"
        content = self._archive_content(action, params)
        if content is None:
            return
        await self._write_archive_line(bot_id, chat_type, chat_id, message_id, content)

    @staticmethod
    def _archive_content(action: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        """归档用的消息内容(统一成消息段列表); 无内容返回 None。"""
        if action == "send_group_forward_msg":
            # 合并转发: 展平为一条带署名的文本(bot 发言按节点内容归档)
            lines = []
            for node in (params.get("messages") or []):
                nd = (node or {}).get("data") or {}
                nick = str(nd.get("nickname") or nd.get("user_id") or "").strip()
                body = extract_plain_text(nd.get("content") or "").strip()
                if body:
                    lines.append(f"{nick}: {body}" if nick else body)
            text = "【合并转发】"
            if lines:
                text += "\n" + "\n".join(lines)
            return [{"type": "text", "data": {"text": text}}]
        return WSServer._sanitize_archive_segments(params.get("message"))

    @staticmethod
    def _sanitize_archive_segments(message) -> list[dict[str, Any]] | None:
        """归档前净化消息段: base64:// 大字段(图片/语音)替换为占位, 防 history 膨胀。"""
        if isinstance(message, str):
            text = message.strip()
            return [{"type": "text", "data": {"text": text}}] if text else None
        if not isinstance(message, list):
            return None
        out: list[dict[str, Any]] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            data = dict(seg.get("data") or {})
            if str(data.get("file") or "").startswith("base64://"):
                data["file"] = "base64://…"
                data.pop("url", None)
            out.append({"type": seg.get("type"), "data": data})
        return out or None

    async def _write_archive_line(
        self, bot_id: str, chat_type: str, chat_id: int | str,
        message_id: str, content: list[dict[str, Any]],
    ) -> None:
        """写一条 message_sent 事件进 history JSONL(失败只记日志)。"""
        instance = self._bot_manager.get(bot_id)
        bot_qq = instance.qq if instance else 0
        bot_nick = instance.nickname if instance else bot_id
        event: dict[str, Any] = {
            "post_type": "message_sent",
            "message_type": chat_type,
            "time": int(time_module.time()),
            "self_id": bot_qq,
            "user_id": bot_qq,
            "message_id": message_id,
            "message": content,
            "sender": {"user_id": bot_qq, "nickname": bot_nick, "card": ""},
        }
        if chat_type == "group":
            try:
                event["group_id"] = int(chat_id)
            except (TypeError, ValueError):
                event["group_id"] = chat_id
        try:
            path = self._archive_path(bot_id, chat_type, chat_id)
            await self._get_archive_writer(path).append(event)
        except Exception as e:
            logger.debug(f"bot 发言归档失败(bot={bot_id}, {chat_type}:{chat_id}): {e}")

    def _archive_path(self, bot_id: str, chat_type: str, chat_id: int | str) -> str:
        """bot 发言的 history 归档路径(与收到的消息同文件)。"""
        return f"{self._data_dir}/history/{bot_id}/{chat_type}/{chat_id}.jsonl"

    def _get_archive_writer(self, path: str) -> JSONLWriter:
        writer = self._archive_writers.get(path)
        if writer is None:
            writer = JSONLWriter(path)
            self._archive_writers[path] = writer
        return writer

    async def send_group_msg(
        self, bot_id: str, group_id: int | str, message: str | list[dict[str, Any]]
    ) -> None:
        """Send a group message via a specific bot (records message_id for reply detection)."""
        await self._send_tracked(
            bot_id, "send_group_msg",
            {"group_id": int(group_id), "message": message},
            "group", group_id,
        )

    async def send_private_msg(
        self, bot_id: str, user_id: int | str, message: str | list[dict[str, Any]]
    ) -> None:
        """Send a private message via a specific bot (records message_id)."""
        await self._send_tracked(
            bot_id, "send_private_msg",
            {"user_id": int(user_id), "message": message},
            "private", user_id,
        )

    async def send_image(
        self, bot_id: str, chat_type: str, chat_id: int | str, image_path: str
    ) -> None:
        """发送本地图片文件(base64 内嵌, 不依赖 NapCat 访问本地路径)。

        chat_type: "private" | "group"
        """
        import base64

        ext = Path(image_path).suffix.lstrip(".").lower() or "png"
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        message: list[dict[str, Any]] = [
            {"type": "image", "data": {"file": f"base64://{b64}"}},
        ]
        logger.debug(f"send_image to {chat_type}:{chat_id} via bot {bot_id} ({ext}, {len(b64) // 1024}KB)")
        if chat_type == "private":
            await self.send_private_msg(bot_id, chat_id, message)
        else:
            await self.send_group_msg(bot_id, chat_id, message)

    async def send_group_forward_msg(
        self, bot_id: str, group_id: int | str, nodes: list[dict[str, Any]]
    ) -> None:
        """发送合并转发消息(OneBot 扩展 API, NapCat 等客户端支持)。

        nodes: 自定义节点数组, 每节点:
          {"type": "node", "data": {"user_id": "QQ", "nickname": "名字",
                                    "content": [{"type": "text", "data": {"text": "..."}}]}}
        """
        await self._send_tracked(
            bot_id, "send_group_forward_msg",
            {"group_id": int(group_id), "messages": nodes},
            "group", group_id,
        )

    # ── 用户昵称查询(供插件使用) ─────────────────────────────

    async def get_nickname(
        self,
        bot_id: str,
        user_id: int | str,
        group_id: int | str | None = None,
    ) -> str:
        """获取用户昵称: 群名片 → QQ 昵称 → 数字兜底。

        缓存带 TTL(成功 10 分钟, 失败 30 秒重试), 避免首次获取失败时
        永久缓存数字导致昵称永远显示为 QQ 号。
        """
        now = time_module.time()
        cache_key = f"{bot_id}:{group_id or 'p'}:{user_id}"
        cached = self._nickname_cache.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

        nickname = str(user_id)
        # 群聊: 先取群成员资料(群名片优先; 空/空白名片回退昵称)
        if group_id is not None and str(group_id).isdigit():
            resp = await self.send_to_bot(
                bot_id, "get_group_member_info",
                {"group_id": int(group_id), "user_id": int(user_id)},
                wait_response=True, timeout=5.0,
            )
            if resp and resp.get("status") == "ok":
                data = resp.get("data") or {}
                card = str(data.get("card") or "").strip()
                member_nick = str(data.get("nickname") or "").strip()
                nickname = card or member_nick or nickname

        if nickname == str(user_id):
            # 群资料没拿到/私聊: 陌生人资料
            resp = await self.send_to_bot(
                bot_id, "get_stranger_info",
                {"user_id": int(user_id)},
                wait_response=True, timeout=5.0,
            )
            if resp and resp.get("status") == "ok":
                data = resp.get("data") or {}
                nickname = str(data.get("nickname") or "").strip() or nickname

        # 失败(仍是数字)只缓存 30 秒, 允许下次重试; 成功缓存 10 分钟
        ttl = 30 if nickname == str(user_id) else 600
        self._nickname_cache[cache_key] = (nickname, now + ttl)
        self._prune_nickname_cache(now)
        return nickname

    def _prune_nickname_cache(self, now: float | None = None) -> None:
        """惰性清理过期昵称缓存, 防止无限增长。"""
        now = time_module.time() if now is None else now
        stale = [k for k, (_, exp) in self._nickname_cache.items() if exp <= now]
        for k in stale:
            del self._nickname_cache[k]