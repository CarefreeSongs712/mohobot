"""Reverse WebSocket server (Universal Client mode).

Listens for incoming OneBot v11 connections on a configurable port.
Each connecting OneBot instance sends X-Self-ID and X-Client-Role headers.
This server handles Universal clients — both event push and API calls
are multiplexed on the same connection per bot.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time as time_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import websockets
import websockets.asyncio.server
from loguru import logger

from mohobot.models.onebot import Event
from mohobot.models.config import is_external_service_group
from mohobot.bot_manager import BotInstance, BotManager
from mohobot.file_store import JSONLWriter
from mohobot.history_dedup import SentMessageClaims, signature_for_send
from mohobot.services.outbound import ChatAddress, DispatchAttempt, OutboundScheduler, ReplySender
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
_BLOCKED_GROUP_SEND = object()
_BLOCKED_DISPATCH = object()


@dataclass(frozen=True)
class DeliveryResult:
    """一次消息发送的结果(issue #128)。

    status:
      - "sent": OneBot 回执确认成功(confirmed=True);
      - "failed": 明确失败(未连接/旧连接/发送异常/回执 retcode 非 0);
      - "blocked": 被出站保护拦截(外部服务独占群/禁用 bot), 未发出;
      - "unknown": 已交给传输层但没有回执, 无法确认是否送达。

    调用方只能把 "sent" 与无法确认的 "unknown" 视作"用户可能看到了",
    "failed"/"blocked" 的内容不得写入对话记忆。
    """

    status: str = "unknown"
    confirmed: bool = False
    response: dict[str, Any] | None = None
    reason: str = ""


def _classify_response(response: dict[str, Any] | None) -> str:
    """OneBot 回执分类: "sent" / "failed" / "unknown"(issue #128 review)。

    旧实现把缺失 retcode 当作 0, 于是 status 缺省的空回执也被当成成功;
    这里要求显式 retcode 才能判定 sent —— 无法确认时必须返回 unknown,
    不能假装成功(归档/记忆写入策略据此区分)。
    """
    if not isinstance(response, dict):
        return "unknown"
    status_raw = response.get("status")
    status = str(status_raw).lower() if status_raw is not None else ""
    retcode_raw = response.get("retcode")
    if retcode_raw is None:
        return "failed" if status == "failed" else "unknown"
    try:
        retcode = int(retcode_raw)
    except (TypeError, ValueError):
        return "unknown"
    if status == "failed":
        return "failed"
    if status not in ("", "ok", "async"):
        return "failed" if retcode != 0 else "unknown"
    return "sent" if retcode == 0 else "failed"


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
        access_token: str = "",
        data_dir: str = "./data",
        global_config=None,
    ):
        self._host = host
        self._port = port
        self._max_size = max_size
        # 反向 WS 鉴权(OneBot v11): 空=不校验; 非空时握手后先验 token
        self._access_token = (access_token or "").strip()
        self._bot_manager = bot_manager
        self._task_supervisor = task_supervisor
        self._data_dir = data_dir
        # Shared live GlobalConfig: read group guards and archive settings per send.
        self._global_config = global_config
        # bot 发言归档: bot 发出的消息以 message_sent 事件写入 history JSONL
        # (群聊写合并文件 data/history/group/{群号}.jsonl, 私聊与收到的消息
        # 同一目录/文件), 供审核面板识别"bot 说过什么"。
        self._archive_writers: dict[str, JSONLWriter] = {}
        # 发送侧跨账号指纹登记: 供入站归档识别"这是我方发言的副本"
        self._sent_claims = SentMessageClaims()
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

    @staticmethod
    def _token_ok(configured: str, headers: dict[str, str], path: str) -> bool:
        """校验 OneBot v11 access_token: Bearer 头或 ?access_token= 查询参数。

        configured 为空 → 不校验(放行, 兼容旧部署); 常数时间比较防计时侧信道。
        """
        if not configured:
            return True
        supplied = ""
        for key in ("authorization", "Authorization"):
            auth = headers.get(key, "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
                break
        if not supplied and "?" in path:
            from urllib.parse import parse_qs, urlsplit

            query = urlsplit(path).query or path.split("?", 1)[1]
            supplied = (parse_qs(query).get("access_token") or [""])[0]
        if not supplied:
            return False
        return hmac.compare_digest(supplied, configured)

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

        # 鉴权(OneBot v11 access_token): 配置了 token 但客户端未提供/不匹配
        # → 握手后立即 1008 关闭, 不注册、不处理任何消息。
        if self._access_token and not self._token_ok(
            self._access_token,
            dict(websocket.request.headers),
            str(getattr(websocket.request, "path", "") or ""),
        ):
            logger.warning(
                f"WS 连接鉴权失败(token 不匹配), 拒绝: remote={websocket.remote_address}"
            )
            await websocket.close(code=1008, reason="invalid access token")
            return

        # Register bot (按 QQ 查找绑定; 未绑定则接受但不处理)
        if str(client_role).lower() in ("event", "api"):
            instance = self._bot_manager.register(qq, websocket, client_role=client_role)
        else:
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

        # issue #127: 同一 QQ 重连时新连接会替换旧实例; 旧连接的读循环可能
        # 尚未退出, 重叠窗口内不得再派发事件/响应(否则同一条消息被处理两次)。
        is_current = getattr(self._bot_manager, "is_current", None)
        if callable(is_current) and not is_current(instance):
            logger.debug(
                f"Ignoring message from stale connection (bot={instance.bot_id}, "
                f"generation={getattr(instance, 'generation', 0)})"
            )
            return

        bot_id = instance.bot_id

        # API response: has 'status' field (echo may be absent if the request
        # didn't carry one, e.g. some clients omit echo on error responses).
        if "status" in data:
            await self._bot_manager.handle_api_response(bot_id, data)
            return
        if getattr(instance, "client_role", "Universal") == "API":
            return

        # 群成员减少通知(被踢/主动退群): 立即从群在线集合剔除该 bot, 否则
        # 全局指令抽签仍可能选中它导致命令无响应(issue #9 第 4 点)。
        # 通知本身不依赖 enabled 开关, 先于开关判断处理。
        if data.get("post_type") == "notice" and data.get("notice_type") == "group_decrease":
            group_id = data.get("group_id")
            if group_id is not None:
                try:
                    self._bot_manager.note_group_left(bot_id, group_id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"note_group_left failed: {exc}")

        # Bot 禁用开关: Web 面板把 bot_config.enabled 置 false 时应停止一切响应
        # (issue #9 第 3 点)。连接保持, 只是不处理事件。
        bot_config = getattr(instance, "config", None)
        if bot_config is not None and not getattr(bot_config, "enabled", True):
            logger.debug(f"Ignoring event for disabled bot {bot_id}")
            return

        # It's an event (has 'post_type') — 异步派发, 不阻塞连接
        if "post_type" in data:
            event = Event.from_dict(data)
            if self._on_event:
                generation = getattr(instance, "generation", 0)

                async def dispatch_current():
                    # 排队期间可能发生同角色重连、解绑或禁用。已开始执行的
                    # callback 保持原有生命周期; 未开始的旧连接事件不产生副作用。
                    if (not instance.bound or instance.bot_id != bot_id
                            or getattr(instance, "generation", 0) != generation
                            or (callable(is_current) and not is_current(instance))
                            or not getattr(getattr(instance, "config", None), "enabled", True)):
                        return
                    await self._on_event(bot_id, event, data)

                coro = dispatch_current()
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

    @staticmethod
    def _group_send_target(action: str, params: dict[str, Any]):
        """Identify group message/forward sends without blocking queries/private APIs."""
        # OneBot async/rate-limited action suffixes must not bypass the guard.
        for suffix in ("_async", "_rate_limited"):
            if action.endswith(suffix):
                action = action[:-len(suffix)]
                break
        if action in ("send_group_msg", "send_group_forward_msg"):
            return params.get("group_id")
        if action in ("send_msg", "send_forward_msg"):
            message_type = params.get("message_type")
            if message_type == "group" or (
                not message_type and params.get("group_id") is not None
            ):
                return params.get("group_id")
        # Quick-operation replies contain the destination in the event context.
        if action == ".handle_quick_operation":
            context = params.get("context") or {}
            operation = params.get("operation") or {}
            if context.get("message_type") == "group" and operation.get("reply") is not None:
                return context.get("group_id")
        return None

    def _blocks_group_send(self, action: str, params: dict[str, Any]) -> bool:
        cfg = getattr(self, "_global_config", None)
        return is_external_service_group(
            self._group_send_target(action, params),
            getattr(cfg, "external_service_groups", ()),
        )

    async def _send_guarded(
        self, instance, payload: dict[str, Any], dispatch_guard: Callable[[], bool] | None = None,
        *, attempt: DispatchAttempt | None = None,
    ):
        """Recheck live guards at scheduler dispatch and after socket-lock waits."""
        blocked = None

        def allowed():
            nonlocal blocked
            if dispatch_guard is not None and not dispatch_guard():
                blocked = _BLOCKED_DISPATCH
            elif self._blocks_group_send(payload["action"], payload["params"]):
                blocked = _BLOCKED_GROUP_SEND
            return blocked is None

        if not allowed():
            return blocked
        guarded_send = getattr(instance, "send_guarded", None)
        if (
            isinstance(instance, BotInstance)
            and getattr(instance.send, "__func__", None) is not BotInstance.send
        ):
            # Existing adapters may override send() instead of owning a socket.
            guarded_send = None
        if callable(guarded_send):
            # Native transport can distinguish operation-start from socket-start.
            # Cancellation while merely waiting on its lock is still unsent.
            if attempt is not None:
                attempt.dispatched = False

            def started():
                if attempt is not None:
                    attempt.dispatched = True

            if not await guarded_send(payload, allowed, on_dispatch=started):
                return blocked
            return None
        # Lightweight embedded adapters retain their existing send(payload) API.
        return await instance.send(payload)

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
        if self._blocks_group_send(action, params):
            return None

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
                bot_id, lambda: self._send_guarded(instance, payload), label=action
            )
            return None

        # Generate unique echo and register the future before queueing the send.
        import uuid
        echo = f"api_{uuid.uuid4().hex}"
        payload["echo"] = echo
        future = self._bot_manager.create_response_future(bot_id, echo)
        # issue #128: 取消可能发生在 socket send 的 await 中(不只是等回执时),
        # 统一用 finally 清理 pending 路由并结清 Future, 避免取消后映射/协程泄漏。
        try:
            result = await self._reply_sender.call(
                bot_id, lambda: self._send_guarded(instance, payload), label=action
            )
            if result is _BLOCKED_GROUP_SEND:
                return None
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"API response timeout for {action} (bot {bot_id})")
            return None
        finally:
            self._bot_manager.remove_response_future(bot_id, echo)
            if not future.done():
                future.cancel()

    async def _send_tracked(
        self, bot_id: str, action: str, params: dict[str, Any],
        chat_type: str, chat_id: int | str,
        wait_response: bool = False, timeout: float = 10.0,
        source: str = "auto",
        outcome: dict[str, Any] | None = None,
        quick_ack_timeout: float = 0.0,
        dispatch_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any] | None:
        """Send a message with echo tracking + bot speech archival.

        - echo 注册后, OneBot 客户端响应会:
            1) 记录 message_id 到 BotInstance(引用回复检测);
            2) resolve 归档 future → 后台把这条 bot 发言以 message_sent
               事件写入 history JSONL(审核数据源)。
        - source 标注发言来源("llm"=LLM 生成, "auto"=命令/插件等机械内容),
          写入归档行供审核面板过滤。
        - wait_response=False(流式分段路径): 不等待, 保持发送速度;
        - wait_response=True: 等待并返回响应 dict(查询语义不变), 且先拿到
          真实回执再归档, 避免回执晚于 8s 归档阈值时 history 只剩 local: id。
        - outcome(可选 dict): 填充 status/reason, 供需要投递状态的调用方
          (send_message_with_status)使用; 返回值保持旧契约不变(#128)。
        """
        if self._blocks_group_send(action, params):
            if outcome is not None:
                outcome.update(status="blocked", reason="external_service_group")
            return None
        import uuid
        instance = self._bot_manager.get(bot_id)
        if not instance:
            logger.warning(f"Cannot send to bot {bot_id}: not connected")
            if outcome is not None:
                outcome.update(status="failed", reason="not_connected")
            return None
        # issue #131: 出站层统一 enabled 保护 —— 禁用但仍连接的 bot 不得再发消息
        # (入站事件已被 _dispatch 丢弃, 主动聊天/后台任务不能绕过)。
        if getattr(instance, "stale", False):
            logger.warning(f"Cannot send to bot {bot_id}: stale connection")
            if outcome is not None:
                outcome.update(status="failed", reason="stale_connection")
            return None
        config = getattr(instance, "config", None)
        if not getattr(config, "enabled", True):
            logger.info(f"Bot {bot_id} disabled, suppress outbound message ({action})")
            if outcome is not None:
                outcome.update(status="blocked", reason="bot_disabled")
            return None
        echo = f"send:{chat_type}:{chat_id}:{uuid.uuid4().hex}"
        self._bot_manager._pending_sent[echo] = (bot_id, chat_type, str(chat_id))
        future = self._bot_manager.create_response_future(bot_id, echo)
        payload = {"action": action, "params": params, "echo": echo}
        attempt = DispatchAttempt()
        # 发送前登记指纹: 其他 bot 会收到这条消息的入站副本(message_id 按
        # 账号分配, 无法靠 id 判重), 据此跳过, 避免同一句 bot 发言存两行。
        claimed = self._claim_sent_signature(instance, chat_type, chat_id, action, params)
        discarded = False

        def discard_unsent():
            nonlocal discarded
            if discarded:
                return
            discarded = True
            self._release_sent_signature(claimed)
            self._bot_manager.drop_pending_sent(echo)
            self._bot_manager.remove_response_future(bot_id, echo)
            future.cancel()

        async def operation():
            result = await self._send_guarded(
                instance, payload,
                lambda: (
                    not future.cancelled()
                    and getattr(getattr(instance, "config", None), "enabled", True)
                    and (dispatch_guard is None or dispatch_guard())
                ),
                attempt=attempt,
            )
            if result is _BLOCKED_GROUP_SEND or result is _BLOCKED_DISPATCH:
                # The caller may already have cancelled while this operation
                # waited for the socket lock. Release tracking here as well so
                # its archive handoff cannot later invent a local sent message.
                discard_unsent()
            return result
        try:
            result = await self._reply_sender.send(
                ChatAddress(bot_id, chat_type, chat_id),
                operation,
                label=action,
                attempt=attempt,
            )
        except asyncio.CancelledError:
            # 调用方被取消(超时/父任务取消)。未派发时排队项会被 worker 丢弃,
            # 这里清掉追踪即可; 已派发时消息可能已经发出, 归档所有权必须转交
            # 后台(等 echo 或 8s 兜底) —— 否则 history 与真实投递不一致,
            # pending/回执 future 也会永久残留。
            if outcome is not None:
                outcome.update(status="failed", reason="cancelled")
            if attempt.dispatched:
                self._spawn_archive_task(self._archive_sent_message(
                    bot_id, chat_type, chat_id, action, params, future, echo, source,
                    owns_response=True,
                ))
            else:
                discard_unsent()
            raise
        except Exception as e:
            # Send failed — don't leave the tracked entry dangling
            if outcome is not None:
                outcome.update(status="failed", reason=f"send_error: {e}")
            discard_unsent()
            raise
        if result is _BLOCKED_GROUP_SEND or result is _BLOCKED_DISPATCH:
            if outcome is not None:
                reason = (
                    "external_service_group" if result is _BLOCKED_GROUP_SEND else
                    "bot_disabled" if not getattr(getattr(instance, "config", None), "enabled", True) else
                    "dispatch_guard"
                )
                outcome.update(status="blocked", reason=reason)
            return None  # Not sent: no reply tracking, timeout, or speech archive.
        if not wait_response:
            # 不等待回执: 后台等 echo(或 8s 兜底)写归档, 默认无法确认送达。
            if outcome is not None:
                outcome.update(status="unknown", reason="not_waited")
            self._spawn_archive_task(self._archive_sent_message(
                bot_id, chat_type, chat_id, action, params, future, echo, source,
                owns_response=True,
            ))
            if quick_ack_timeout <= 0:
                return None
            # 流水线路径的"快速负 ack"窗口(review): ack_timeout=0 后协议端的
            # retcode!=0/status=failed 不再被 5s 等待捕获, 但主链必须知道该段
            # 其实没发出去。短暂等首条回执, 明确失败就反馈 failed; 明确成功
            # 也可反馈 sent; 窗口内无回执仍记 unknown, 由后台归档继续等。
            try:
                quick_response = await asyncio.wait_for(
                    asyncio.shield(future), timeout=quick_ack_timeout)
            except asyncio.TimeoutError:
                return None
            except asyncio.CancelledError:
                # 归档任务已 owns_response=True, 会结清 future 与 pending;
                # 这里只把状态记清楚再向上传播取消。
                if outcome is not None:
                    outcome.update(status="unknown", reason="cancelled")
                raise
            if outcome is not None and quick_response is not None:
                verdict = _classify_response(quick_response)
                if verdict == "sent":
                    outcome.update(status="sent", reason="")
                elif verdict == "failed":
                    outcome.update(status="failed", reason="negative_ack")
                else:
                    outcome.update(status="unknown", reason="unconfirmed_ack")
            return None
        # issue #128: 先拿到真实回执再归档, 避免回执晚于归档阈值时 history
        # 只剩 local: id。调用方等待窗口(默认 5s)短于归档兜底窗口(8s)时:
        # - 5~8s 的慢回执: 把 future 交给后台归档继续等满兜底窗口, 拿到真实
        #   message_id 再落行(review: 不能把慢回执当成超时写 local:);
        # - 超过兜底窗口仍无回执: 后台按 local: id 归档(与旧行为一致), 状态
        #   显式记 unknown, 不假装成功;
        # - 等待中被取消且消息已 dispatch: 同样先交后台归档再 re-raise,
        #   不能因为调用方取消就丢掉这次真实发送的 history 记录。
        response: dict[str, Any] | None = None
        try:
            response = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.CancelledError:
            if outcome is not None:
                outcome.update(status="unknown", reason="cancelled")
            if attempt.dispatched:
                self._spawn_archive_task(self._archive_sent_message(
                    bot_id, chat_type, chat_id, action, params, future, echo, source,
                    owns_response=True,
                ))
            else:
                self._release_sent_signature(claimed)
                self._bot_manager.drop_pending_sent(echo)
                self._bot_manager.remove_response_future(bot_id, echo)
                future.cancel()
            raise
        except asyncio.TimeoutError:
            logger.warning(
                f"API response timeout for {action} (bot {bot_id}); "
                "handing echo routing to archive task for late ack"
            )
            if outcome is not None:
                outcome.update(status="unknown", reason="ack_timeout")
            self._spawn_archive_task(self._archive_sent_message(
                bot_id, chat_type, chat_id, action, params, future, echo, source,
                owns_response=True,
            ))
            return None
        self._bot_manager.remove_response_future(bot_id, echo)
        self._bot_manager.drop_pending_sent(echo)
        if outcome is not None:
            verdict = _classify_response(response)
            if verdict == "sent":
                outcome.update(status="sent", reason="")
            elif verdict == "failed":
                outcome.update(status="failed", reason="negative_ack")
            else:
                outcome.update(status="unknown", reason="unconfirmed_ack")
        await self._archive_sent_message(
            bot_id, chat_type, chat_id, action, params, None, echo, source,
            owns_response=False, response=response,
        )
        return response

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

    # ── 发送侧指纹登记(入站归档去重) ─────────────────────────

    def _claim_sent_signature(
        self, instance, chat_type: str, chat_id: int | str,
        action: str, params: dict[str, Any],
    ) -> tuple[Any, ...] | None:
        """登记本次发送的跨账号指纹; 非群聊或内容为空时不登记。

        返回可用于 ``_release_sent_signature`` 的登记句柄(或 None)。
        """
        claims = getattr(self, "_sent_claims", None)
        if claims is None or chat_type != "group":
            return None
        signature = signature_for_send(action, params)
        if not signature[0] and not signature[1]:
            return None
        sender = str(getattr(instance, "qq", "") or "")
        claims.claim(chat_id, sender, signature)
        return (chat_id, sender, signature)

    def _release_sent_signature(self, claimed: tuple[Any, ...] | None) -> None:
        """发送未真正发生(取消/失败/被拦)时撤销登记, 保留真实入站记录。"""
        if claimed is None:
            return
        claims = getattr(self, "_sent_claims", None)
        if claims is not None:
            claims.release(*claimed)

    # 归档等待 echo 响应的超时(秒); 超时用本地标记 id 兜底
    # 流水线发送时等待"快速负 ack"的窗口(issue #128 review): 只捕获协议端
    # 立即返回的 retcode!=0/status=failed; 超过该窗口仍记 unknown 并交后台归档。
    _QUICK_ACK_WINDOW = 0.25
    _ARCHIVE_ECHO_TIMEOUT = 8.0

    async def _archive_sent_message(
        self, bot_id: str, chat_type: str, chat_id: int | str,
        action: str, params: dict[str, Any],
        future: asyncio.Future | None = None, echo: str = "", source: str = "auto",
        *, owns_response: bool = True, response: dict[str, Any] | None = None,
    ) -> None:
        """把一条 bot 发言以 message_sent 事件写入 history JSONL。

        - message_id 优先取 echo 响应返回值(稳定身份, 审核结论不失联);
          超时/未返回时用本地标记兜底(仍唯一, 但不会被"引用 bot 消息"命中)。
        - 客户端明确返回失败(retcode 非成功)说明消息未发出, 不归档。
        - 调用方已拿到回执(wait_response=True)时传 response, 不再等待 future,
          避免归档与调用方超时竞争导致 history 写入 local: id (#128)。
        """
        if response is None and future is not None:
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(future), timeout=self._ARCHIVE_ECHO_TIMEOUT,
                )
            except asyncio.TimeoutError:
                response = None
            except Exception:
                return  # 发送链路失败(连接断开等), 不归档
            finally:
                # A confirmed-delivery caller owns routing until its longer timeout.
                # The archive timeout must not steal a later (e.g. 9s) success echo.
                if owns_response:
                    self._bot_manager.remove_response_future(bot_id, echo)
                    self._bot_manager.drop_pending_sent(echo)

        if response is not None and _classify_response(response) == "failed":
            return  # 客户端明确失败, 消息未发出

        data = (response or {}).get("data") or {}
        mid = data.get("message_id")
        if mid is not None and str(mid).strip():
            message_id = str(mid)
        else:
            import uuid
            message_id = f"local:{uuid.uuid4().hex}"
        content = self._archive_content(action, params)
        if content is None:
            return
        await self._write_archive_line(
            bot_id, chat_type, chat_id, message_id, content, source,
        )

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
        message_id: str, content: list[dict[str, Any]], source: str = "auto",
    ) -> None:
        """写一条 message_sent 事件进 history JSONL(失败只记日志)。

        群聊写合并文件 data/history/group/{群号}.jsonl(与收到的群消息同文件,
        行内 bot_id 标注发言 bot); 旧 per-bot 布局已停止写入。source 标注来源:
        "llm"=LLM 生成回复, "auto"=命令/插件等机械内容(审核面板据此过滤)。
        """
        # 维护窗口内跳过归档(不写旧 inode), 见 issue #8
        barrier = getattr(self, "_history_barrier", None)
        if barrier is not None and barrier.frozen:
            logger.debug("history 维护中, 跳过 bot 发言归档")
            return
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
            "bot_id": bot_id,
            "source": source,
            "message": content,
            "sender": {"user_id": bot_qq, "nickname": bot_nick, "card": ""},
        }
        if chat_type == "group":
            try:
                event["group_id"] = int(chat_id)
            except (TypeError, ValueError):
                event["group_id"] = chat_id
        try:
            for path in self._archive_paths(bot_id, chat_type, chat_id):
                await self._get_archive_writer(path).append(event)
        except Exception as e:
            logger.debug(f"bot 发言归档失败(bot={bot_id}, {chat_type}:{chat_id}): {e}")

    def _archive_paths(self, bot_id: str, chat_type: str, chat_id: int | str) -> list[str]:
        """bot 发言的 history 归档路径(群聊只有合并文件, 私聊按 bot 分目录)。"""
        if chat_type == "group":
            # 只写合并布局; 旧 per-bot 布局已停止写入(仅 review 读侧兼容存量)
            return [f"{self._data_dir}/history/group/{chat_id}.jsonl"]
        return [f"{self._data_dir}/history/{bot_id}/private/{chat_id}.jsonl"]

    # ── 历史维护屏障 (issue #8) ──────────────────────────────

    def set_history_barrier(self, barrier) -> None:
        """注入历史维护屏障: 冻结期间跳过 message_sent 归档并注册关闭回调。"""
        self._history_barrier = barrier
        if barrier is not None:
            barrier.register_closer(self.close_archive_writers)

    async def close_archive_writers(self) -> None:
        """退休并排空 bot 发言归档 writer, 不让旧追加跨越维护窗口。"""
        writers = list(self._archive_writers.items())
        for _path, writer in writers:
            writer.retire()
        self._archive_writers.clear()
        cancelled = None
        for path, writer in writers:
            try:
                await writer.close()
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"关闭 archive writer 失败({path}): {exc}")
        logger.info(f"[History] 已关闭 {len(writers)} 个 bot 发言归档 writer")
        if cancelled is not None:
            raise cancelled

    def _get_archive_writer(self, path: str) -> JSONLWriter:
        writer = self._archive_writers.get(path)
        if writer is None:
            writer = JSONLWriter(path)
            self._archive_writers[path] = writer
        return writer

    async def send_group_msg(
        self, bot_id: str, group_id: int | str, message: str | list[dict[str, Any]],
        source: str = "auto",
    ) -> None:
        """Send a group message via a specific bot (records message_id for reply detection)."""
        await self._send_tracked(
            bot_id, "send_group_msg",
            {"group_id": int(group_id), "message": message},
            "group", group_id, source=source,
        )

    async def send_private_msg(
        self, bot_id: str, user_id: int | str, message: str | list[dict[str, Any]],
        source: str = "auto", *, wait_response: bool = False,
    ) -> dict[str, Any] | None:
        """Compatible fire-and-forget by default; tracked callers get explicit delivery status."""
        if wait_response and not self._bot_manager.get(bot_id):
            return {"delivery_status": "failed"}
        outcome: dict[str, Any] = {}
        response = await self._send_tracked(
            bot_id, "send_private_msg",
            {"user_id": int(user_id), "message": message},
            "private", user_id, source=source, wait_response=wait_response,
            outcome=outcome,
        )
        if not wait_response:
            return response
        payload = dict(response or {})
        result_status = str(outcome.get("status") or "")
        if result_status:
            payload["delivery_status"] = {
                "sent": "success", "failed": "failed", "blocked": "failed",
            }.get(result_status, "unknown")
        else:
            # 兼容测试/外部 patch(未提供 outcome)时按旧响应语义推断
            from mohobot.services.persona_ab import delivery_status
            payload["delivery_status"] = delivery_status(response)
        return payload

    async def send_message_with_status(
        self, bot_id: str, chat_type: str, chat_id: int | str,
        message: str | list[dict[str, Any]], *, source: str = "llm",
        ack_timeout: float = 5.0,
        dispatch_guard: Callable[[], bool] | None = None,
    ) -> DeliveryResult:
        """发送消息并返回投递结果(issue #128)。

        与 send_group_msg/send_private_msg 的旧契约(返回 None)并存: 需要
        判断"这条内容是否真的发出"的主链使用本方法。返回 "sent" 表示收到
        成功回执; "unknown" 表示已派发但回执未确认(不得当作 sent 记成功);
        "failed"/"blocked" 表示确定未发出, 不得写入对话记忆。流水线模式
        (ack_timeout<=0) 仍会等 _QUICK_ACK_WINDOW 捕获快速负 ack。
        """
        if chat_type == "group":
            action = "send_group_msg"
            params: dict[str, Any] = {"group_id": int(chat_id), "message": message}
        else:
            action = "send_private_msg"
            params = {"user_id": int(chat_id), "message": message}
        outcome: dict[str, Any] = {}
        if ack_timeout <= 0:
            # 流水线模式(issue #128 review): 流式分段发送逐段同步等回执会把
            # 慢适配器串成 ack_timeout/段; 这里只负责派发, 但额外等一个很短的
            # "快速负 ack"窗口(见 _QUICK_ACK_WINDOW), 让协议端立即返回的
            # failed/retcode!=0 仍能把该段判为未发出。窗口内无回执记 unknown,
            # 回执与归档由后台继续等(见 _send_tracked wait_response=False)。
            try:
                await self._send_tracked(
                    bot_id, action, params, chat_type, chat_id,
                    wait_response=False, source=source, outcome=outcome,
                    quick_ack_timeout=self._QUICK_ACK_WINDOW,
                    dispatch_guard=dispatch_guard,
                )
            except Exception as e:  # noqa: BLE001 - 发送异常要显式反馈给调用方
                logger.warning(
                    f"发送消息失败(bot={bot_id} {chat_type}:{chat_id}): {e}"
                )
                return DeliveryResult(status="failed", reason=f"send_error: {e}")
            status = str(outcome.get("status") or "unknown")
            if status == "sent":
                return DeliveryResult(status="sent", confirmed=True, reason="")
            return DeliveryResult(
                status=status,
                confirmed=False,
                reason=str(outcome.get("reason") or ""),
            )
        try:
            response = await self._send_tracked(
                bot_id, action, params, chat_type, chat_id,
                wait_response=True, timeout=ack_timeout, source=source,
                outcome=outcome, dispatch_guard=dispatch_guard,
            )
        except Exception as e:  # noqa: BLE001 - 发送异常要显式反馈给调用方
            logger.warning(
                f"发送消息失败(bot={bot_id} {chat_type}:{chat_id}): {e}"
            )
            return DeliveryResult(status="failed", reason=f"send_error: {e}")
        status = str(outcome.get("status") or "unknown")
        if status == "sent":
            return DeliveryResult(status="sent", confirmed=True, response=response)
        return DeliveryResult(
            status=status, response=response,
            reason=str(outcome.get("reason") or ""),
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
        self, bot_id: str, group_id: int | str, nodes: list[dict[str, Any]],
        source: str = "auto",
    ) -> None:
        """发送合并转发消息(OneBot 扩展 API, NapCat 等客户端支持)。

        nodes: 自定义节点数组, 每节点:
          {"type": "node", "data": {"user_id": "QQ", "nickname": "名字",
                                    "content": [{"type": "text", "data": {"text": "..."}}]}}
        """
        await self._send_tracked(
            bot_id, "send_group_forward_msg",
            {"group_id": int(group_id), "messages": nodes},
            "group", group_id, source=source,
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
