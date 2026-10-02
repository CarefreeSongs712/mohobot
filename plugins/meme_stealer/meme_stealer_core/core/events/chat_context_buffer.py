"""收录时聊天上下文：进程内保留最近消息，供 VLM 标注时理解图片含义。

AstrBot 不向插件提供群聊原始消息记录，因此这里自行缓存。缓存只在内存中，
插件重启后从空开始；Bot 自己的回复由 LLM 响应钩子补录。
"""

from __future__ import annotations

import itertools
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from .event_context import get_event_session_key, normalize_event_value

MAX_GROUP_MESSAGES = 100
MAX_SENDER_MESSAGES = 50
MAX_TEXT_CHARS = 200
MAX_TRACKED_CONVERSATIONS = 500
MAX_TRACKED_SENDERS = 2000


@dataclass(frozen=True, slots=True)
class ChatRecord:
    seq: int
    conversation: str
    sender_id: str
    sender_name: str
    text: str
    is_bot: bool = False


def _message_text(event: Any) -> str:
    """纯文本消息内容；图片等非文本段用占位符表示。"""
    text = ""
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            text = normalize_event_value(getter())
        except Exception:
            text = ""
    has_image = False
    try:
        for comp in event.get_messages() or []:
            if type(comp).__name__ == "Image":
                has_image = True
                break
    except Exception:
        pass
    if has_image:
        text = f"{text} [图片]".strip()
    return text[:MAX_TEXT_CHARS]


def _sender(event: Any) -> tuple[str, str]:
    sender_id = ""
    sender_name = ""
    for getter_name, target in (("get_sender_id", "id"), ("get_sender_name", "name")):
        getter = getattr(event, getter_name, None)
        if not callable(getter):
            continue
        try:
            value = normalize_event_value(getter())
        except Exception:
            value = ""
        if target == "id":
            sender_id = value
        else:
            sender_name = value
    return sender_id, sender_name or sender_id or "未知用户"


def _is_group(event: Any) -> bool:
    getter = getattr(event, "get_group_id", None)
    if not callable(getter):
        return False
    try:
        return bool(normalize_event_value(getter()))
    except Exception:
        return False


class ChatContextBuffer:
    """按会话、按发送者保存最近消息；全部操作为同步，线程安全。"""

    def __init__(self) -> None:
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._conversations: OrderedDict[str, deque[ChatRecord]] = OrderedDict()
        self._senders: OrderedDict[tuple[str, str], deque[ChatRecord]] = OrderedDict()

    @staticmethod
    def _touch(store: OrderedDict, key, maxlen: int, limit: int) -> deque:
        bucket = store.get(key)
        if bucket is None:
            bucket = deque(maxlen=maxlen)
            store[key] = bucket
            while len(store) > limit:
                store.popitem(last=False)
        else:
            store.move_to_end(key)
        return bucket

    def record_event(self, event: Any) -> int:
        """记录一条用户消息，返回其序号；空消息返回 0。"""
        text = _message_text(event)
        if not text:
            return 0
        sender_id, sender_name = _sender(event)
        return self._append(
            get_event_session_key(event), sender_id, sender_name, text, is_bot=False
        )

    def record_bot_reply(self, event: Any, text: str, bot_name: str = "Bot") -> int:
        """补录 Bot 自己的文字回复，让群上下文完整。"""
        content = str(text or "").strip()[:MAX_TEXT_CHARS]
        if not content:
            return 0
        return self._append(get_event_session_key(event), "", bot_name, content, is_bot=True)

    def _append(
        self, conversation: str, sender_id: str, sender_name: str, text: str, *, is_bot: bool
    ) -> int:
        with self._lock:
            record = ChatRecord(
                seq=next(self._seq),
                conversation=conversation,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                is_bot=is_bot,
            )
            self._touch(
                self._conversations, conversation, MAX_GROUP_MESSAGES, MAX_TRACKED_CONVERSATIONS
            ).append(record)
            if sender_id and not is_bot:
                self._touch(
                    self._senders,
                    (conversation, sender_id),
                    MAX_SENDER_MESSAGES,
                    MAX_TRACKED_SENDERS,
                ).append(record)
            return record.seq

    def render_for_event(
        self,
        event: Any,
        *,
        before_seq: int,
        sender_limit: int,
        group_limit: int,
    ) -> str:
        """渲染图片所在消息之前的聊天记录；没有可用记录时返回空串。

        群聊：最近 group_limit 条群消息 + 发送者不在该窗口内的最近 sender_limit 条。
        私聊：发送者最近 sender_limit 条。
        """
        sender_limit = max(0, min(int(sender_limit or 0), MAX_SENDER_MESSAGES))
        group_limit = max(0, min(int(group_limit or 0), MAX_GROUP_MESSAGES))
        if not sender_limit and not group_limit:
            return ""
        conversation = get_event_session_key(event)
        sender_id, sender_name = _sender(event)
        in_group = _is_group(event)

        def earlier(records) -> list[ChatRecord]:
            return [r for r in records if before_seq <= 0 or r.seq < before_seq]

        with self._lock:
            group_records: list[ChatRecord] = []
            if in_group and group_limit:
                bucket = self._conversations.get(conversation) or ()
                group_records = earlier(bucket)[-group_limit:]
            sender_records: list[ChatRecord] = []
            if sender_id and sender_limit:
                bucket = self._senders.get((conversation, sender_id)) or ()
                shown = {r.seq for r in group_records}
                sender_records = [r for r in earlier(bucket) if r.seq not in shown][
                    -sender_limit:
                ]

        current_text = ""
        if before_seq > 0:
            with self._lock:
                for record in reversed(self._conversations.get(conversation) or ()):
                    if record.seq == before_seq:
                        current_text = record.text
                        break

        sections: list[str] = []
        if group_records:
            sections.append(f"[群聊最近 {len(group_records)} 条消息，按时间顺序]")
            sections.extend(self._format(r, sender_id) for r in group_records)
        if sender_records:
            title = "发送者更早的消息" if in_group else "发送者最近的消息"
            sections.append(f"[{title}（发送者：{sender_name}）]")
            sections.extend(self._format(r, sender_id) for r in sender_records)
        if current_text and current_text != "[图片]":
            sections.append(f"[图片所在消息，发送者：{sender_name}]")
            sections.append(current_text)
        if not sections:
            return ""
        return "<chat_context>\n" + "\n".join(sections) + "\n</chat_context>"

    @staticmethod
    def _format(record: ChatRecord, image_sender_id: str) -> str:
        if record.is_bot:
            name = f"{record.sender_name}（机器人）"
        elif image_sender_id and record.sender_id == image_sender_id:
            name = f"{record.sender_name}（图片发送者）"
        else:
            name = record.sender_name
        return f"{name}: {record.text}"
