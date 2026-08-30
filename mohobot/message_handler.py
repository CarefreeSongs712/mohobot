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
    """Orchestrates the message processing pipeline.

    Flow:
      1. Receive raw OneBot event
      2. Archive to history/ (JSONL raw event log)
      3. Classify: notice/meta → plugin hooks; message → process
      4. Group gate: only respond if @mentioned, replied-to, or command
      5. Interceptors run (commands → keywords → plugins)
      6. Legacy path: Context load → LLM streaming → reply-quote first chunk
      7. Save context after reply completes
    """

    def __init__(
        self,
        ws_server,
        context_manager,
        llm_service,
        plugin_system,
        data_dir: str = "./data",
        context_max_rounds: int = 30,
        reply_config=None,
        database_manager=None,
        image_cache=None,
        global_config=None,
        song_matcher=None,
        emotion_manager=None,
    ):
        self._ws = ws_server
        self._ctx_mgr = context_manager
        self._llm = llm_service
        self._plugins = plugin_system
        self._data_dir = data_dir
        self._context_max_rounds = context_max_rounds
        self._interceptors: list = []  # Ordered list of interceptors
        self._global_config = global_config  # GlobalConfig(戳回复等全局配置读取)
        self._writer_registry: dict[str, JSONLWriter] = {}
        # 全局歌曲匹配器(识别 + 注解; 未注入时降级)
        self._song_matcher = song_matcher
        # 情感系统(注入 + 后台分析; 未启用时为 None)
        self._emotion = emotion_manager

        self._db = database_manager
        # 图片缓存(下载 + phash 去重 + 描述缓存)
        self._image_cache = image_cache

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

        # 群聊最近消息缓冲: f"{bot_id}:{group_id}" -> [{"user_id","name","content","time"}, ...]
        # 仅保存在内存, 生成回复时临时注入 prompt(不写入 context, 不参与 AI 总结)。
        # 条数取全局配置 group_recent_msgs_count(0 = 关闭)。
        if global_config is not None:
            self._group_recent_count = max(0, int(getattr(global_config, "group_recent_msgs_count", 10)))
        else:
            self._group_recent_count = 10
        self._group_recent_msgs: dict[str, list[dict]] = {}

        # 环境感知缓存: {(bot_id, chat_type, chat_id): 感知文本}
        # 每次收到消息时从插件收集, 附加到 LLM 回复请求(不写入 context)
        self._perception_text: dict[tuple, str] = {}

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
        # ping/PONG: 群聊不 @ 也回复(全局功能, 无需斜杠)
        if text.strip().lower() == "ping":
            return True

        # Direct @mention of the bot (bot_id 是内部编号, @ 的是绑定 QQ)
        instance = None
        if self._ws and self._ws._bot_manager:
            instance = self._ws._bot_manager.get(bot_id)
        bot_qq = str(instance.qq) if instance else bot_id
        if event.is_mentioned(bot_qq):
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

        # ── 群消息: 记录群内 bot 存在(全局指令去重依据) ──
        if isinstance(event, GroupMessageEvent):
            if self._ws and self._ws._bot_manager:
                self._ws._bot_manager.note_group_message(bot_id, event.group_id)
            # 群聊最近消息缓冲(回复时临时注入, 不写入 context)
            await self._note_group_recent(bot_id, event)

        # ── 环境感知: 每次消息刷新缓存(时间/节假日/农历/节气/群聊环境) ──
        # 供 LLM 回复请求注入, 不写入 context
        if self._plugins is not None:
            try:
                chat_type = self._get_chat_type(event)
                chat_id = self._get_chat_id(event)
                perception = await self._plugins.collect_perception(bot_id, event, raw)
                if perception:
                    self._perception_text[(bot_id, chat_type, chat_id)] = perception
            except Exception as e:
                logger.debug(f"Collect perception failed: {e}")

        # ── 插件观察钩子: 所有消息(含未 @bot 的群消息)先过一遍插件 ──
        # (活跃记录 / 求婚"同意/拒绝"回复 / 无前缀关键词触发)。
        # 插件明确消费时发送回复并结束; 否则继续正常流程。
        if self._plugins is not None:
            try:
                observed_handled, observed_reply = await self._plugins.dispatch_observed(
                    bot_id, event, raw
                )
                if observed_handled:
                    if observed_reply:
                        await self._send_reply(bot_id, event, observed_reply)
                    return
            except Exception as e:
                logger.exception(f"Plugin observe dispatch error: {e}")

        # ── Group gate: only respond if @mentioned, replied-to, or command ──
        if isinstance(event, GroupMessageEvent):
            if not await self._should_respond_to_group(bot_id, event):
                logger.debug(f"Skipping group message (not mentioned): user={event.user_id}")
                return
            # ── 全局指令去重: 群内多 bot 时只由 bot_id 最小者回复 ──
            # (如 /占卜 /help; 插件可通过类属性 global_triggers 声明)
            if self._should_defer_global_command(bot_id, event):
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

        # ── 图片引用归一化: NapCat 群聊图片常只有 file 文件名/路径而无 url,
        # 通过 OneBot get_image API 换取 base64 → data URI(视觉描述/多模态可用)
        await self._normalize_image_segments(bot_id, event)

        # ── Run interceptor chain ──
        for interceptor in self._interceptors:
            handled, response = await interceptor.intercept(bot_id, event, raw)
            if handled:
                if response:
                    await self._send_reply(bot_id, event, response)
                return

        # ── ping/PONG: 去除首尾空白后完全匹配(忽略大小写), 群聊不 @ 也回复 ──
        # (群 gate 已放行 ping; 被 ban 用户已被拦截链过滤)
        if self._is_ping_text(event):
            await self._send_reply(bot_id, event, "PONG")
            return

        # ── LLM path ──
        chat_type = self._get_chat_type(event)
        chat_id = self._get_chat_id(event)

        # ── Legacy path: streaming with reply-quote ──
        # Load session context (+ 群聊最近消息临时注入, 不写回 context 文件)
        context = await self._build_legacy_context(bot_id, chat_type, chat_id)

        # Stream response — split by punctuation + length (标点符号+长度分隔法)
        full_reply = await self._stream_llm_reply(bot_id, event, context, raw)

        # Save context after streaming completes
        if full_reply.strip():
            user_msg = {
                # Role = "QQ号-昵称" so the LLM knows WHO said it
                "role": self._speaker_role(event),
                "content": self._context_content(event.message),
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
            # history → 数据库 (SQLite)
            self._persist_legacy_turn(
                bot_id, chat_type, chat_id, event,
                user_msg["content"], full_reply,
            )
            # 情感系统: 后台分析本轮对话(不阻塞, 失败不影响回复)
            if self._emotion is not None:
                try:
                    self._emotion.schedule_turn(
                        bot_id, event, user_msg["content"], full_reply,
                    )
                except Exception as e:
                    logger.debug(f"情感分析调度失败: {e}")

    # ── 图片引用归一化 ─────────────────────────────────────────

    @staticmethod
    def _context_content(message) -> str:
        """上下文/数据库存储用的消息文本。

        有纯文本时直接用(带图时补 [图片] 标记); 无文本(纯图/语音/表情等)
        时按段类型给占位符, 避免把整个段列表的 repr(含归一化后的
        base64 data URI)写进上下文并被送进 LLM。
        """
        text = extract_plain_text(message)
        if text:
            if isinstance(message, list) and any(
                isinstance(seg, dict) and seg.get("type") == "image"
                for seg in message
            ):
                return f"{text} [图片]"
            return text
        placeholders = {
            "image": "[图片]",
            "record": "[语音]",
            "video": "[视频]",
            "face": "[表情]",
            "forward": "[合并转发]",
            "json": "[卡片消息]",
            "file": "[文件]",
        }
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict):
                    hit = placeholders.get(seg.get("type"))
                    if hit:
                        return hit
        return "[消息]"

    async def _resolve_image_data_uri(self, bot_id: str, file_ref: str) -> str:
        """file 文件名/路径 → OneBot get_image → data URI(失败返回 "")。

        NapCat 群聊图片的 image 段常只有 file 字段而没有可下载 url,
        需调用 get_image API 换取 base64(按魔数推断 mime)。
        """
        if not file_ref or file_ref.startswith("base64://"):
            return ""
        ws = self._ws
        if ws is None:
            return ""
        try:
            resp = await ws.send_to_bot(
                bot_id, "get_image", {"file": file_ref},
                wait_response=True, timeout=8.0,
            )
            b64 = str(((resp or {}).get("data") or {}).get("base64", "") or "")
            if b64:
                import base64 as _b64
                mime = "image/jpeg"
                try:
                    head = _b64.b64decode(b64[:64])
                    if head[:4] == b"\x89PNG":
                        mime = "image/png"
                    elif head[:3] == b"GIF":
                        mime = "image/gif"
                    elif head[:2] == b"BM":
                        mime = "image/bmp"
                    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                        mime = "image/webp"
                except Exception:
                    pass
                return f"data:{mime};base64,{b64}"
        except Exception as e:
            logger.debug(f"get_image 失败({file_ref[:40]}): {e}")
        return ""

    async def _normalize_image_segments(self, bot_id: str, event: MessageEvent) -> None:
        """把无 url 的图片段(file 为文件名/路径)通过 OneBot get_image 换成 data URI。

        NapCat 群聊图片的 image 段常只有 file 字段(文件名或 file:// 路径)而没有
        可下载的 url, 视觉描述/多模态直接取不到图。这里调用 get_image API
        换取 base64 并写入 data["url"], 下游统一走 data URI。
        失败时保持原样(调用方降级为 "[图片]")。
        """
        if not isinstance(event.message, list):
            return
        for seg in event.message:
            if not (isinstance(seg, dict) and seg.get("type") == "image"):
                continue
            data = seg.setdefault("data", {})
            if str(data.get("url", "") or "").strip():
                continue  # 已有 url
            file_ref = str(data.get("file", "") or "")
            if not file_ref or file_ref.startswith("base64://"):
                continue
            uri = await self._resolve_image_data_uri(bot_id, file_ref)
            if uri:
                data["url"] = uri
                logger.debug(f"图片 get_image 成功: {file_ref[:40]} → data URI")

    def _vision_callback(self, bot_id: str):
        """视觉描述回调(供 ImageCache 使用): 本地文件 base64 内嵌, 30s 超时。"""
        async def _cb(image_url: str, local_path: str) -> str:
            try:
                return await asyncio.wait_for(
                    self._llm.describe_image_file(local_path),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning(f"Vision describe timeout for bot {bot_id}")
                return ""
            except Exception as e:
                logger.warning(f"Vision describe failed for bot {bot_id}: {e}")
                return ""
        return _cb

    # ── ping/PONG ──────────────────────────────────────────────

    @staticmethod
    def _is_ping_text(event) -> bool:
        """去除首尾空白后完全匹配 ping(忽略大小写)。"""
        from mohobot.utils.cq_code import extract_plain_text as _ept
        text = ""
        if isinstance(event.message, str):
            text = event.message
        elif isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
        return text.strip().lower() == "ping"

    # ── 群聊最近消息(内存缓冲, 回复时临时注入) ─────────────────

    async def _song_annotation_for_event(self, event: MessageEvent) -> str:
        """识别消息中的歌曲并返回【歌曲信息】注解段(无命中返回 "")。

        供 LLMService 注入。
        消息原文(含图片描述)做识别; 库为空/失败 → 空串, 正常走 LLM。
        """
        if self._song_matcher is None:
            return ""
        try:
            text = (extract_plain_text(event.message) or "").strip()
            if not text:
                return ""
            match = self._song_matcher.match(text)
            if match is None:
                return ""
            return match.build_annotation()
        except Exception as e:
            logger.debug(f"歌曲识别失败: {e}")
            return ""

    async def _note_group_recent(self, bot_id: str, event: GroupMessageEvent) -> None:
        """把一条群消息记入最近消息缓冲(仅内存, 满 N 条淘汰最旧)。

        图片消息标记 image=True 并记录引用(url/file), 注入时只对最新一张
        调 VLM 识别(走全局缓存), 旧图显示 "[图片]" 占位。
        """
        if self._group_recent_count <= 0:
            return
        try:
            from mohobot.utils.cq_code import extract_image_urls, extract_plain_text
            text = (extract_plain_text(event.message) or "").strip()
            sender = getattr(event, "sender", None)
            name = ""
            if sender is not None:
                name = (getattr(sender, "card", "") or getattr(sender, "nickname", "") or "").strip()
            entry: dict = {
                "user_id": str(event.user_id),
                "name": name or str(event.user_id),
                "content": text[:80],  # 单条截断, 防注入过长
                "time": int(event.time or 0),
            }
            # 图片引用: 优先 url/data URI; 未归一化时兜底 file(识别时再 get_image)
            image_urls = extract_image_urls(event.message)
            if image_urls:
                entry["image"] = True
                entry["image_ref"] = image_urls[0]
            elif isinstance(event.message, list):
                for seg in event.message:
                    if isinstance(seg, dict) and seg.get("type") == "image":
                        file_ref = str((seg.get("data") or {}).get("file", "") or "")
                        if file_ref and not file_ref.startswith("base64://"):
                            entry["image"] = True
                            entry["image_ref"] = ""
                            entry["image_file"] = file_ref
                        break
            key = f"{bot_id}:{event.group_id}"
            buf = self._group_recent_msgs.setdefault(key, [])
            buf.append(entry)
            if len(buf) > self._group_recent_count:
                del buf[:len(buf) - self._group_recent_count]
        except Exception as e:
            logger.debug(f"Note group recent message failed: {e}")

    async def _describe_recent_image(self, bot_id: str, entry: dict) -> str:
        """识别群聊临时上下文中的一张图片(走全局 ImageCache 缓存)。"""
        ref = entry.get("image_ref") or ""
        if not ref and entry.get("image_file"):
            ref = await self._resolve_image_data_uri(bot_id, entry["image_file"])
        if not ref or self._image_cache is None or self._llm is None:
            return ""
        try:
            _, desc = await self._image_cache.get_or_describe(
                ref, vision_callback=self._vision_callback(bot_id),
            )
            return desc if desc and desc != "[图片]" else ""
        except Exception as e:
            logger.debug(f"Describe recent image failed: {e}")
            return ""

    async def _format_group_recent(self, bot_id: str, group_id) -> str:
        """把缓冲中的最近群消息格式化为 prompt 文本段(空则返回 "")。

        图片消息: 只对最新一张调 VLM 识别(走缓存), 其他标 "[图片]" 占位。
        """
        if self._group_recent_count <= 0:
            return ""
        buf = self._group_recent_msgs.get(f"{bot_id}:{group_id}", [])
        if not buf:
            return ""
        # 最新一张图片的索引(从后往前找)
        latest_img_idx = None
        for i in range(len(buf) - 1, -1, -1):
            if buf[i].get("image"):
                latest_img_idx = i
                break
        desc = ""
        if latest_img_idx is not None:
            desc = await self._describe_recent_image(bot_id, buf[latest_img_idx])
        from mohobot.utils.time_utils import TZ_UTC8
        from datetime import datetime as _dt
        lines = []
        for i, entry in enumerate(buf):
            t = ""
            if entry.get("time"):
                try:
                    t = _dt.fromtimestamp(entry["time"], TZ_UTC8).strftime("%H:%M")
                except Exception:
                    t = ""
            content = entry.get("content", "")
            if entry.get("image"):
                if i == latest_img_idx and desc:
                    content = f"{content} [图片]（{desc}）" if content else f"[图片]（{desc}）"
                else:
                    content = f"{content} [图片]" if content else "[图片]"
            lines.append(f"{t} {entry['name']}: {content}")
        return "【群聊最近消息】\n" + "\n".join(lines)

    async def _build_legacy_context(self, bot_id: str, chat_type: str, chat_id: str) -> list[dict]:
        """加载会话上下文, 群聊时临时附加最近消息段 + 环境感知段。

        附加的 system 条目不写回 context 文件, 不参与上下文压缩总结。
        """
        context = await self._ctx_mgr.load_context(bot_id, chat_type, chat_id)
        context = list(context)
        if chat_type == "group":
            recent = await self._format_group_recent(bot_id, chat_id)
            if recent:
                context.append({"role": "system", "content": recent})
        # 环境感知(仅 LLM 请求, 不写入 context)
        perception = self._perception_text.get((bot_id, chat_type, chat_id), "")
        if perception:
            context.append({
                "role": "system",
                "content": f"【环境感知】\n{perception}",
            })
        # 情感系统(仅 LLM 请求, 不写入 context): 对该用户的情感状态 + 语气指导
        if self._emotion is not None:
            try:
                emotion_block = await self._emotion.build_context_block(bot_id, event)
                if emotion_block:
                    context.append({"role": "system", "content": emotion_block})
            except Exception as e:
                logger.debug(f"情感上下文注入失败: {e}")
        return context

    # ── 工具结果泄漏防御(双保险) ─────────────────────────────

    @staticmethod
    def _sanitize_tool_leak(text) -> str:
        """发送前拦截工具结果泄漏: 整条以 "[工具" 开头 → 丢弃;
        含 "\n[工具调用: " 拼接后缀 → 截断。返回 "" 表示不发送。"""
        if not isinstance(text, str) or not text:
            return text
        if text.startswith("[工具"):
            logger.warning("拦截工具结果泄漏消息(整条丢弃)")
            return ""
        idx = text.find("\n[工具调用: ")
        if idx >= 0:
            logger.warning("截断工具结果泄漏后缀")
            return text[:idx]
        return text

    def _persist_legacy_turn(
        self, bot_id: str, chat_type: str, chat_id: str,
        event: MessageEvent, user_text: str, ai_text: str,
    ) -> None:
        """Legacy 路径下把这一轮对话写入数据库 (history → DB)。"""
        if self._db is None:
            return
        try:
            user_id = str(event.user_id) if chat_type == "private" else chat_id
            self._db.add_conversation(
                user_id, bot_id, "user", user_text,
                msg_type="text", meta_data={"chat_type": chat_type, "chat_id": chat_id},
            )
            self._db.add_conversation(
                user_id, bot_id, "agent", ai_text,
                msg_type="text", meta_data={"chat_type": chat_type, "chat_id": chat_id},
            )
        except Exception as e:
            logger.debug(f"Persist legacy turn to DB failed: {e}")

    @staticmethod
    def _speaker_role(event: MessageEvent) -> str:
        """Build a speaker role string: \"{qq}-{nickname}\" (e.g. 3831097597-墨染荷韵)."""
        if isinstance(event, GroupMessageEvent):
            nickname = event.sender.card or event.sender.nickname or f"User-{event.user_id}"
        else:
            nickname = event.sender.nickname or f"User-{event.user_id}"
        return f"{event.user_id}-{nickname}"

    def _bot_config(self, bot_id: str):
        """当前 bot 的私有配置(BotConfig), 用于旧版路径的人设/模型覆盖。"""
        if self._ws and self._ws._bot_manager:
            instance = self._ws._bot_manager.get(bot_id)
            if instance is not None:
                return instance.config
        return None

    # ── Reply behavior (config-driven) ────────────────────────

    # Strong break punctuation (sentence end). NOTE: single \n is NOT a break —
    # double newline (\n\n) is handled separately in _find_cut as a paragraph break.
    _PUNCT_STRONG = "。！？!?…"
    # Soft break punctuation (clause end)
    _PUNCT_SOFT = "；;，,、"

    # ── 长引文保护(歌词/台词引用) ──────────────────────────────
    # 引号内的内容是完整语段(如歌词), 按标点+长度切分会把一句歌词切碎;
    # 引文长度 ≥ _QUOTE_MIN_LEN 时整体单独成一段。
    _QUOTE_OPEN = "\u201c"    # “
    _QUOTE_CLOSE = "\u201d"   # ”
    _QUOTE_MIN_LEN = 16       # 短引语(如 说"你好呀")不单独成段
    # 引文前文字的合法边界(比普通分段多允许 冒号/破折号, 如 "那就唱副歌吧：")
    _QUOTE_PREFIX_BOUNDARY = "。！？!?…；;，,、：:—\n"

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
                bot_config=self._bot_config(bot_id),
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
            bot_config=self._bot_config(bot_id),
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
            bot_config=self._bot_config(bot_id),
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
          0. 长引文保护: 引号(“”)内长度 ≥ _QUOTE_MIN_LEN 的引文(歌词/台词)
             整体单独成段, 引文未闭合(流式中途)时绝不在引文中间切分
          1. Double newline (\n\n) — paragraph break, ALWAYS splits (no min length)
          2. Hard cap: force-flush at _SEG_MAX_LEN, cutting at last punctuation
          3. Segment ≥ _SEG_MIN_LEN may flush at last strong/soft punctuation
        """
        segments: list[str] = []

        while True:
            # 0. 长引文保护(优先于其它规则)
            quote = self._find_long_quote(buffer)
            if quote is not None:
                kind, open_idx, close_idx = quote
                before = buffer[:open_idx]
                cut = self._quote_prefix_cut(before) if before else None
                if kind == "unclosed":
                    # 引文未闭合(流式中途): 引号前的文字按边界先发,
                    # 引文部分留在缓冲等待闭合, 绝不在引文中间切分
                    if cut:
                        segments.append(buffer[:cut])
                        buffer = buffer[cut:]
                    break
                # 引文已闭合且足够长 → 引文整体单独成段
                if cut is not None and 0 < cut <= open_idx:
                    segments.append(buffer[:cut])
                    buffer = buffer[cut:]
                    continue
                if not before.strip():
                    segments.append(buffer[open_idx : close_idx + 1])
                    buffer = buffer[close_idx + 1 :]
                    continue
                # 引号前有文字但无边界 → 与引文黏在一起成段(不在句中硬切)
                segments.append(buffer[: close_idx + 1])
                buffer = buffer[close_idx + 1 :]
                continue

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

    def _find_long_quote(self, text: str) -> tuple[str, int, int] | None:
        """找第一对长度 ≥ _QUOTE_MIN_LEN 的引号。

        返回 ("closed", 引号起, 引号止) / ("unclosed", 引号起, 占位);
        引号未闭合(流式中途)时即使后面还有成对引号也视为未闭合。
        没有长引文返回 None(短引语走普通分段规则)。
        """
        open_idx = text.find(self._QUOTE_OPEN)
        while open_idx != -1:
            close_idx = text.find(self._QUOTE_CLOSE, open_idx + 1)
            if close_idx == -1:
                return ("unclosed", open_idx, -1)
            if close_idx - open_idx - 1 >= self._QUOTE_MIN_LEN:
                return ("closed", open_idx, close_idx)
            open_idx = text.find(self._QUOTE_OPEN, close_idx + 1)
        return None

    def _quote_prefix_cut(self, before: str) -> int | None:
        """引文前文字的边界切点(双换行/强标点/冒号/软标点, 取最后一个)。"""
        idx = before.rfind("\n\n")
        if idx != -1:
            return idx + 2
        for i in range(len(before) - 1, -1, -1):
            if before[i] in self._QUOTE_PREFIX_BOUNDARY and before[i] != "\n":
                return i + 1
        return None

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
        # 戳一戳(OneBot: notice_type=notify, sub_type=poke) → 反射通道
        if event.notice_type == "notify" and event.sub_type == "poke":
            await self._handle_poke(bot_id, event)

        logger.debug(f"Notice from bot {bot_id}: {event.notice_type}")
        await self._plugins.dispatch_notice(bot_id, event, raw)

    # 戳一戳内置默认文案(bot 私有 / 全局配置都没有时兜底)
    DEFAULT_TOUCH_REPLIES = [
        "呜哇！吓我一跳～",
        "嘿嘿，别戳啦～",
        "唔……怎么了？",
        "戳我干嘛呀～",
    ]

    async def _handle_poke(self, bot_id: str, event: NoticeEvent) -> None:
        """戳一戳: 确认戳的是本 bot 后,回复固定文案。

        文案优先级: bot 私有 touch_replies > 全局 touch_replies > 内置默认。
        """

        # 判断被戳对象是不是本 bot(target_id 可能缺省)
        bot_qq = bot_id
        if self._ws and self._ws._bot_manager:
            instance = self._ws._bot_manager.get(bot_id)
            if instance is not None:
                bot_qq = str(instance.qq)
        target = str(event.target_id or "")
        if target and target not in ("0", "0.0") and target != bot_qq:
            return  # 戳的是别人,忽略

        replies = self._resolve_touch_replies(bot_id)
        if replies and self._ws:
            text = random.choice(replies)
            if event.group_id:
                await self._ws.send_group_msg(bot_id, event.group_id, text)
            else:
                await self._ws.send_private_msg(bot_id, event.user_id, text)

    def _resolve_touch_replies(self, bot_id: str) -> list[str]:
        """解析戳一戳固定回复列表: bot 私有 > 全局配置 > 内置默认。"""
        instance = None
        if self._ws and self._ws._bot_manager:
            instance = self._ws._bot_manager.get(bot_id)
        if instance is not None and getattr(instance.config, "touch_replies", []):
            return list(instance.config.touch_replies)
        if self._global_config is not None:
            global_replies = getattr(self._global_config, "touch_replies", None) or []
            if global_replies:
                return list(global_replies)
        return list(self.DEFAULT_TOUCH_REPLIES)

    async def _handle_request(self, bot_id: str, event: RequestEvent, raw: dict) -> None:
        """Handle request events (friend add, group add/invite).

        先交给插件(关系管理器等)处理: 插件 on_request 返回 True 表示已接管
        (自动规则/审批转发)。未接管时**静默不处理** —— 不自动同意也不拒绝,
        申请保留由人工/群管理员处理(bot 作为群管理员不应自动通过用户进群申请)。
        """
        if self._plugins is not None:
            try:
                handled = await self._plugins.dispatch_request(bot_id, event, raw)
                if handled:
                    return
            except Exception as e:
                logger.error(f"Request dispatch failed: {e}")

        # 插件未接管 → 静默跳过(不再自动同意)
        logger.info(
            f"Request 未被插件接管, 静默不处理: bot={bot_id} "
            f"type={event.request_type} user={event.user_id} flag={event.flag}"
        )

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
        """Alias for _send_message — send a reply.

        群聊超长纯文本(>300 字符且多行)自动改用合并转发, 避免刷屏;
        合并转发失败(客户端不支持)时回退普通文本发送。
        发送前过工具结果泄漏防御(双保险, 正常链路已不回显工具结果)。
        """
        if isinstance(reply, str):
            reply = self._sanitize_tool_leak(reply)
            if not reply:
                return
        if (
            isinstance(reply, str)
            and isinstance(event, GroupMessageEvent)
            and len(reply) >= self._forward_min_len
        ):
            if await self._try_send_forward(bot_id, event, reply):
                return
        await self._send_message(bot_id, event, reply)

    # 合并转发阈值: 群聊文本回复超过 600 字时改用合并转发
    # (短内容直接发一条; /help 等较长指令式输出自动合并)
    _forward_min_len = 600

    # 框架内置全局指令(/ 前缀, 群内多 bot 只由 bot_id 最小者回复)
    _GLOBAL_COMMANDS = {"/help"}
    # 前缀匹配的全局指令(带参数的命令): 封禁系统 /ban /pass /dec-* 系列
    _GLOBAL_COMMAND_PREFIXES = ("/ban", "/pass", "/dec-")

    def _should_defer_global_command(self, bot_id: str, event: GroupMessageEvent) -> bool:
        """全局指令去重: 命中全局指令且群内有多个 bot 时,
        非 bot_id 最小者静默跳过(不回复, 也不交给 LLM)。

        精确匹配(插件 global_triggers + 内置 /help) + 前缀匹配(封禁系统命令)。
        """
        if not (self._ws and self._ws._bot_manager):
            return False
        text = extract_plain_text(event.message).strip()
        if not text:
            return False
        # 命中集合: 内置 + 插件声明的 global_triggers
        triggers = set(self._GLOBAL_COMMANDS)
        if self._plugins is not None:
            for meta in getattr(self._plugins, "_plugins", []):
                inst = meta.get("instance")
                if inst is None:
                    continue
                gt = getattr(inst.__class__, "global_triggers", None)
                if isinstance(gt, (set, list, tuple)):
                    triggers.update(str(t) for t in gt)
        is_global = text.startswith(self._GLOBAL_COMMAND_PREFIXES)
        if not is_global:
            # 精确匹配 + 命令+空格参数的前缀匹配(如 "/点歌 白鸟" 命中 "/点歌")
            for t in triggers:
                if text == t or text.startswith(t + " "):
                    is_global = True
                    break
        if not is_global:
            return False
        # 群内最小 bot 才回复
        min_bot = self._ws._bot_manager.min_bot_for_group(str(event.group_id))
        if min_bot is None or min_bot == bot_id:
            return False
        logger.debug(f"全局指令 {text!r} 由 {min_bot} 回复, {bot_id} 跳过")
        return True

    async def _try_send_forward(self, bot_id: str, event: GroupMessageEvent, text: str) -> bool:
        """把长文本按字数切块(每块 _forward_chunk_chars 字)作为合并转发节点发送。

        不按行拆分 — 纯按字数分条。失败返回 False(回退普通发送)。
        """
        try:
            chunks = [
                text[i:i + self._forward_chunk_chars]
                for i in range(0, len(text), self._forward_chunk_chars)
            ]
            if len(chunks) < 2:
                return False  # 不足两块不应走到这里
            bot_qq, bot_nick = self._bot_identity(bot_id)
            nodes = [
                {
                    "type": "node",
                    "data": {
                        "user_id": str(bot_qq),
                        "nickname": bot_nick,
                        "content": [{"type": "text", "data": {"text": chunk}}],
                    },
                }
                for chunk in chunks
            ]
            if self._ws is None:
                return False
            await self._ws.send_group_forward_msg(bot_id, event.group_id, nodes)
            logger.debug(
                f"合并转发回复 bot={bot_id} group={event.group_id} "
                f"nodes={len(nodes)} (每块 {self._forward_chunk_chars} 字)"
            )
            return True
        except Exception as e:
            logger.warning(f"合并转发失败, 回退普通发送: {e}")
            return False

    # 合并转发分块字数(纯按字数分条, 不按行)
    _forward_chunk_chars = 1000

    def _bot_identity(self, bot_id: str) -> tuple[int, str]:
        """当前 bot 的 (QQ 号, 昵称), 用于合并转发节点署名。"""
        if self._ws and self._ws._bot_manager:
            instance = self._ws._bot_manager.get(bot_id)
            if instance is not None:
                return instance.qq or 0, getattr(instance.config, "nickname", "") or bot_id
        return 0, bot_id

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