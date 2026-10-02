"""消息事件的容错读取工具。"""

from typing import Any


def normalize_event_value(value: object) -> str:
    """把适配器返回的标量安全转换为字符串。"""
    if value is None:
        return ""
    try:
        normalized = str(value).strip()
    except Exception:
        return ""
    if normalized.startswith("`") and normalized.endswith("`") and len(normalized) >= 2:
        normalized = normalized[1:-1].strip()
    return normalized


def get_event_session_key(event: Any | None, *, default: str = "global") -> str:
    """读取稳定会话键，供冷却、后台任务和强制捕获窗口共用。"""
    if event is None:
        return default

    # StealerEvent.unified_msg_origin：群聊按群号（多个 bot 共享），私聊按 bot + 用户。
    try:
        unified_msg_origin = normalize_event_value(
            getattr(event, "unified_msg_origin", "")
        )
    except Exception:
        unified_msg_origin = ""
    if unified_msg_origin:
        return unified_msg_origin

    getter = getattr(event, "get_session_id", None)
    if callable(getter):
        try:
            session_id = normalize_event_value(getter())
        except Exception:
            session_id = ""
        if session_id:
            return session_id
    return default


def get_event_bot_session_key(event: Any | None, *, default: str = "global") -> str:
    """bot 维度的会话键：同群多个 bot 各自的发送冷却与待发表情互不干扰。"""
    session = get_event_session_key(event, default=default)
    bot_id = normalize_event_value(getattr(event, "bot_id", "")) if event is not None else ""
    if not bot_id or session.startswith(f"{bot_id}:"):
        return session
    return f"{bot_id}:{session}"


def unwrap_event(event: Any) -> Any:
    """兼容旧调用点：mohobot 的工具直接拿到消息事件，无需解包。"""
    return event
