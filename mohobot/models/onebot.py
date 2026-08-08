"""OneBot v11 protocol data models.

Covers events, API calls, message segments, and quick operations
as defined in the OneBot v11 standard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ── Message Segment ──────────────────────────────────────────

@dataclass
class MessageSegment:
    """A single message segment in array format."""
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text(cls, text: str) -> "MessageSegment":
        return cls(type="text", data={"text": text})

    @classmethod
    def at(cls, qq: str | int) -> "MessageSegment":
        return cls(type="at", data={"qq": str(qq)})

    @classmethod
    def at_all(cls) -> "MessageSegment":
        return cls(type="at", data={"qq": "all"})

    @classmethod
    def image(cls, file: str, url: str = "") -> "MessageSegment":
        data = {"file": file}
        if url:
            data["url"] = url
        return cls(type="image", data=data)

    @classmethod
    def reply(cls, message_id: int | str) -> "MessageSegment":
        return cls(type="reply", data={"id": str(message_id)})

    @classmethod
    def face(cls, face_id: int | str) -> "MessageSegment":
        return cls(type="face", data={"id": str(face_id)})


# ── Sender ────────────────────────────────────────────────────

@dataclass
class Sender:
    """Message sender information."""
    user_id: int
    nickname: str = ""
    sex: str = "unknown"
    age: int = 0
    # Group-specific
    card: str = ""
    area: str = ""
    level: str = ""
    role: str = "member"
    title: str = ""


# ── Events ────────────────────────────────────────────────────

@dataclass
class Event:
    """Base OneBot event."""
    time: int
    self_id: int
    post_type: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        post_type = d.get("post_type", "")

        if post_type == "message":
            return MessageEvent.from_dict(d)
        elif post_type == "notice":
            return NoticeEvent.from_dict(d)
        elif post_type == "request":
            return RequestEvent.from_dict(d)
        elif post_type == "meta_event":
            return MetaEvent.from_dict(d)
        else:
            return cls(time=d.get("time", 0), self_id=d.get("self_id", 0), post_type=post_type)


# ── Message Event ─────────────────────────────────────────────

@dataclass
class MessageEvent(Event):
    """Base message event."""
    message_type: str = ""
    sub_type: str = ""
    message_id: int = 0
    user_id: int = 0
    message: list[dict[str, Any]] | str = ""
    raw_message: str = ""
    font: int = 0
    sender: Sender = field(default_factory=Sender)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MessageEvent":
        message_type = d.get("message_type", "")

        if message_type == "private":
            return PrivateMessageEvent.from_dict(d)
        elif message_type == "group":
            return GroupMessageEvent.from_dict(d)
        else:
            sender_dict = d.get("sender", {})
            return cls(
                time=d.get("time", 0),
                self_id=d.get("self_id", 0),
                post_type="message",
                message_type=message_type,
                sub_type=d.get("sub_type", ""),
                message_id=d.get("message_id", 0),
                user_id=d.get("user_id", 0),
                message=d.get("message", ""),
                raw_message=d.get("raw_message", ""),
                font=d.get("font", 0),
                sender=Sender(**{k: v for k, v in sender_dict.items() if k in Sender.__dataclass_fields__}),
            )


@dataclass
class PrivateMessageEvent(MessageEvent):
    """Private message event."""
    message_type: Literal["private"] = "private"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PrivateMessageEvent":
        sender_dict = d.get("sender", {})
        return cls(
            time=d.get("time", 0),
            self_id=d.get("self_id", 0),
            post_type="message",
            message_type="private",
            sub_type=d.get("sub_type", "friend"),
            message_id=d.get("message_id", 0),
            user_id=d.get("user_id", 0),
            message=d.get("message", ""),
            raw_message=d.get("raw_message", ""),
            font=d.get("font", 0),
            sender=Sender(**{k: v for k, v in sender_dict.items() if k in Sender.__dataclass_fields__}),
        )


@dataclass
class GroupMessageEvent(MessageEvent):
    """Group message event."""
    message_type: Literal["group"] = "group"
    group_id: int = 0
    anonymous: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GroupMessageEvent":
        sender_dict = d.get("sender", {})
        return cls(
            time=d.get("time", 0),
            self_id=d.get("self_id", 0),
            post_type="message",
            message_type="group",
            sub_type=d.get("sub_type", "normal"),
            message_id=d.get("message_id", 0),
            user_id=d.get("user_id", 0),
            group_id=d.get("group_id", 0),
            message=d.get("message", ""),
            raw_message=d.get("raw_message", ""),
            font=d.get("font", 0),
            anonymous=d.get("anonymous"),
            sender=Sender(**{k: v for k, v in sender_dict.items() if k in Sender.__dataclass_fields__}),
        )

    def is_mentioned(self, self_id: int | str) -> bool:
        """Check if the bot was @mentioned DIRECTLY (by its QQ, not @all).

        Note: reply-quote triggers are NOT checked here — they require
        knowing whether the quoted message was sent by the bot, which is
        handled in message_handler via sent-message tracking.
        """
        if isinstance(self_id, str):
            self_id = int(self_id)
        if isinstance(self.message, list):
            for seg in self.message:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "at":
                    # Only a direct @mention of the bot itself counts
                    if seg.get("data", {}).get("qq") == str(self_id):
                        return True
        return False


# ── Notice Event ──────────────────────────────────────────────

@dataclass
class NoticeEvent(Event):
    """Notice event (group admin changes, member changes, etc.)."""
    notice_type: str = ""
    user_id: int = 0
    # Group-specific
    group_id: int = 0
    operator_id: int = 0
    # Extra data
    target_id: int = 0
    duration: int = 0
    sub_type: str = ""
    file: dict[str, Any] | None = None  # For group_file_upload
    file_id: str = ""  # For group_file_upload actually it's not in spec but common
    honor_type: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NoticeEvent":
        return cls(
            time=d.get("time", 0),
            self_id=d.get("self_id", 0),
            post_type="notice",
            notice_type=d.get("notice_type", ""),
            user_id=d.get("user_id", 0),
            group_id=d.get("group_id", 0),
            operator_id=d.get("operator_id", 0),
            target_id=d.get("target_id", 0),
            duration=d.get("duration", 0),
            sub_type=d.get("sub_type", ""),
            honor_type=d.get("honor_type", ""),
        )


# ── Request Event ─────────────────────────────────────────────

@dataclass
class RequestEvent(Event):
    """Request event (friend add, group join/invite)."""
    request_type: str = ""
    user_id: int = 0
    comment: str = ""
    flag: str = ""
    group_id: int = 0
    sub_type: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RequestEvent":
        return cls(
            time=d.get("time", 0),
            self_id=d.get("self_id", 0),
            post_type="request",
            request_type=d.get("request_type", ""),
            user_id=d.get("user_id", 0),
            comment=d.get("comment", ""),
            flag=d.get("flag", ""),
            group_id=d.get("group_id", 0),
            sub_type=d.get("sub_type", ""),
        )


# ── Meta Event ────────────────────────────────────────────────

@dataclass
class MetaEvent(Event):
    """Meta event (lifecycle, heartbeat)."""
    meta_event_type: str = ""
    status: dict[str, Any] | None = None
    interval: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MetaEvent":
        return cls(
            time=d.get("time", 0),
            self_id=d.get("self_id", 0),
            post_type="meta_event",
            meta_event_type=d.get("meta_event_type", ""),
            status=d.get("status"),
            interval=d.get("interval", 0),
        )


# ── API Call & Response ───────────────────────────────────────

@dataclass
class ApiCall:
    """An API call from the OneBot client (action request)."""
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    echo: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApiCall":
        return cls(
            action=d.get("action", ""),
            params=d.get("params", {}),
            echo=d.get("echo"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"action": self.action, "params": self.params}
        if self.echo is not None:
            result["echo"] = self.echo
        return result


@dataclass
class ApiResponse:
    """Response to an API call."""
    status: str = "ok"
    retcode: int = 0
    data: Any = None
    echo: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "retcode": self.retcode,
            "data": self.data,
        }
        if self.echo is not None:
            result["echo"] = self.echo
        return result


# ── Quick Operation (for event response) ──────────────────────

@dataclass
class QuickOperation:
    """Quick operation response for message events."""
    reply: str | list[dict[str, Any]] | None = None
    auto_escape: bool = False
    at_sender: bool = False
    delete: bool = False
    kick: bool = False
    ban: bool = False
    ban_duration: int = 30 * 60  # 30 minutes

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.reply is not None:
            result["reply"] = self.reply
            result["auto_escape"] = self.auto_escape
        if self.at_sender:
            result["at_sender"] = True
        if self.delete:
            result["delete"] = True
        if self.kick:
            result["kick"] = True
        if self.ban:
            result["ban"] = True
            result["ban_duration"] = self.ban_duration
        return result