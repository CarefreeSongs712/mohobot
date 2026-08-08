"""Bot lifecycle management.

Maintains a registry of connected bots (keyed by bot QQ / X-Self-ID),
loads/saves per-bot configuration, and provides API call routing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiofiles
import aiofiles.os
from loguru import logger

from mohobot.models.config import BotConfig


class BotInstance:
    """Represents a connected bot instance."""

    def __init__(self, bot_id: str, websocket: "websockets.WebSocketServerProtocol", config: BotConfig):
        self.bot_id: str = bot_id
        self.ws: "websockets.WebSocketServerProtocol" = websocket
        self.config: BotConfig = config
        self.connected_at: float = asyncio.get_event_loop().time()
        self.message_count: int = 0
        self._send_lock: asyncio.Lock = asyncio.Lock()
        # Track message IDs this bot has SENT, per chat: {"group:123": {"456", "789"}}
        self._sent_messages: dict[str, set[str]] = {}

    def record_sent_message(self, chat_type: str, chat_id: int | str, message_id: int | str) -> None:
        """Record a message ID the bot sent, so replies quoting it can be detected."""
        key = f"{chat_type}:{chat_id}"
        self._sent_messages.setdefault(key, set()).add(str(message_id))

    def is_my_message(self, chat_type: str, chat_id: int | str, message_id: int | str) -> bool:
        """Check if a given message_id was sent by this bot in the given chat."""
        key = f"{chat_type}:{chat_id}"
        return str(message_id) in self._sent_messages.get(key, set())

    async def send(self, data: dict[str, Any]) -> None:
        """Send a JSON message to this bot via WebSocket (thread-safe)."""
        async with self._send_lock:
            try:
                payload = json.dumps(data, ensure_ascii=False)
                await self.ws.send(payload)
            except Exception as e:
                logger.error(f"Failed to send to bot {self.bot_id}: {e}")
                raise

    async def call_api(self, action: str, params: dict[str, Any] | None = None,
                       echo: str | None = None) -> dict[str, Any]:
        """Send an API call and wait for the response."""
        request = {"action": action, "params": params or {}}
        if echo:
            request["echo"] = echo
        await self.send(request)
        return request  # Response will be handled via the message handler

    @property
    def qq(self) -> int:
        return self.config.qq

    @property
    def nickname(self) -> str:
        return self.config.nickname if self.config.nickname else f"Bot-{self.bot_id}"


class BotManager:
    """Manages all connected bot instances."""

    def __init__(self, data_dir: str = "./data"):
        self._bots: dict[str, BotInstance] = {}
        self._data_dir = data_dir
        self._pending_responses: dict[str, asyncio.Future] = {}
        logger.info(f"BotManager initialized (data_dir={data_dir})")

    def register(self, bot_id: str, websocket: "websockets.WebSocketServerProtocol") -> BotInstance:
        """Register a newly connected bot."""
        config_path = Path(self._data_dir) / "bots" / bot_id / "config.json"
        config = BotConfig.load(config_path)

        # Auto-fill QQ from bot_id if not set
        if config.qq == 0:
            config.qq = int(bot_id)

        instance = BotInstance(bot_id, websocket, config)
        self._bots[bot_id] = instance
        logger.info(f"Bot registered: {bot_id} (QQ={config.qq}, nickname={config.nickname})")
        return instance

    def unregister(self, bot_id: str) -> None:
        """Remove a disconnected bot."""
        if bot_id in self._bots:
            del self._bots[bot_id]
            logger.info(f"Bot unregistered: {bot_id}")

    def get(self, bot_id: str) -> BotInstance | None:
        """Get a bot instance by ID."""
        return self._bots.get(bot_id)

    def get_by_qq(self, qq: int | str) -> BotInstance | None:
        """Find a bot by its QQ number."""
        qq_str = str(qq)
        for bot in self._bots.values():
            if str(bot.config.qq) == qq_str:
                return bot
        return None

    @property
    def all_bots(self) -> list[BotInstance]:
        return list(self._bots.values())

    @property
    def bot_count(self) -> int:
        return len(self._bots)

    async def save_bot_config(self, bot_id: str) -> None:
        """Save the bot's config to disk."""
        instance = self._bots.get(bot_id)
        if instance:
            config_path = Path(self._data_dir) / "bots" / bot_id / "config.json"
            instance.config.save(config_path)
            logger.info(f"Bot config saved: {bot_id}")

    async def handle_api_response(self, bot_id: str, response: dict[str, Any]) -> None:
        """Route an API response to the waiting caller."""
        echo = response.get("echo")
        if echo and echo in self._pending_responses:
            future = self._pending_responses.pop(echo)
            if not future.done():
                future.set_result(response)

    def create_response_future(self, echo: str) -> asyncio.Future:
        """Create a future for awaiting an API response."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses[echo] = future
        return future