"""LLM service — OpenAI-compatible chat and vision model interaction.

Handles prompt assembly, tool calling, vision integration, and response generation.
"""

from __future__ import annotations

import os
import time
import json
from pathlib import Path
from typing import Any, AsyncGenerator

import aiofiles
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

        # 视觉能力可用性: 有 key(含环境变量/回退 chat key)且配置了视觉模型。
        # 注意: 不能用 self._cfg.llm.vision_api_key 判断——env 变量/回退会被漏掉。
        self._vision_available = bool(
            vision_key and self._cfg.llm.vision_model and self._vision_client
        )

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
            {
                "type": "function",
                "function": {
                    "name": "anysearch_search",
                    "description": "实时联网搜索获取最新外部信息(新闻、百科、价格、事件等)",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索查询, 简洁明确",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
        ]

        # Anysearch 实时联网搜索(未配置 key 时工具自动移除)
        from mohobot.anysearch import AnySearchClient
        self._anysearch_client: AnySearchClient | None = None
        if self._cfg.anysearch.enabled and self._cfg.anysearch.api_key:
            self._anysearch_client = AnySearchClient(
                api_key=self._cfg.anysearch.api_key,
                base_url=self._cfg.anysearch.base_url,
                timeout=self._cfg.anysearch.timeout,
            )
        else:
            self._tools_schemas = [t for t in self._tools_schemas
                                   if t["function"]["name"] != "anysearch_search"]

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
        if has_images and self._vision_available:
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

        # Record token usage
        usage = getattr(response, "usage", None)
        if usage is not None:
            await self._record_usage(model, usage, bot_id, event)

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
        if has_images and self._vision_available:
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
                stream_options={"include_usage": True},
            )
        except Exception as e:
            logger.error(f"LLM stream call failed: {e}")
            yield (f"[LLM 调用失败: {e}]", True)
            return

        full_content = ""
        tool_calls_buffer: dict[int, dict] = {}
        got_any_data = False
        stream_usage = None  # Usage arrives in the final stream chunk

        async for chunk in stream:
            # Capture usage from the final chunk (choices may be empty)
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                stream_usage = usage
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

        # Record token usage from the final stream chunk
        if stream_usage is not None:
            await self._record_usage(model, stream_usage, bot_id, event)

        yield ("", True)  # Signal completion with no extra text

    # ── Token usage tracking (web panel stats) ─────────────────

    async def _record_usage(self, model: str, usage: Any, bot_id: str, event: MessageEvent) -> None:
        """Append one usage record to data/stats/llm_usage.jsonl."""
        try:
            from mohobot.file_store import JSONLWriter

            usage_dir = Path(self._cfg.data_dir) / "stats"
            usage_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "time": time.time(),
                "bot_id": bot_id,
                "model": model,
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
            }
            # JSONLWriter 带 per-file 锁: 6 bot 并发回复结束时同时写 usage 不交错
            writer = JSONLWriter(usage_dir / "llm_usage.jsonl")
            await writer.append(record)
        except Exception as e:
            logger.debug(f"Failed to record LLM usage: {e}")

    async def summarize_context(self, entries: list[dict]) -> str | None:
        """总结一段较早的对话(上下文压缩用, 复用全局 chat_model)。

        Prompt 要求 LLM 自行抉择: 先全局概要, 再对最重要的轮次(≤5)逐轮浓缩。
        失败返回 None(调用方降级为直接裁剪)。
        """
        if not self._available or self._chat_client is None:
            logger.warning("LLM 未配置, 上下文总结不可用(直接裁剪)")
            return None
        lines = []
        for e in entries:
            role = e.get("role", "user")
            content = str(e.get("content", "")).strip()
            if not content:
                continue
            if role == "assistant":
                lines.append(f"机器人: {content}")
            elif role == "summary":
                lines.append(f"[早期总结]: {content}")
            else:
                lines.append(f"用户({role}): {content}")
        if not lines:
            return None
        prompt = (
            "你是一个对话压缩助手。下面是某段较早的对话(用户消息与机器人回复)。\n"
            "请将其压缩为一份总结:\n"
            "1. 先给出全局概要(2-4 句, 概括主题、重要事实、人物关系、未完成事项)\n"
            "2. 针对最重要的轮次(不超过 5 个)逐轮浓缩, 保留关键信息\n"
            "3. 总长度不超过 800 字, 使用简洁中文, 不要使用 markdown 标题\n\n"
            "对话内容:\n" + "\n".join(lines)
        )
        try:
            resp = await self._chat_client.chat.completions.create(
                model=self._cfg.llm.chat_model,
                messages=[
                    {"role": "system", "content": "你是对话压缩助手。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1200,
            )
            text = (resp.choices[0].message.content or "").strip()
            return text or None
        except Exception as e:
            logger.warning(f"上下文总结失败: {e}")
            return None

    async def get_usage_stats(self) -> dict[str, Any]:
        """Aggregate token usage from data/stats/llm_usage.jsonl.

        Returns totals + per-model breakdown + today's usage.
        """
        import aiofiles
        usage_file = Path(self._cfg.data_dir) / "stats" / "llm_usage.jsonl"
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        per_model: dict[str, dict] = {}
        today = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}

        import datetime
        from mohobot.utils.time_utils import TZ_UTC8
        today_start = (
            datetime.datetime.now(TZ_UTC8)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

        if not usage_file.exists():
            return {"totals": totals, "per_model": per_model, "today": today}

        async with aiofiles.open(usage_file, "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pt = rec.get("prompt_tokens", 0)
                ct = rec.get("completion_tokens", 0)
                tt = rec.get("total_tokens", 0)
                totals["prompt_tokens"] += pt
                totals["completion_tokens"] += ct
                totals["total_tokens"] += tt
                totals["calls"] += 1
                model = rec.get("model", "unknown")
                pm = per_model.setdefault(model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
                pm["calls"] += 1
                pm["prompt_tokens"] += pt
                pm["completion_tokens"] += ct
                pm["total_tokens"] += tt
                if rec.get("time", 0) >= today_start:
                    today["prompt_tokens"] += pt
                    today["completion_tokens"] += ct
                    today["total_tokens"] += tt
                    today["calls"] += 1

        return {"totals": totals, "per_model": per_model, "today": today}

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
            if role == "summary":
                # 上下文压缩产生的总结块: 作为 system 消息注入(早期对话浓缩)
                messages.append({
                    "role": "system",
                    "content": f"【较早对话总结】\n{content}",
                })
            elif role == "system":
                # 临时注入段(如群聊最近消息): 直接作为 system 消息
                messages.append({"role": "system", "content": content})
            elif role in ("user", "assistant"):
                messages.append({"role": role, "content": content})
            else:
                # Named speaker role, e.g. "3831097597-墨染荷韵"
                messages.append({
                    "role": "user",
                    "content": f"[{role}]: {content}",
                })

        # 3. Current time (UTC+8 北京时间, 不依赖系统时区)
        from mohobot.utils.time_utils import format_utc8
        now = format_utc8("%Y-%m-%d %H:%M:%S %A")
        time_msg = f"当前时间: {now}"

        # 4. Build user input message
        user_text = extract_plain_text(event.message)

        # Handle image messages — only process the FIRST image to prevent flooding
        image_urls = extract_image_urls(event.message)
        if image_urls and len(image_urls) > 1:
            logger.debug(f"Limiting {len(image_urls)} images to first 1 for LLM input")
        image_urls = image_urls[:1]  # Never send more than 1 image per message

        user_content: str | list = user_text or ""

        if image_urls and self._vision_available:
            # Multi-modal message: text + first image only (仅视觉模型可处理)
            content_parts: list[dict] = []
            if user_text:
                content_parts.append({"type": "text", "text": user_text})
            for url in image_urls:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": url},
                })
            user_content = content_parts
        elif image_urls:
            # 无视觉能力: 不给纯文本模型塞 image_url 内容(会报错),降级为占位文本
            placeholder = f"{user_text}（用户发送了图片）" if user_text else "（用户发送了图片）"
            logger.debug("Vision unavailable — image content degraded to text placeholder")
            user_content = placeholder

        # 5. Final user message — the @mention check is now done in message_handler.py
        if isinstance(user_content, list):
            # Prepend time info as text
            user_content.insert(0, {"type": "text", "text": time_msg})
        else:
            user_content = f"{time_msg}\n\n{user_content}" if user_content else time_msg

        messages.append({"role": "user", "content": user_content})

        return messages

    async def describe_image(self, url: str, max_tokens: int = 256) -> str:
        """用视觉模型描述一张图片,供 agent 流水线使用。

        返回简短描述;视觉不可用或调用失败时返回空串(调用方降级为占位符)。
        """
        if not self._vision_available or self._vision_client is None:
            return ""
        try:
            response = await self._vision_client.chat.completions.create(
                model=self._cfg.llm.vision_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请用一句简短、客观的话描述这张图片的内容。"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }],
                max_tokens=max_tokens,
                temperature=0.3,
            )
            text = (response.choices[0].message.content or "").strip()
            if not text:
                logger.debug("Vision describe returned empty")
            return text
        except Exception as e:
            logger.warning(f"Vision describe failed: {e}")
            return ""

    async def describe_image_file(self, local_path: str, max_tokens: int = 256) -> str:
        """用视觉模型描述本地图片文件。

        图片以 base64 data URI 内嵌请求体发送, 不依赖网关访问外网
        (QQ 图源 gchat.qpic.cn 需鉴权, 直接传 URL 常导致模型返回空)。
        """
        if not self._vision_available or self._vision_client is None:
            return ""
        try:
            import base64 as _b64
            ext = Path(local_path).suffix.lower()
            mime = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            }.get(ext, "image/jpeg")
            with open(local_path, "rb") as f:
                data = _b64.b64encode(f.read()).decode()
            return await self.describe_image(f"data:{mime};base64,{data}", max_tokens)
        except Exception as e:
            logger.warning(f"Vision describe file failed: {e}")
            return ""

    async def _execute_tool(self, func_name: str, args_json: str) -> str:
        """Execute a tool/function call and return the result."""
        try:
            args = json.loads(args_json) if args_json else {}
        except json.JSONDecodeError:
            args = {}

        if func_name == "get_current_time":
            from mohobot.utils.time_utils import format_utc8
            return format_utc8("%Y-%m-%d %H:%M:%S")
        elif func_name == "get_group_member_info":
            # This would need a bot connection to call the API
            return json.dumps({"error": "不在 WebSocket 连接中无法获取成员信息"}, ensure_ascii=False)
        elif func_name == "anysearch_search":
            if self._anysearch_client is None:
                return json.dumps({"error": "Anysearch 未配置 API Key"}, ensure_ascii=False)
            query = str(args.get("query", "")).strip()
            if not query:
                return json.dumps({"error": "搜索查询不能为空"}, ensure_ascii=False)
            try:
                return await self._anysearch_client.safe_search(query, max_results=5)
            except Exception as e:
                return json.dumps({"error": f"搜索失败: {e}"}, ensure_ascii=False)
        else:
            return json.dumps({"error": f"未知工具: {func_name}"}, ensure_ascii=False)

    async def close(self) -> None:
        """Close the HTTP clients."""
        if self._chat_client:
            await self._chat_client.close()
        if self._vision_client and self._vision_client is not self._chat_client:
            await self._vision_client.close()