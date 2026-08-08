"""LLM service — OpenAI-compatible chat and vision model interaction.

Handles prompt assembly, tool calling, vision integration, and response generation.
"""

from __future__ import annotations

import os
import time
import json
from typing import Any, AsyncGenerator

from loguru import logger
from openai import AsyncOpenAI

from mohobot.models.config import GlobalConfig, BotConfig
from mohobot.models.onebot import (
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from mohobot.utils.cq_code import extract_plain_text, extract_image_urls


class LLMService:
    """LLM interaction service with prompt assembly and vision support."""

    def __init__(self, global_config: GlobalConfig):
        self._cfg = global_config
        self._available = False

        api_key = self._cfg.llm.chat_api_key or os.environ.get("MOHOBOT_LLM_API_KEY", "")
        vision_key = self._cfg.llm.vision_api_key or os.environ.get("MOHOBOT_VISION_API_KEY", "") or api_key

        # Initialize chat client (lazy init — allow empty key for testing)
        if api_key:
            self._chat_client = AsyncOpenAI(
                api_key=api_key,
                base_url=self._cfg.llm.chat_base_url,
            )
            self._available = True
        else:
            self._chat_client = None
            logger.warning("LLM chat API key not configured — LLM calls will fail")

        # Initialize vision client (can be same or different provider)
        if vision_key and vision_key != api_key:
            self._vision_client = AsyncOpenAI(
                api_key=vision_key,
                base_url=self._cfg.llm.vision_base_url or self._cfg.llm.chat_base_url,
            )
        elif self._chat_client:
            self._vision_client = self._chat_client
        else:
            self._vision_client = None

        # System prompt building blocks
        self._tools_schemas: list[dict] = [
            {
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "description": "获取当前日期和时间",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_group_member_info",
                    "description": "获取群成员信息",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "group_id": {
                                "type": "integer",
                                "description": "群号",
                            },
                            "user_id": {
                                "type": "integer",
                                "description": "QQ 号",
                            },
                        },
                        "required": ["group_id", "user_id"],
                    },
                },
            },
        ]

    async def chat(
        self,
        bot_id: str,
        event: MessageEvent,
        context: list[dict[str, Any]],
        raw_event: dict[str, Any],
        bot_config: BotConfig | None = None,
    ) -> tuple[str | None, list[dict[str, Any]] | None]:
        """Process a message through the LLM.

        Returns:
            (reply_text, tool_results) — tool_results may be None if no tools were called.
        """
        # Check if LLM is available
        if not self._available or self._chat_client is None:
            logger.warning("LLM not configured — cannot process message")
            return "LLM 服务未配置（缺少 API Key），请在 config/global.yaml 中设置。", None

        # Determine which model and client to use
        model = self._cfg.llm.chat_model
        temperature = self._cfg.llm.chat_temperature
        max_tokens = self._cfg.llm.chat_max_tokens
        client = self._chat_client

        # Check for image content — use vision model if images present
        has_images = bool(extract_image_urls(event.message))
        if has_images and self._cfg.llm.vision_api_key:
            model = self._cfg.llm.vision_model
            client = self._vision_client
            logger.debug(f"Using vision model {model} for message with images")

        # Allow per-bot model override
        if bot_config and bot_config.chat_model_override:
            model = bot_config.chat_model_override

        # Build messages array
        messages = await self._build_messages(bot_id, event, context, bot_config)

        logger.debug(
            f"LLM call: model={model}, messages={len(messages)}, "
            f"context_len={len(context)}"
        )

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=self._tools_schemas,
                tool_choice="auto",
            )
        except Exception as e:
            logger.error(f"LLM API call failed: {e}")
            return f"[LLM 调用失败: {e}]", None

        choice = response.choices[0] if response.choices else None
        if not choice:
            return None, None

        reply_text = choice.message.content or ""
        tool_calls = choice.message.tool_calls

        # Handle tool calls
        tool_results = None
        if tool_calls:
            tool_results = []
            for tc in tool_calls:
                result = await self._execute_tool(tc.function.name, tc.function.arguments)
                tool_results.append({
                    "tool_call_id": tc.id,
                    "function_name": tc.function.name,
                    "arguments": tc.function.arguments,
                    "result": result,
                })
                reply_text += f"\n[工具调用: {tc.function.name} → {result}]"

        return reply_text, tool_results

    async def chat_stream(
        self,
        bot_id: str,
        event: MessageEvent,
        context: list[dict[str, Any]],
        raw_event: dict[str, Any],
        bot_config: BotConfig | None = None,
    ) -> AsyncGenerator[tuple[str, bool], None]:
        """Streaming LLM chat. Yields (text_chunk, is_final) tuples.

        When is_final=True, that chunk may include tool call results.
        The caller should send individual chunks as they arrive.
        """
        if not self._available or self._chat_client is None:
            yield ("LLM 服务未配置（缺少 API Key），请在 config/global.yaml 中设置。", True)
            return

        model = self._cfg.llm.chat_model
        temperature = self._cfg.llm.chat_temperature
        max_tokens = self._cfg.llm.chat_max_tokens
        client = self._chat_client

        # Check for image content
        has_images = bool(extract_image_urls(event.message))
        if has_images and self._cfg.llm.vision_api_key and self._vision_client:
            model = self._cfg.llm.vision_model
            client = self._vision_client
            logger.debug(f"Using vision model {model} for streaming with images")

        if bot_config and bot_config.chat_model_override:
            model = bot_config.chat_model_override

        messages = await self._build_messages(bot_id, event, context, bot_config)

        # Cap max_tokens — some gateways return an EMPTY stream for huge values
        # (verified: 409600 → 0 chunks, 4096~131072 all work)
        max_tokens = min(self._cfg.llm.chat_max_tokens, 131072)

        logger.debug(
            f"LLM stream call: model={model}, messages={len(messages)}, "
            f"context_len={len(context)}, max_tokens={max_tokens}"
        )

        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=self._tools_schemas,
                tool_choice="auto",
                stream=True,
                stream_options={"include_usage": False},
            )
        except Exception as e:
            logger.error(f"LLM stream call failed: {e}")
            yield (f"[LLM 调用失败: {e}]", True)
            return

        full_content = ""
        tool_calls_buffer: dict[int, dict] = {}
        got_any_data = False

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            # Accumulate text content
            if delta.content:
                got_any_data = True
                full_content += delta.content
                yield (delta.content, False)

            # Accumulate tool calls
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {
                            "id": tc.id or "",
                            "function_name": tc.function.name or "",
                            "arguments": tc.function.arguments or "",
                        }
                    else:
                        if tc.id:
                            tool_calls_buffer[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls_buffer[idx]["function_name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls_buffer[idx]["arguments"] += tc.function.arguments

        # After stream ends, execute tool calls if any
        if tool_calls_buffer:
            got_any_data = True
            tool_results = []
            for idx, tc_data in sorted(tool_calls_buffer.items()):
                args_str = tc_data.get("arguments", "{}") or "{}"
                result = await self._execute_tool(tc_data["function_name"], args_str)
                tool_results.append(result)
                full_content += f"\n[工具调用: {tc_data['function_name']} → {result}]"

            if tool_results:
                yield (f"\n[工具: {'; '.join(tool_results)}]", True)
                return

        # Empty stream guard: some gateways return 0 chunks for unsupported
        # max_tokens / model combos — surface the problem instead of staying silent
        if not got_any_data:
            logger.warning(
                f"LLM stream returned NO data (model={model}, max_tokens={max_tokens}) — "
                "gateway may not support this combo"
            )
            yield ("[模型未返回内容——请检查 max_tokens 或模型配置]", True)
            return

        yield ("", True)  # Signal completion with no extra text

    async def _build_messages(
        self,
        bot_id: str,
        event: MessageEvent,
        context: list[dict[str, Any]],
        bot_config: BotConfig | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete messages array for the LLM call.

        Order:
          1. System prompt (persona)
          2. Tools definition (already in API call)
          3. User profile info
          4. Session context (from context manager)
          5. Current time and input message
        """
        messages: list[dict[str, Any]] = []

        # 1. System prompt
        persona = bot_config.persona if bot_config and bot_config.persona else "你是 Mohobot，一个有用的 AI 助手。"
        system_content = persona

        # Add user profile info to system prompt
        if isinstance(event, GroupMessageEvent):
            sender_name = event.sender.card or event.sender.nickname or f"User-{event.user_id}"
            system_content += (
                f"\n\n当前对话环境：群聊（群号: {event.group_id}）\n"
                f"发送者: {sender_name} (QQ: {event.user_id})\n"
                f"机器人昵称: {bot_config.nickname if bot_config else 'Mohobot'}"
            )
        elif isinstance(event, PrivateMessageEvent):
            sender_name = event.sender.nickname or f"User-{event.user_id}"
            system_content += (
                f"\n\n当前对话环境：私聊\n"
                f"发送者: {sender_name} (QQ: {event.user_id})"
            )

        messages.append({"role": "system", "content": system_content})

        # 2. Session context — insert as alternating user/assistant messages.
        #    Context roles are either "user"/"assistant" or "{qq}-{nickname}"
        #    (e.g. "3831097597-墨染荷韵") — named roles are prefixed so the
        #    model knows exactly who said what.
        for entry in context:
            role = entry.get("role", "user")
            content = entry.get("content", "")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": content})
            else:
                # Named speaker role, e.g. "3831097597-墨染荷韵"
                messages.append({
                    "role": "user",
                    "content": f"[{role}]: {content}",
                })

        # 3. Current time
        now = time.strftime("%Y-%m-%d %H:%M:%S %A")
        time_msg = f"当前时间: {now}"

        # 4. Build user input message
        user_text = extract_plain_text(event.message)

        # Handle image messages — only process the FIRST image to prevent flooding
        image_urls = extract_image_urls(event.message)
        if image_urls and len(image_urls) > 1:
            logger.debug(f"Limiting {len(image_urls)} images to first 1 for LLM input")
        image_urls = image_urls[:1]  # Never send more than 1 image per message

        user_content: str | list = user_text or ""

        if image_urls:
            # Multi-modal message: text + first image only
            content_parts: list[dict] = []
            if user_text:
                content_parts.append({"type": "text", "text": user_text})
            for url in image_urls:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": url},
                })
            user_content = content_parts

        # 5. Final user message — the @mention check is now done in message_handler.py
        if isinstance(user_content, list):
            # Prepend time info as text
            user_content.insert(0, {"type": "text", "text": time_msg})
        else:
            user_content = f"{time_msg}\n\n{user_content}" if user_content else time_msg

        messages.append({"role": "user", "content": user_content})

        return messages

    async def _execute_tool(self, func_name: str, args_json: str) -> str:
        """Execute a tool/function call and return the result."""
        try:
            args = json.loads(args_json) if args_json else {}
        except json.JSONDecodeError:
            args = {}

        if func_name == "get_current_time":
            return time.strftime("%Y-%m-%d %H:%M:%S")
        elif func_name == "get_group_member_info":
            # This would need a bot connection to call the API
            return json.dumps({"error": "不在 WebSocket 连接中无法获取成员信息"}, ensure_ascii=False)
        else:
            return json.dumps({"error": f"未知工具: {func_name}"}, ensure_ascii=False)

    async def close(self) -> None:
        """Close the HTTP clients."""
        if self._chat_client:
            await self._chat_client.close()
        if self._vision_client and self._vision_client is not self._chat_client:
            await self._vision_client.close()