"""LLM service — OpenAI-compatible chat and vision model interaction.

Handles prompt assembly, tool calling, vision integration, and response generation.
"""

from __future__ import annotations

import asyncio
import os
import time
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, AsyncGenerator

import aiofiles
from loguru import logger
from openai import AsyncOpenAI

from mohobot.models.config import GlobalConfig, BotConfig, LLMConfig
from mohobot.models.onebot import (
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from mohobot.utils.cq_code import extract_plain_text, extract_image_urls
from mohobot.services.usage import UsageRecorder


class LLMService:
    """LLM interaction service with prompt assembly and vision support."""

    # Reserved even when disabled/unadvertised: plugins cannot replace built-ins.
    _BUILTIN_TOOL_NAMES = frozenset({
        "get_current_time", "get_group_member_info", "anysearch_search",
    })

    def __init__(self, global_config: GlobalConfig, image_cache=None, usage_recorder: UsageRecorder | None = None,
                 song_annotator=None, persona_service=None):
        self._cfg = global_config
        self._persona_service = persona_service
        self._usage_recorder = usage_recorder or UsageRecorder(self._cfg.data_dir)
        self._owns_usage_recorder = usage_recorder is None
        # 用量记录缓存: (到期时间, 记录列表)。文件有几万行, 全量解析较慢,
        # dashboard 与用量页 60s 内复用同一份解析结果。
        self._usage_records_cache: tuple[float, list[dict]] | None = None
        # 图片缓存(下载 + phash 去重 + 描述缓存)。可选注入, 未传时降级为每次直调 vision。
        self._image_cache = image_cache
        # 歌曲信息注解器(全局): 回调 (event) -> 注解文本 或 None。
        # 在发送给 LLM 前把歌曲信息注入用户消息下方(不写入 context)。
        self._song_annotator = song_annotator
        self._retired_clients: list[AsyncOpenAI] = []
        identities, clients = self._prepare_clients(self._cfg.llm, [])
        self._install_clients(self._cfg.llm, identities, clients)
        if not self._available:
            logger.warning("LLM chat API key not configured — LLM calls will fail")

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

    @staticmethod
    def _provider_identities(llm: LLMConfig) -> dict[str, tuple[str, str] | None]:
        """Resolve keys/URLs, including env and chat fallbacks, for each role."""
        chat_key = llm.chat_api_key or os.environ.get("MOHOBOT_LLM_API_KEY", "")
        vision_key = llm.vision_api_key or os.environ.get("MOHOBOT_VISION_API_KEY", "") or chat_key
        emotion_key = llm.emotion_api_key or os.environ.get("MOHOBOT_EMOTION_API_KEY", "") or chat_key
        return {
            "chat": (chat_key, llm.chat_base_url) if chat_key else None,
            "vision": (vision_key, llm.vision_base_url or llm.chat_base_url) if vision_key else None,
            "emotion": (emotion_key, llm.emotion_base_url or llm.chat_base_url) if emotion_key else None,
        }

    def _prepare_clients(
        self, llm: LLMConfig, created: list[AsyncOpenAI],
    ) -> tuple[dict[str, tuple[str, str] | None], dict[str, AsyncOpenAI | None]]:
        """Build all new clients before publishing; reuse only matching key/URL pairs."""
        identities = self._provider_identities(llm)
        reusable = {}
        for role, identity in getattr(self, "_client_identities", {}).items():
            client = getattr(self, f"_{role}_client", None)
            if identity is not None and client is not None:
                reusable[identity] = client
        clients = {}
        for role, identity in identities.items():
            if identity is None:
                clients[role] = None
                continue
            if identity not in reusable:
                client = AsyncOpenAI(api_key=identity[0], base_url=identity[1])
                created.append(client)
                reusable[identity] = client
            clients[role] = reusable[identity]
        return identities, clients

    def _install_clients(self, llm: LLMConfig, identities: dict, clients: dict) -> None:
        self._client_identities = identities
        self._chat_client = clients["chat"]
        self._vision_client = clients["vision"]
        self._emotion_client = clients["emotion"]
        self._available = self._chat_client is not None
        self._vision_available = bool(llm.vision_model and self._vision_client is not None)

    async def sync_config(self, config: GlobalConfig) -> None:
        """Hot-update only shared config.llm; retire replaced clients until close().

        Model/temperature changes reuse providers. Client construction failure leaves
        the current LLM config and clients untouched, and closes unpublished clients.
        """
        llm = deepcopy(config.llm)
        created: list[AsyncOpenAI] = []
        try:
            identities, clients = self._prepare_clients(llm, created)
        except Exception:
            await self._close_clients(created)
            raise

        # No await in this publication block: config and client references move together.
        retired = getattr(self, "_retired_clients", [])
        retained_ids = {id(client) for client in clients.values() if client is not None}
        retired_ids = {id(client) for client in retired}
        for role in ("chat", "vision", "emotion"):
            old = getattr(self, f"_{role}_client", None)
            if old is not None and id(old) not in retained_ids and id(old) not in retired_ids:
                retired.append(old)
                retired_ids.add(id(old))
        self._cfg.llm = llm
        self._retired_clients = retired
        self._install_clients(llm, identities, clients)

    def _current_tools_schemas(self) -> list[dict]:
        """Advertise executable tools; reserved built-in names always win conflicts."""
        tools = [
            tool for tool in self._tools_schemas
            if tool["function"]["name"] != "get_group_member_info"
            and (tool["function"]["name"] != "anysearch_search"
                 or getattr(self, "_anysearch_client", None) is not None)
        ]
        try:
            from mohobot.services.llm_tools import registry
            names = self._BUILTIN_TOOL_NAMES | {tool["function"]["name"] for tool in tools}
            tools.extend(tool for tool in registry.schemas() if tool["function"]["name"] not in names)
        except Exception as exc:
            logger.warning(f"LLM plugin tool schemas unavailable: {exc}")
        return tools

    async def chat(
        self,
        bot_id: str,
        event: MessageEvent,
        context: list[dict[str, Any]],
        raw_event: dict[str, Any],
        bot_config: BotConfig | None = None,
        persona_content: str | None = None,
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

        # 图片不再切视觉模型: 描述已由 _build_messages 内预调用视觉模型转成文本,
        # 主模型(纯文本 chat_model)统一处理、不接收图片原始信息。
        if bot_config and bot_config.chat_model_override:
            model = bot_config.chat_model_override

        # Build messages array
        messages = await self._build_messages(bot_id, event, context, bot_config, persona_content)

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
                tools=self._current_tools_schemas(),
                tool_choice="auto",
            )
        except Exception as e:
            logger.error(f"LLM API call failed: {e}")
            return f"[LLM 调用失败: {e}]", None

        await self._record_usage(
            model, getattr(response, "usage", None), bot_id, event, module="chat"
        )
        choice = response.choices[0] if response.choices else None
        if not choice:
            return None, None

        reply_text = choice.message.content or ""
        tool_calls = choice.message.tool_calls

        # Handle tool calls: 工具结果作为 tool 消息回传 LLM,
        # 再调用一次生成最终自然语言回复(不向用户输出原始搜索结果)
        tool_results = None
        if tool_calls:
            tool_results = []
            messages.append({
                "role": "assistant",
                "content": choice.message.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                result = await self._execute_tool(tc.function.name, tc.function.arguments)
                tool_results.append({
                    "tool_call_id": tc.id,
                    "function_name": tc.function.name,
                    "arguments": tc.function.arguments,
                    "result": result,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            # 二次调用: 基于工具结果生成最终回复
            try:
                response2 = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=self._current_tools_schemas(),
                    tool_choice="auto",
                )
                choice2 = response2.choices[0] if response2.choices else None
                await self._record_usage(
                    model, getattr(response2, "usage", None), bot_id, event,
                    module="chat", kind="tool_follow_up",
                )
                reply_text = (choice2.message.content or "") if choice2 else ""
            except Exception as e:
                logger.error(f"LLM API call failed (after tools): {e}")
                reply_text = f"[LLM 调用失败: {e}]"

        return reply_text, tool_results

    async def chat_stream(
        self,
        bot_id: str,
        event: MessageEvent,
        context: list[dict[str, Any]],
        raw_event: dict[str, Any],
        bot_config: BotConfig | None = None,
        persona_content: str | None = None,
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

        # 图片不再切视觉模型: 描述已由 _build_messages 内预调用视觉模型转成文本,
        # 主模型(纯文本 chat_model)统一处理、不接收图片原始信息。

        if bot_config and bot_config.chat_model_override:
            model = bot_config.chat_model_override

        messages = await self._build_messages(bot_id, event, context, bot_config, persona_content)

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
                tools=self._current_tools_schemas(),
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

        if stream_usage is not None:
            await self._record_usage(
                model, stream_usage, bot_id, event,
                module="chat", kind="stream",
            )

        # After stream ends, execute tool calls if any:
        # 工具结果作为 tool 消息回传 LLM 后进入多轮 follow-up:
        # 模型可能在看工具结果后继续调用工具(如搜索为空时换关键词再搜),
        # 因此循环处理 tool_calls 直到模型给出文本或达到轮次上限。
        max_tool_rounds = 4
        tool_round = 0
        while tool_calls_buffer:
            # 达到轮次上限后不再提供工具, 强制模型基于已有工具结果给出文本回答。
            final_round = tool_round >= max_tool_rounds
            if final_round:
                logger.warning(
                    f"LLM tool rounds hit {max_tool_rounds}, forcing text answer without tools"
                )
            tool_round += 1
            messages.append({
                "role": "assistant",
                "content": full_content or None,
                "tool_calls": [
                    {"id": tc_data.get("id") or f"call_{idx}", "type": "function",
                     "function": {"name": tc_data.get("function_name", ""),
                                  "arguments": tc_data.get("arguments", "{}")}}
                    for idx, tc_data in sorted(tool_calls_buffer.items())
                ],
            })
            for idx, tc_data in sorted(tool_calls_buffer.items()):
                args_str = tc_data.get("arguments", "{}")
                result = await self._execute_tool(tc_data["function_name"], args_str)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_data.get("id") or f"call_{idx}",
                    "content": result,
                })
            full_content = ""
            try:
                follow_params: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if not final_round:
                    follow_params["tools"] = self._current_tools_schemas()
                    follow_params["tool_choice"] = "auto"
                stream2 = await client.chat.completions.create(**follow_params)
                got_final = False
                stream2_usage = None
                tool_calls_buffer = {}
                async for chunk in stream2:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        stream2_usage = usage
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue
                    if delta.content:
                        got_final = True
                        full_content += delta.content
                        yield (delta.content, False)
                    if delta.tool_calls and not final_round:
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
                if stream2_usage is not None:
                    await self._record_usage(
                        model, stream2_usage, bot_id, event,
                        module="chat", kind="tool_follow_up",
                    )
                if got_final and not tool_calls_buffer:
                    yield ("", True)
                    return
                if tool_calls_buffer:
                    # 模型要求继续调用工具 → 进入下一轮
                    continue
                # 流为空(无文本无工具调用) → 非流式重试一次
                logger.warning("LLM tool-follow stream returned NO content; retrying non-stream")
                try:
                    retry_params: dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "stream": False,
                    }
                    if not final_round:
                        retry_params["tools"] = self._current_tools_schemas()
                        retry_params["tool_choice"] = "auto"
                    response3 = await client.chat.completions.create(**retry_params)
                    choice3 = response3.choices[0] if response3.choices else None
                    await self._record_usage(
                        model, getattr(response3, "usage", None), bot_id, event,
                        module="chat", kind="tool_follow_up_retry",
                    )
                    retry_tool_calls = getattr(choice3.message, "tool_calls", None) if choice3 else None
                    if retry_tool_calls and not final_round:
                        # 模型在非流式重试中仍要求调用工具 → 交给下一轮循环
                        tool_calls_buffer = {
                            idx: {"id": tc.id or "", "function_name": tc.function.name,
                                  "arguments": tc.function.arguments or ""}
                            for idx, tc in enumerate(retry_tool_calls)
                        }
                        continue
                    fallback_text = (choice3.message.content or "") if choice3 else ""
                    if fallback_text:
                        yield (fallback_text, False)
                        yield ("", True)
                        return
                except Exception as e:
                    logger.warning(f"LLM tool-follow non-stream retry failed: {e}")
                yield ("[工具调用完成，但模型未返回文本]", True)
                return
            except Exception as e:
                logger.error(f"LLM stream call failed (after tools): {e}")
                yield (f"[LLM 调用失败: {e}]", True)
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

    # ── Token usage tracking (web panel stats) ─────────────────

    async def _record_usage(
        self, model: str, usage: Any, bot_id: str, event: MessageEvent,
        module: str = "chat",
        kind: str = "chat",
    ) -> None:
        """Record one provider request through the shared async recorder.

        会话维度(chat_type/chat_id)从消息事件提取, 供按会话统计用量;
        无事件上下文的调用(总结/情感/识图)记为未知会话。
        """
        chat_type = ""
        chat_id = ""
        user_id = ""
        if event is not None:
            user_id = str(getattr(event, "user_id", "") or "")
            mt = str(getattr(event, "message_type", "") or "")
            if mt == "group":
                chat_type = "group"
                chat_id = str(getattr(event, "group_id", "") or "")
            elif mt == "private":
                chat_type = "private"
                chat_id = user_id
        await self._usage_recorder.record(
            usage,
            model=model,
            bot_id=bot_id,
            module=module,
            kind=kind,
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=user_id,
        )

    async def _load_usage_records(self, ttl: float = 60.0) -> list[dict]:
        """全量读取 llm_usage.jsonl 并解析, 带 TTL 缓存(默认 60s)。"""
        import aiofiles
        now = time.monotonic()
        if self._usage_records_cache is not None and now < self._usage_records_cache[0]:
            return self._usage_records_cache[1]
        usage_file = Path(self._cfg.data_dir) / "stats" / "llm_usage.jsonl"
        records: list[dict] = []
        if usage_file.exists():
            async with aiofiles.open(usage_file, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        self._usage_records_cache = (now + ttl, self._filter_usage_records(records))
        return self._usage_records_cache[1]

    def _filter_usage_records(self, records: list[dict]) -> list[dict]:
        """按配置排除模型(usage_excluded_models): 其调用不计入用量统计, 记录仍落盘。"""
        excluded = {
            str(m).strip() for m in (getattr(self._cfg, "usage_excluded_models", None) or [])
            if str(m).strip()
        }
        if not excluded:
            return records
        return [r for r in records if str(r.get("model", "")) not in excluded]

    @staticmethod
    def _range_since(range_key: str) -> float:
        """range_key → 起始时间戳。

        "Nh"(近 N 小时)为滚动窗口(从现在往回数); 天数窗口按 UTC+8
        当日 00:00 起往前数 N 天(含今日), 供 /用量 命令沿用。
        """
        import datetime
        import re
        from mohobot.utils.time_utils import TZ_UTC8

        now = datetime.datetime.now(TZ_UTC8)
        if re.fullmatch(r"\d{1,4}h", range_key):
            hours = max(1, min(8760, int(range_key[:-1])))
            return (now - datetime.timedelta(hours=hours)).timestamp()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if range_key == "7d":
            days = 7
        elif range_key == "30d":
            days = 30
        elif re.fullmatch(r"\d{1,4}d", range_key):
            days = max(1, min(3650, int(range_key[:-1])))
        else:
            days = 1
        return (day_start - datetime.timedelta(days=days - 1)).timestamp()

    async def get_session_usage_stats(self, range_key: str = "today") -> dict[str, Any]:
        """按聊天会话聚合 token 用量, 供 WebUI 与 /用量 会话 命令共用。

        range_key: today(今日) / 7d(近7天) / 30d(近30天) / 自定义 "Nd"(近N天, 含今日)。
        旧记录无会话字段 → 归入 chat_id 为空字符串的未知会话。
        """
        since = self._range_since(range_key)

        sessions: dict[tuple[str, str, str], dict] = {}
        for rec in await self._load_usage_records():
            if rec.get("time", 0) < since:
                continue
            pt = int(rec.get("prompt_tokens", 0) or 0)
            ct = int(rec.get("completion_tokens", 0) or 0)
            tt = int(rec.get("total_tokens", 0) or 0) or (pt + ct)
            key = (
                str(rec.get("bot_id", "") or "?"),
                str(rec.get("chat_type", "") or ""),
                str(rec.get("chat_id", "") or ""),
            )
            s = sessions.setdefault(key, {
                "calls": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0,
                "cached_tokens": 0,
                "modules": {},
            })
            s["calls"] += 1
            s["prompt_tokens"] += pt
            s["completion_tokens"] += ct
            s["total_tokens"] += tt
            s["cached_tokens"] += int(rec.get("cached_tokens", 0) or 0)
            mod = str(rec.get("module", "") or "其他")
            m = s["modules"].setdefault(mod, {"calls": 0, "total_tokens": 0})
            m["calls"] += 1
            m["total_tokens"] += tt

        items = [
            {"bot_id": bid, "chat_type": ctype, "chat_id": cid, **stats}
            for (bid, ctype, cid), stats in sessions.items()
        ]
        items.sort(key=lambda x: -x["total_tokens"])
        totals = {
            "calls": sum(s["calls"] for s in sessions.values()),
            "prompt_tokens": sum(s["prompt_tokens"] for s in sessions.values()),
            "completion_tokens": sum(s["completion_tokens"] for s in sessions.values()),
            "total_tokens": sum(s["total_tokens"] for s in sessions.values()),
            "cached_tokens": sum(s["cached_tokens"] for s in sessions.values()),
        }
        return {"range": range_key, "totals": totals, "sessions": items}

    async def get_user_usage_stats(self, range_key: str = "today") -> dict[str, Any]:
        """按发起对话的用户聚合 token 用量(群/私聊合并, 跨 bot 合并)。

        无用户身份的记录(总结/情感/识图等系统内部调用, 以及旧记录)归"未知用户"。
        返回 users: [{user_id, calls, tokens..., bots: {bot_id: {calls, total_tokens, modules}}}]。
        """
        since = self._range_since(range_key)
        users: dict[str, dict] = {}
        for rec in await self._load_usage_records():
            if rec.get("time", 0) < since:
                continue
            pt = int(rec.get("prompt_tokens", 0) or 0)
            ct = int(rec.get("completion_tokens", 0) or 0)
            tt = int(rec.get("total_tokens", 0) or 0) or (pt + ct)
            uid = str(rec.get("user_id", "") or "")
            bot = str(rec.get("bot_id", "") or "?")
            mod = str(rec.get("module", "") or "其他")

            u = users.setdefault(uid, {
                "user_id": uid, "calls": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "cached_tokens": 0, "bots": {},
            })
            u["calls"] += 1
            u["prompt_tokens"] += pt
            u["completion_tokens"] += ct
            u["total_tokens"] += tt
            u["cached_tokens"] += int(rec.get("cached_tokens", 0) or 0)

            b = u["bots"].setdefault(bot, {"calls": 0, "total_tokens": 0, "modules": {}})
            b["calls"] += 1
            b["total_tokens"] += tt
            m = b["modules"].setdefault(mod, {"calls": 0, "total_tokens": 0})
            m["calls"] += 1
            m["total_tokens"] += tt

        items = list(users.values())
        # 未知用户排最后, 其余按 token 降序
        items.sort(key=lambda x: (x["user_id"] == "", -x["total_tokens"]))
        totals = {
            "calls": sum(u["calls"] for u in items),
            "prompt_tokens": sum(u["prompt_tokens"] for u in items),
            "completion_tokens": sum(u["completion_tokens"] for u in items),
            "total_tokens": sum(u["total_tokens"] for u in items),
            "cached_tokens": sum(u["cached_tokens"] for u in items),
        }
        return {"range": range_key, "totals": totals, "users": items}

    async def get_module_usage_stats(self, range_key: str = "today") -> dict[str, Any]:
        """按用途(module)聚合: 每个 bot 在哪些地方调用多少 token/次数。

        返回 bots: [{bot_id, calls, total_tokens, modules: {module: {calls, total_tokens}}}],
        bot_id 为空的系统内部调用归"系统"行。
        """
        since = self._range_since(range_key)
        bots: dict[str, dict] = {}
        for rec in await self._load_usage_records():
            if rec.get("time", 0) < since:
                continue
            pt = int(rec.get("prompt_tokens", 0) or 0)
            ct = int(rec.get("completion_tokens", 0) or 0)
            tt = int(rec.get("total_tokens", 0) or 0) or (pt + ct)
            bot = str(rec.get("bot_id", "") or "")
            bot = bot or "系统"
            mod = str(rec.get("module", "") or "其他")

            b = bots.setdefault(bot, {"bot_id": bot, "calls": 0, "total_tokens": 0, "modules": {}})
            b["calls"] += 1
            b["total_tokens"] += tt
            m = b["modules"].setdefault(mod, {"calls": 0, "total_tokens": 0})
            m["calls"] += 1
            m["total_tokens"] += tt

        items = list(bots.values())
        # 系统行排最后, 其余按 bot_id 排序
        items.sort(key=lambda x: (x["bot_id"] == "系统", x["bot_id"]))
        totals = {
            "calls": sum(b["calls"] for b in items),
            "total_tokens": sum(b["total_tokens"] for b in items),
        }
        return {"range": range_key, "totals": totals, "bots": items}

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
                temperature=float(getattr(self._cfg.llm, "summarize_temperature", 0.3)),
                max_tokens=int(getattr(self._cfg.llm, "summarize_max_tokens", 4096)),
            )
            await self._record_usage(
                self._cfg.llm.chat_model, getattr(resp, "usage", None),
                "", None, module="summary", kind="summary",
            )
            text = (resp.choices[0].message.content or "").strip()
            return text or None
        except Exception as e:
            logger.warning(f"上下文总结失败: {e}")
            return None

    async def complete_text(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        max_tokens: int = 512,
        temperature: float = 0.7,
        module: str = "plugin",
    ) -> str:
        """通用单轮补全(供插件调用): 无上下文/无事件/无工具。

        复用 chat 模型; 失败返回空串(调用方自行降级), 不抛异常。
        """
        if not self._available or self._chat_client is None:
            logger.warning("LLM 未配置, complete_text 不可用")
            return ""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = await self._chat_client.chat.completions.create(
                model=self._cfg.llm.chat_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            await self._record_usage(
                self._cfg.llm.chat_model, getattr(resp, "usage", None),
                "", None, module=module, kind="complete",
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"complete_text 调用失败(module={module}): {e}")
            return ""

    async def analyze_emotion(self, prompt: str, model: str | None = None) -> str | None:
        """情感专家分析(二次 LLM; 独立 emotion 模型可配, 缺省回退 chat 模型)。

        model: 按次指定模型(队列积压时的快速模型 burst); 留空用配置的 emotion_model。
        失败/未配置返回 None, 调用方(EmotionExpert)自行降级。
        """
        if self._emotion_client is None:
            return None
        model = model or self._cfg.llm.emotion_model or self._cfg.llm.chat_model
        try:
            resp = await self._emotion_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=float(getattr(self._cfg.llm, "emotion_temperature", 0.3)),
                max_tokens=int(getattr(self._cfg.llm, "emotion_max_tokens", 512)),
            )
            await self._record_usage(
                model, getattr(resp, "usage", None),
                "", None, module="emotion", kind="emotion",
            )
            return (resp.choices[0].message.content or "").strip() or None
        except Exception as e:
            logger.warning(f"情感分析 LLM 调用失败: {e}")
            return None

    async def get_usage_stats(self) -> dict[str, Any]:
        """Aggregate token usage from data/stats/llm_usage.jsonl.

        Returns totals + per-model breakdown + today's usage.
        """
        import datetime
        from mohobot.utils.time_utils import TZ_UTC8
        today_start = (
            datetime.datetime.now(TZ_UTC8)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        per_model: dict[str, dict] = {}
        today = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}

        for rec in await self._load_usage_records():
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

    def resolve_bot_persona(self, bot_config: BotConfig | None) -> dict[str, Any]:
        service = getattr(self, "_persona_service", None)
        if service is not None:
            return service.resolve_bot(bot_config)
        return {
            "id": "", "name": "默认人设", "source": "default",
            "content": (bot_config.persona if bot_config and bot_config.persona
                        else "你是 Mohobot，一个有用的 AI 助手。"),
        }

    async def _build_messages(
        self,
        bot_id: str,
        event: MessageEvent,
        context: list[dict[str, Any]],
        bot_config: BotConfig | None = None,
        persona_content: str | None = None,
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
        perception_parts: list[str] = []

        # 1. System prompt
        persona = (persona_content if persona_content is not None
                   else self.resolve_bot_persona(bot_config)["content"])
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

        # TTS 语音标注提示(仅开启 TTS 的 bot): 引导 LLM 用 <tts></tts> 标注朗读句
        if (
            getattr(self._cfg, "tts", None) is not None
            and self._cfg.tts.enabled
            and bot_config is not None
            and bot_config.tts_enabled
        ):
            system_content += self._cfg.tts.tts_prompt_template

        messages.append({"role": "system", "content": system_content})

        # 2. Session context — insert as alternating user/assistant messages.
        #    Context roles are either "user"/"assistant" or "{qq}-{nickname}"
        #    (e.g. "3831097597-墨染荷韵") — named roles are prefixed so the
        #    model knows exactly who said what.
        for entry in context:
            role = entry.get("role", "user")
            content = entry.get("content", "")
            if role == "perception":
                # 环境感知: 并入主系统提示(不作为对话中的独立消息,
                # 防止模型把它当成聊天内容向外议论)
                perception_parts.append(content)
            elif role == "summary":
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

        if perception_parts:
            messages[0]["content"] += "\n\n【环境感知】\n" + "\n".join(perception_parts)

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

        user_content = user_text or ""

        if image_urls:
            # 与 beta(Agent)路径一致的图片语义: 先预调用视觉模型取描述,
            # 主模型只接收「图文文本 + 描述」, 不接收图片原始信息(image_url)。
            vision_desc = await self._describe_image_for_text(image_urls[0])
            if user_text and vision_desc:
                user_content = f"{user_text}（图片内容：{vision_desc}）"
            elif vision_desc:
                user_content = f"[图片]（{vision_desc}）"
            else:
                # 视觉不可用或描述失败: 降级为占位文本
                user_content = f"{user_text}（用户发送了图片）" if user_text else "（用户发送了图片）"

        # 5. Final user message — the @mention check is now done in message_handler.py
        #    (主模型始终为纯文本, 不再构造多模态 image_url 分片)
        user_content = f"{time_msg}\n\n{user_content}" if user_content else time_msg

        # 6. 歌曲信息注入(全局, 私聊+群聊): 消息含歌曲信息时, 在用户消息下方
        #    追加【歌曲信息】段(介绍 + 词/曲/混/调等 + 完整歌词)。
        #    仅本次请求携带, 不写入 context 文件。
        if self._song_annotator is not None:
            try:
                annotation = await self._song_annotator(event)
                if annotation:
                    user_content = f"{user_content}\n\n{annotation}"
            except Exception as e:
                logger.debug(f"Song annotation failed: {e}")

        messages.append({"role": "user", "content": user_content})

        return messages

    async def _describe_image_for_text(self, url: str) -> str:
        """Legacy 路径用: 预调用视觉模型把图片转述为文本描述。

        优先走 ImageCache(下载 → phash 去重 → 描述缓存, 命中缓存不再调 vision);
        未注入 image_cache 时降级直调 describe_image(每次调用)。
        视觉不可用或调用失败返回空串(调用方降级为占位文本)。
        """
        if not self._vision_available or self._vision_client is None:
            return ""
        if self._image_cache is not None:
            try:
                _, description = await self._image_cache.get_or_describe(
                    url, vision_callback=self._vision_callback(),
                )
                return description or ""
            except Exception as e:
                logger.warning(f"ImageCache failed in _build_messages: {e}")
                return ""
        # 无缓存注入: 直调 describe_image(不支持下载的 URL 可能返回空)
        return await self.describe_image(url)

    def _vision_callback(self):
        """视觉描述回调(供 ImageCache 使用): 本地文件 base64 内嵌, 30s 超时。"""
        async def _cb(image_url: str, local_path: str) -> str:
            try:
                return await asyncio.wait_for(
                    self.describe_image_file(local_path),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Vision describe timeout in _build_messages")
                return ""
            except Exception as e:
                logger.warning(f"Vision describe failed in _build_messages: {e}")
                return ""
        return _cb

    async def describe_image(self, url: str, max_tokens: int | None = None) -> str:
        """用视觉模型描述一张图片,供 agent 流水线使用。

        提示词取全局配置 llm.vision_prompt(默认含中V人物特征参照);
        视觉不可用或调用失败时返回空串(调用方降级为占位符)。
        """
        if not self._vision_available or self._vision_client is None:
            return ""
        # 参数可在 WebUI 配置(llm.vision_max_tokens / vision_temperature);
        # 推理型视觉模型思考会烧 token, 预算太小会导致正文为空。
        if max_tokens is None:
            max_tokens = int(getattr(self._cfg.llm, "vision_max_tokens", 2048))
        vision_temperature = float(getattr(self._cfg.llm, "vision_temperature", 0.3))
        try:
            prompt = (self._cfg.llm.vision_prompt or "").strip() or "请用一句简短、客观的话描述这张图片的内容。"
            response = await self._vision_client.chat.completions.create(
                model=self._cfg.llm.vision_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }],
                max_tokens=max_tokens,
                temperature=vision_temperature,
            )
            await self._record_usage(
                self._cfg.llm.vision_model, getattr(response, "usage", None),
                "", None, module="vision", kind="vision",
            )
            text = (response.choices[0].message.content or "").strip()
            if not text:
                logger.debug("Vision describe returned empty")
            return text
        except Exception as e:
            logger.warning(f"Vision describe failed: {e}")
            return ""

    async def describe_image_file(self, local_path: str, max_tokens: int | None = None) -> str:
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
            args = json.loads(args_json)
        except (TypeError, ValueError):
            return json.dumps({"error": "工具参数必须是有效的 JSON"}, ensure_ascii=False)
        if not isinstance(args, dict):
            return json.dumps({"error": "工具参数必须是 JSON 对象"}, ensure_ascii=False)

        # Built-ins win name conflicts, including unadvertised compatibility stubs.
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
        from mohobot.services.llm_tools import registry
        if registry.contains(func_name):
            return await registry.execute(func_name, args)
        return json.dumps({"error": f"未知工具: {func_name}"}, ensure_ascii=False)

    @staticmethod
    async def _close_clients(clients: list) -> None:
        """Attempt each distinct client once, even if another client fails to close."""
        seen = set()
        for client in clients:
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            try:
                await client.close()
            except Exception as exc:
                logger.warning(f"LLM client close failed: {exc}")

    async def close(self) -> None:
        """Close current and retired HTTP clients once, deduplicating shared roles."""
        clients = list(getattr(self, "_retired_clients", []))
        self._retired_clients = []
        for role in ("chat", "vision", "emotion"):
            clients.append(getattr(self, f"_{role}_client", None))
            setattr(self, f"_{role}_client", None)
        self._available = False
        self._vision_available = False
        self._client_identities = {}
        await self._close_clients(clients)
        recorder = getattr(self, "_usage_recorder", None)
        if getattr(self, "_owns_usage_recorder", False) and recorder is not None:
            self._owns_usage_recorder = False
            await recorder.close()
