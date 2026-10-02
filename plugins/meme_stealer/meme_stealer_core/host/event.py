"""mohobot 消息事件的包装。

mohobot 把 ``(bot_id, event, raw)`` 交给插件钩子；核心代码需要的是一个能
读发送者/群号/消息组件、能回复消息的事件对象。``StealerEvent`` 只做这层
转换，发送与取图委托给 :class:`HostBridge`（由插件入口用 WSServer 实现）。
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, Protocol

from .components import (
    Image,
    MessageEventResult,
    Plain,
    segments_to_components,
    to_onebot_segments,
)
from .log import logger


class HostBridge(Protocol):
    async def send_segments(
        self, bot_id: str, chat_type: str, chat_id: str, segments: list[dict[str, Any]]
    ) -> bool: ...

    async def fetch_image_data_uri(self, bot_id: str, file_ref: str) -> str: ...

    async def fetch_message_segments(self, bot_id: str, message_id: str) -> list[dict[str, Any]]: ...


class RawEvent(dict):
    """原始 OneBot 事件：既能 ``raw["message"]`` 也能 ``raw.message``。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


_CQ_PATTERN = re.compile(r"\[CQ:([a-zA-Z0-9_-]+)((?:,[^\]]*)?)\]")


def _unescape_cq(text: str) -> str:
    return (
        text.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def parse_message_segments(message: Any) -> list[dict[str, Any]]:
    """OneBot 消息（数组或 CQ 码字符串）→ 段列表。"""
    if isinstance(message, list):
        return [seg for seg in message if isinstance(seg, dict)]
    if not isinstance(message, str) or not message:
        return []
    try:
        from mohobot.utils.cq_code import parse_cq_code

        return [seg for seg in parse_cq_code(message) if isinstance(seg, dict)]
    except Exception:
        pass
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in _CQ_PATTERN.finditer(message):
        if match.start() > cursor:
            segments.append(
                {"type": "text", "data": {"text": _unescape_cq(message[cursor:match.start()])}}
            )
        data: dict[str, Any] = {}
        for pair in match.group(2).lstrip(",").split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                data[key] = _unescape_cq(value)
        segments.append({"type": match.group(1), "data": data})
        cursor = match.end()
    if cursor < len(message):
        segments.append({"type": "text", "data": {"text": _unescape_cq(message[cursor:])}})
    return segments


def _text(value: Any) -> str:
    return str(value or "").strip()


class StealerEvent:
    """单条消息在插件内的视图。"""

    def __init__(
        self,
        bot_id: str,
        event: Any,
        raw: dict[str, Any] | None = None,
        *,
        bridge: HostBridge | None = None,
    ) -> None:
        raw = raw if isinstance(raw, dict) else {}
        self.bot_id = str(bot_id or "")
        self.event = event
        self.bridge = bridge

        self.group_id = _text(getattr(event, "group_id", None) or raw.get("group_id"))
        self.user_id = _text(getattr(event, "user_id", None) or raw.get("user_id"))
        self.self_id = _text(getattr(event, "self_id", None) or raw.get("self_id"))
        self.message_id = _text(getattr(event, "message_id", None) or raw.get("message_id"))
        sender = getattr(event, "sender", None)
        raw_sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        self.sender_name = (
            _text(getattr(sender, "card", None) or raw_sender.get("card"))
            or _text(getattr(sender, "nickname", None) or raw_sender.get("nickname"))
            or self.user_id
        )

        message = getattr(event, "message", None)
        if message in (None, ""):
            message = raw.get("message")
        self._segments = parse_message_segments(message)
        self._components: list[Any] | None = None
        self._extras: dict[str, Any] = {}

        raw_message = RawEvent(raw)
        raw_message["message"] = self._segments
        self.message_obj = SimpleNamespace(
            raw_message=raw_message,
            message_id=self.message_id,
            group_id=self.group_id,
            self_id=self.self_id,
            type=self.chat_type,
            sender=SimpleNamespace(user_id=self.user_id, nickname=self.sender_name),
        )

    # ── 会话标识 ───────────────────────────────────────────

    @property
    def chat_type(self) -> str:
        return "group" if self.group_id else "private"

    @property
    def chat_id(self) -> str:
        return self.group_id or self.user_id

    @property
    def unified_msg_origin(self) -> str:
        """冷却、聊天记录、强制收录窗口共用的会话键。

        群聊按群号（同群多个 bot 共享一份上下文和冷却）；私聊按 bot + 用户，
        不同 bot 与同一用户的私聊是不同会话。
        """
        if self.group_id:
            return f"GroupMessage:{self.group_id}"
        return f"{self.bot_id}:FriendMessage:{self.user_id}"

    def get_session_id(self) -> str:
        return self.chat_id

    def get_group_id(self) -> str:
        return self.group_id

    def get_sender_id(self) -> str:
        return self.user_id

    def get_sender_name(self) -> str:
        return self.sender_name

    def get_self_id(self) -> str:
        return self.self_id

    def is_private_chat(self) -> bool:
        return not self.group_id

    # ── 消息内容 ───────────────────────────────────────────

    def get_messages(self) -> list[Any]:
        if self._components is None:
            resolver = self._resolve_image if self.bridge is not None else None
            self._components = segments_to_components(self._segments, resolver)
        return self._components

    def get_message_str(self) -> str:
        return "".join(
            comp.text for comp in self.get_messages() if isinstance(comp, Plain)
        ).strip()

    def get_images(self) -> list[Image]:
        return [comp for comp in self.get_messages() if isinstance(comp, Image)]

    def get_reply_id(self) -> str:
        """被引用消息的 message_id；没有引用返回空串。"""
        for seg in self._segments:
            if seg.get("type") == "reply":
                data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                return _text(data.get("id"))
        return ""

    async def get_quoted_images(self) -> list[Image]:
        """被引用那条消息里的图片（OneBot get_msg）；没有引用或取不到时返回空列表。"""
        reply_id = self.get_reply_id()
        if not reply_id or self.bridge is None:
            return []
        segments = await self.bridge.fetch_message_segments(self.bot_id, reply_id)
        resolver = self._resolve_image if self.bridge is not None else None
        return [
            comp
            for comp in segments_to_components(parse_message_segments(segments), resolver)
            if isinstance(comp, Image)
        ]

    async def _resolve_image(self, image: Image) -> str:
        file_ref = image.file
        if not file_ref or self.bridge is None:
            return ""
        return await self.bridge.fetch_image_data_uri(self.bot_id, file_ref)

    # ── 附加状态 ───────────────────────────────────────────

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    def get_extra(self, key: str | None = None, default: Any = None) -> Any:
        if key is None:
            return dict(self._extras)
        return self._extras.get(key, default)

    # ── 回复 ───────────────────────────────────────────────

    def make_result(self) -> MessageEventResult:
        return MessageEventResult()

    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult().message(text)

    def image_result(self, url_or_path: str) -> MessageEventResult:
        ref = str(url_or_path or "")
        if ref.startswith(("http://", "https://")):
            return MessageEventResult().url_image(ref)
        return MessageEventResult().file_image(ref)

    def chain_result(self, chain: list[Any]) -> MessageEventResult:
        return MessageEventResult(chain)

    async def send(self, content: Any) -> bool:
        """把文本/组件链/结果发回本会话。"""
        return await self.send_segments(to_onebot_segments(content))

    async def send_segments(self, segments: list[dict[str, Any]]) -> bool:
        if not segments:
            return False
        if self.bridge is None:
            logger.warning("[Stealer] 未连接 mohobot，无法发送消息")
            return False
        return await self.bridge.send_segments(
            self.bot_id, self.chat_type, self.chat_id, segments
        )

    def __repr__(self) -> str:
        return (
            f"StealerEvent(bot={self.bot_id}, {self.chat_type}={self.chat_id}, "
            f"user={self.user_id}, mid={self.message_id})"
        )
