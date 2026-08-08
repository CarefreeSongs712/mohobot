"""Central message processing pipeline.

Flow:
  1. Receive raw OneBot event
  2. Archive to history/ (JSONL)
  3. Classify: notice/meta → plugin hooks; message → process
  4. Group gate: only respond if @mentioned, replied-to, or command
  5. Interceptors run (commands → keywords → plugins)
  6. Context load → LLM streaming → reply-quote first chunk → subsequent chunks
  7. Save context after streaming completes
"""

from __future__ import annotations

import asyncio
import random
import time as time_module
from typing import Any

from loguru import logger

from mohobot.models.onebot import (
    Event,
    GroupMessageEvent,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    PrivateMessageEvent,
    RequestEvent,
)
from mohobot.file_store import JSONLWriter
from mohobot.utils.cq_code import extract_plain_text


class MessageHandler:
    """Orchestrates the message processing pipeline."""

    def __init__(
        self,
        ws_server,
        context_manager,
        llm_service,
        plugin_system,
        data_dir: str = "./data",
        context_max_rounds: int = 30,
        reply_config=None,
    ):
        self._ws = ws_server
        self._ctx_mgr = context_manager
        self._llm = llm_service
        self._plugins = plugin_system
        self._data_dir = data_dir
        self._context_max_rounds = context_max_rounds
        self._interceptors: list = []  # Ordered list of interceptors
        self._writer_registry: dict[str, JSONLWriter] = {}

        # Reply behavior from global config (stream/segment/delay/quote)
        if reply_config is None:
            from mohobot.models.config import ReplyConfig
            reply_config = ReplyConfig()
        self._segment_reply = reply_config.segment_reply
        self._seg_min_len = reply_config.segment_min_len
        self._seg_max_len = reply_config.segment_max_len
        self._seg_delay_min = reply_config.segment_delay_min
        self._seg_delay_max = reply_config.segment_delay_max
        self._reply_quote = reply_config.reply_quote
        self._stream = reply_config.stream

        # Image rate-limiting: track last image time per (bot_id, user_id)
        self._last_image_time: dict[str, float] = {}
        self._image_cooldown = 10.0  # seconds

    def set_interceptors(self, interceptors: list) -> None:
        """Set the ordered interceptor chain."""
        self._interceptors = interceptors
        logger.info(f"MessageHandler: {len(interceptors)} interceptor(s) registered")

    async def handle_event(self, bot_id: str, event: Event, raw: dict[str, Any]) -> None:
        """Entry point for all incoming events."""
        try:
            # Step 1: Archive to history (if it's a message event)
            if isinstance(event, MessageEvent):
                await self._archive_event(bot_id, event, raw)

            # Step 2: Classify and dispatch
            if isinstance(event, MessageEvent):
                await self._handle_message(bot_id, event, raw)
            elif isinstance(event, NoticeEvent):
                await self._handle_notice(bot_id, event, raw)
            elif isinstance(event, RequestEvent):
                await self._handle_request(bot_id, event, raw)
            elif isinstance(event, MetaEvent):
                await self._handle_meta(bot_id, event, raw)
            else:
                logger.debug(f"Unhandled event type from bot {bot_id}: {raw.get('post_type')}")
        except Exception as e:
            logger.exception(f"Error handling event from bot {bot_id}: {e}")

    async def _archive_event(self, bot_id: str, event: MessageEvent, raw: dict) -> None:
        """Write raw event to history JSONL."""
        if isinstance(event, PrivateMessageEvent):
            file_path = f"{self._data_dir}/history/{bot_id}/private/{event.user_id}.jsonl"
        elif isinstance(event, GroupMessageEvent):
            file_path = f"{self._data_dir}/history/{bot_id}/group/{event.group_id}.jsonl"
        else:
            return

        writer = self._get_or_create_writer(file_path)
        await writer.append(raw)

    def _get_or_create_writer(self, file_path: str) -> JSONLWriter:
        """Get or create a JSONLWriter for the given path."""
        if file_path not in self._writer_registry:
            self._writer_registry[file_path] = JSONLWriter(file_path)
        return self._writer_registry[file_path]

    async def _should_respond_to_group(self, bot_id: str, event: GroupMessageEvent) -> bool:
        """Check if the bot should respond in a group setting.

        Returns True ONLY if:
          - The message starts with command prefix (/)
          - The bot is @mentioned DIRECTLY (by its own QQ, not @all)
          - The message is a reply (quote) of a message the BOT ITSELF sent
        """
        # Always respond to commands
        text = extract_plain_text(event.message)
        if text.startswith("/"):
            return True

        # Direct @mention of the bot
        if event.is_mentioned(bot_id):
            return True

        # Reply quoting the bot's own message
        if await self._is_reply_to_bot(bot_id, event):
            return True

        return False

    async def _is_reply_to_bot(self, bot_id: str, event: GroupMessageEvent) -> bool:
        """Check if the message quotes a message that the bot itself sent.

        Uses the sent-message tracking on BotInstance: a reply only counts
        if its quoted message_id matches one the bot has sent in this group.
        """
        if not isinstance(event.message, list):
            return False

        instance = None
        if self._ws and self._ws._bot_manager:
            instance = self._ws._bot_manager.get(bot_id)
        if instance is None:
            return False

        for seg in event.message:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "reply":
                quoted_id = seg.get("data", {}).get("id")
                if quoted_id and instance.is_my_message("group", event.group_id, quoted_id):
                    return True
        return False

    async def _check_image_rate_limit(self, bot_id: str, event: PrivateMessageEvent, raw: dict) -> bool:
        """Check if images should be stripped due to rapid-fire image sending.

        Returns True if images were removed (rate-limited), False otherwise.
        """
        from mohobot.utils.cq_code import extract_image_urls

        image_urls = extract_image_urls(event.message)
        if not image_urls:
            return False

        key = f"{bot_id}:{event.user_id}"
        now = time_module.time()
        last_time = self._last_image_time.get(key, 0)

        if now - last_time < self._image_cooldown:
            # Rate-limited: strip images from the message, keep text only
            logger.debug(f"Image rate-limited for {key}: {now - last_time:.1f}s since last image")
            return True

        self._last_image_time[key] = now
        return False

    async def _handle_message(self, bot_id: str, event: MessageEvent, raw: dict) -> None:
        """Process a message event through the pipeline."""
        text_preview = extract_plain_text(event.message)[:80]
        logger.info(
            f"Message from bot={bot_id}, user={event.user_id}, "
            f"type={event.message_type}, text='{text_preview}'"
        )

        # ── Group gate: only respond if @mentioned, replied-to, or command ──
        if isinstance(event, GroupMessageEvent):
            if not await self._should_respond_to_group(bot_id, event):
                logger.debug(f"Skipping group message (not mentioned): user={event.user_id}")
                return

        # ── Private chat image rate-limiting ──
        if isinstance(event, PrivateMessageEvent):
            if await self._check_image_rate_limit(bot_id, event, raw):
                # Strip images from the message so LLM only sees text
                if isinstance(event.message, list):
                    event.message = [
                        seg for seg in event.message
                        if not (isinstance(seg, dict) and seg.get("type") == "image")
                    ]

        # ── Run interceptor chain ──
        for interceptor in self._interceptors:
            handled, response = await interceptor.intercept(bot_id, event, raw)
            if handled:
                if response:
                    await self._send_reply(bot_id, event, response)
                return

        # ── LLM path: streaming with reply-quote ──
        chat_type = self._get_chat_type(event)
        chat_id = self._get_chat_id(event)

        # Load session context
        context = await self._ctx_mgr.load_context(bot_id, chat_type, chat_id)

        # Stream response — split by punctuation + length (标点符号+长度分隔法)
        full_reply = await self._stream_llm_reply(bot_id, event, context, raw)

        # Save context after streaming completes
        if full_reply.strip():
            user_msg = {
                # Role = "QQ号-昵称" so the LLM knows WHO said it
                "role": self._speaker_role(event),
                "content": extract_plain_text(event.message) or str(event.message),
                "timestamp": event.time,
            }
            ai_msg = {
                "role": "assistant",
                "content": full_reply,
                "timestamp": int(time_module.time()),
            }
            await self._ctx_mgr.append_context(
                bot_id, chat_type, chat_id, [user_msg, ai_msg],
                max_rounds=self._context_max_rounds,
            )

    @staticmethod
    def _speaker_role(event: MessageEvent) -> str:
        """Build a speaker role string: \"{qq}-{nickname}\" (e.g. 3831097597-墨染荷韵)."""
        if isinstance(event, GroupMessageEvent):
            nickname = event.sender.card or event.sender.nickname or f"User-{event.user_id}"
        else:
            nickname = event.sender.nickname or f"User-{event.user_id}"
        return f"{event.user_id}-{nickname}"

    # ── Reply behavior (config-driven) ────────────────────────

    # Strong break punctuation (sentence end). NOTE: single \n is NOT a break —
    # double newline (\n\n) is handled separately in _find_cut as a paragraph break.
    _PUNCT_STRONG = "。！？!?…"
    # Soft break punctuation (clause end)
    _PUNCT_SOFT = "；;，,、"

    async def _stream_llm_reply(self, bot_id, event, context, raw) -> str:
        """Generate and send the LLM reply per the configured behavior.

        - stream=True:    逐 token 流式接收
        - stream=False:   一次性等待完整回复
        - segment_reply:  按标点+长度切分为多条消息发送
        - segment_delay:  分段之间的随机延迟区间
        - reply_quote:    首条回复是否引用触发消息
        Returns the full assembled reply text (for context save).
        """
        if not self._segment_reply:
            # Non-segmented: collect everything, send as ONE message at the end
            return await self._send_single_reply(bot_id, event, context, raw)

        # Non-streaming path: single blocking call, then segment & send
        if not self._stream:
            reply_text, _ = await self._llm.chat(
                bot_id=bot_id,
                event=event,
                context=context,
                raw_event=raw,
            )
            full_reply = reply_text or ""
            await self._send_full_text(bot_id, event, full_reply)
            return full_reply

        buffer = ""
        full_reply = ""
        first_sent = False
        segments_sent = 0

        async for chunk, is_final in self._llm.chat_stream(
            bot_id=bot_id,
            event=event,
            context=context,
            raw_event=raw,
        ):
            if chunk:
                buffer += chunk
                full_reply += chunk

            # Flush complete segments from the buffer
            flushed = self._flush_ready_segments(buffer)
            buffer = flushed["rest"]
            for seg in flushed["segments"]:
                segments_sent += 1
                first_sent = await self._send_segment(bot_id, event, seg, first_sent)

            if is_final and buffer:
                # Stream ended with text left in the buffer — always flush it
                segments_sent += 1
                first_sent = await self._send_segment(bot_id, event, buffer, first_sent)
                buffer = ""

        logger.debug(
            f"Streamed reply done: {segments_sent} segment(s), "
            f"total {len(full_reply)} chars"
        )
        return full_reply

    async def _send_single_reply(self, bot_id, event, context, raw) -> str:
        """Non-segmented path: wait for full reply (streaming or not), send once.

        With stream=True the chunks are still consumed incrementally (so the
        LLM call isn't wasted) but only the final assembled text is sent.
        """
        full_reply = ""
        async for chunk, _ in self._llm.chat_stream(
            bot_id=bot_id,
            event=event,
            context=context,
            raw_event=raw,
        ):
            if chunk:
                full_reply += chunk

        text = full_reply.strip()
        if text:
            if self._reply_quote:
                message = [
                    {"type": "reply", "data": {"id": str(event.message_id)}},
                    {"type": "text", "data": {"text": text}},
                ]
            else:
                message = text
            await self._send_message(bot_id, event, message)
        logger.debug(f"Single reply sent: {len(full_reply)} chars")
        return full_reply

    async def _send_full_text(self, bot_id, event, text: str) -> None:
        """Segment a complete reply text and send with configured delays."""
        first_sent = False
        for seg in self._flush_ready_segments(text)["segments"]:
            first_sent = await self._send_segment(bot_id, event, seg, first_sent)
        if text.strip() and not first_sent:
            await self._send_segment(bot_id, event, text, first_sent)

    async def _send_segment(self, bot_id, event, seg: str, first_sent: bool) -> bool:
        """Send one segmented message, stripped of whitespace.

        Returns the updated first_sent flag.
        """
        seg = seg.strip()
        if not seg:
            return first_sent  # Empty after strip — skip

        if first_sent:
            # Random delay between consecutive messages (config-driven)
            await asyncio.sleep(random.uniform(self._seg_delay_min, self._seg_delay_max))
            message: str | list[dict] = seg
        else:
            if self._reply_quote:
                # First segment quotes the user's message
                message = [
                    {"type": "reply", "data": {"id": str(event.message_id)}},
                    {"type": "text", "data": {"text": seg}},
                ]
            else:
                message = seg
            first_sent = True

        await self._send_message(bot_id, event, message)
        return first_sent

    def _flush_ready_segments(self, buffer: str) -> dict:
        """Split buffered text into ready-to-send segments.

        Rules (标点符号+长度分隔法):
          1. Double newline (\n\n) — paragraph break, ALWAYS splits (no min length)
          2. Hard cap: force-flush at _SEG_MAX_LEN, cutting at last punctuation
          3. Segment ≥ _SEG_MIN_LEN may flush at last strong/soft punctuation
        """
        segments: list[str] = []

        while True:
            # 1. Double newline paragraph break — split regardless of length
            idx = buffer.find("\n\n")
            if idx != -1:
                cut = idx + 2
                segments.append(buffer[:cut])
                buffer = buffer[cut:]
                continue

            # 2. Hard cap reached — cut at last punctuation within cap, else force
            if len(buffer) >= self._seg_max_len:
                cut = self._find_cut(buffer[: self._seg_max_len])
                if cut is None:
                    cut = self._seg_max_len
                segments.append(buffer[:cut])
                buffer = buffer[cut:]
                continue

            # 3. Min length + punctuation break
            if len(buffer) >= self._seg_min_len:
                cut = self._find_cut(buffer)
                if cut is not None and cut >= self._seg_min_len:
                    segments.append(buffer[:cut])
                    buffer = buffer[cut:]
                    continue

            break

        return {"segments": segments, "rest": buffer}

    def _find_cut(self, text: str) -> int | None:
        """Find the cut position: last \\n\\n, else last strong, else last soft punct."""
        idx = text.rfind("\n\n")
        if idx != -1 and idx + 2 <= len(text):
            return idx + 2
        for i in range(len(text) - 1, -1, -1):
            if text[i] in self._PUNCT_STRONG:
                return i + 1
        for i in range(len(text) - 1, -1, -1):
            if text[i] in self._PUNCT_SOFT:
                return i + 1
        return None

    async def _handle_notice(self, bot_id: str, event: NoticeEvent, raw: dict) -> None:
        """Handle notice events — dispatch to plugins."""
        logger.debug(f"Notice from bot {bot_id}: {event.notice_type}")
        await self._plugins.dispatch_notice(bot_id, event, raw)

    async def _handle_request(self, bot_id: str, event: RequestEvent, raw: dict) -> None:
        """Handle request events (friend add, group invite) — auto-approve for now."""
        logger.info(f"Request from bot {bot_id}: {event.request_type} from {event.user_id}")
        if event.request_type == "friend":
            await self._ws.send_to_bot(bot_id, "set_friend_add_request", {
                "flag": event.flag,
                "approve": True,
            })
        elif event.request_type == "group":
            await self._ws.send_to_bot(bot_id, "set_group_add_request", {
                "flag": event.flag,
                "sub_type": event.sub_type or "add",
                "approve": True,
            })

    async def _handle_meta(self, bot_id: str, event: MetaEvent, raw: dict) -> None:
        """Handle meta events (heartbeat, lifecycle)."""
        if event.meta_event_type == "heartbeat":
            logger.debug(f"Heartbeat from bot {bot_id}")
        elif event.meta_event_type == "lifecycle":
            logger.info(f"Bot {bot_id} lifecycle: {raw.get('sub_type', 'connect')}")
        await self._plugins.dispatch_meta(bot_id, event, raw)

    async def _send_message(
        self, bot_id: str, event: MessageEvent, message: str | list[dict]
    ) -> None:
        """Send a message to the appropriate chat (private or group)."""
        if isinstance(event, PrivateMessageEvent):
            await self._ws.send_private_msg(bot_id, event.user_id, message)
        elif isinstance(event, GroupMessageEvent):
            await self._ws.send_group_msg(bot_id, event.group_id, message)

    async def _send_reply(
        self, bot_id: str, event: MessageEvent, reply: str | list[dict]
    ) -> None:
        """Alias for _send_message — send a reply."""
        await self._send_message(bot_id, event, reply)

    def _get_chat_type(self, event: MessageEvent) -> str:
        if isinstance(event, PrivateMessageEvent):
            return "private"
        return "group"

    def _get_chat_id(self, event: MessageEvent) -> str:
        if isinstance(event, PrivateMessageEvent):
            return str(event.user_id)
        return str(event.group_id)

    async def close(self) -> None:
        """Close all open file writers."""
        for writer in self._writer_registry.values():
            await writer.close()