"""Reverse WebSocket server (Universal Client mode).

Listens for incoming OneBot v11 connections on a configurable port.
Each connecting OneBot instance sends X-Self-ID and X-Client-Role headers.
This server handles Universal clients — both event push and API calls
are multiplexed on the same connection per bot.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Awaitable

import websockets
import websockets.asyncio.server
from loguru import logger

from mohobot.models.onebot import Event
from mohobot.bot_manager import BotManager

# Type alias for the event callback
EventCallback = Callable[[str, Event, dict[str, Any]], Awaitable[None]]


class WSServer:
    """Reverse WebSocket server for OneBot v11 Universal Client connections."""

    def __init__(
        self,
        bot_manager: BotManager,
        host: str = "0.0.0.0",
        port: int = 8080,
        max_size: int = 10 * 1024 * 1024,
    ):
        self._host = host
        self._port = port
        self._max_size = max_size
        self._bot_manager = bot_manager
        self._on_event: EventCallback | None = None
        self._server: websockets.asyncio.server.Server | None = None
        self._heartbeat_interval: float = 30.0  # seconds

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
        """Gracefully stop the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("WebSocket server stopped")

    async def _handle_connection(
        self, websocket: websockets.asyncio.server.ServerConnection
    ) -> None:
        """Handle an incoming WebSocket connection from a OneBot instance."""
        # Extract headers
        headers = dict(websocket.request.headers)
        bot_id = headers.get("x-self-id", headers.get("X-Self-ID", "unknown"))
        client_role = headers.get(
            "x-client-role", headers.get("X-Client-Role", "Universal")
        )

        logger.info(
            f"New connection: bot_id={bot_id}, role={client_role}, "
            f"remote={websocket.remote_address}"
        )

        # Register bot
        instance = self._bot_manager.register(bot_id, websocket)
        logger.info(f"Bot {bot_id} ({instance.nickname}) connected")

        try:
            async for raw_message in websocket:
                try:
                    data = json.loads(raw_message)
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON from bot {bot_id}: {e}")
                    continue

                await self._dispatch(bot_id, data)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"Bot {bot_id} disconnected: {e.code} {e.reason}")
        except Exception as e:
            logger.error(f"Connection error for bot {bot_id}: {e}")
        finally:
            self._bot_manager.unregister(bot_id)

    async def _dispatch(self, bot_id: str, data: dict[str, Any]) -> None:
        """Dispatch an incoming message — event or API response."""
        # Check if it's an API response (has 'status' field and 'echo')
        if "status" in data and "echo" in data:
            # Route API response
            await self._bot_manager.handle_api_response(bot_id, data)
            return

        # It's an event (has 'post_type')
        if "post_type" in data:
            event = Event.from_dict(data)
            if self._on_event:
                await self._on_event(bot_id, event, data)
            return

        # Unknown message type
        logger.debug(f"Unknown message from bot {bot_id}: {data}")

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
        instance = self._bot_manager.get(bot_id)
        if not instance:
            logger.warning(f"Cannot send to bot {bot_id}: not connected")
            return None

        payload = {"action": action, "params": params or {}}

        if not wait_response:
            await instance.send(payload)
            return None

        # Generate unique echo and wait for the response
        import uuid
        echo = f"api_{uuid.uuid4().hex}"
        payload["echo"] = echo
        future = self._bot_manager.create_response_future(echo)
        await instance.send(payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"API response timeout for {action} (bot {bot_id})")
            return None

    async def send_group_msg(
        self, bot_id: str, group_id: int | str, message: str | list[dict[str, Any]]
    ) -> None:
        """Send a group message via a specific bot (records message_id for reply detection)."""
        resp = await self.send_to_bot(
            bot_id, "send_group_msg",
            {"group_id": int(group_id), "message": message},
            wait_response=True,
        )
        # Record the sent message_id so replies quoting it can trigger the bot
        if resp and isinstance(resp.get("data"), dict):
            mid = resp["data"].get("message_id")
            if mid is not None:
                instance = self._bot_manager.get(bot_id)
                if instance:
                    instance.record_sent_message("group", group_id, mid)

    async def send_private_msg(
        self, bot_id: str, user_id: int | str, message: str | list[dict[str, Any]]
    ) -> None:
        """Send a private message via a specific bot (records message_id)."""
        resp = await self.send_to_bot(
            bot_id, "send_private_msg",
            {"user_id": int(user_id), "message": message},
            wait_response=True,
        )
        if resp and isinstance(resp.get("data"), dict):
            mid = resp["data"].get("message_id")
            if mid is not None:
                instance = self._bot_manager.get(bot_id)
                if instance:
                    instance.record_sent_message("private", user_id, mid)