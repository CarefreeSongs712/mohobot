"""CQ code parser — converts between CQ string format and message segment arrays.

Supports both directions:
  - CQ string → list[dict]  (parse_cq_code)
  - list[dict] → CQ string  (build_cq_code)
"""

import re
from typing import Any

# Regex for CQ code: [CQ:type,key1=val1,key2=val2]
# Handles escaped characters: &#44; (,) &#91; ([) &#93; (]) &amp; (&)
_CQ_CODE_RE = re.compile(r"\[CQ:([a-zA-Z0-9_]+)((?:,[^\[\]]*)?)\]")

# Escape mapping for CQ code values
_CQ_ESCAPE_TO_CHAR = {
    "&amp;": "&",
    "&#44;": ",",
    "&#91;": "[",
    "&#93;": "]",
}

_CQ_CHAR_TO_ESCAPE = {v: k for k, v in _CQ_ESCAPE_TO_CHAR.items()}


def _unescape(text: str) -> str:
    """Unescape CQ-encoded text."""
    for escaped, char in _CQ_ESCAPE_TO_CHAR.items():
        text = text.replace(escaped, char)
    return text


def _escape(text: str) -> str:
    """Escape text for CQ code values."""
    for char, escaped in _CQ_CHAR_TO_ESCAPE.items():
        text = text.replace(char, escaped)
    return text


def parse_cq_code(message_str: str) -> list[dict[str, Any]]:
    """Parse a CQ-code string into a list of message segment dicts.

    Pure text (not inside [CQ:...]) becomes a 'text' segment.
    """
    segments: list[dict[str, Any]] = []
    last_end = 0

    for match in _CQ_CODE_RE.finditer(message_str):
        start = match.start()

        # Text before this CQ code
        if start > last_end:
            text = _unescape(message_str[last_end:start])
            if text:
                segments.append({"type": "text", "data": {"text": text}})

        cq_type = match.group(1)
        params_str = match.group(2)

        data: dict[str, str] = {}
        if params_str:
            # Split params by comma, respecting that values may contain commas
            # Each param is key=value
            for param in params_str.lstrip(",").split(","):
                if "=" in param:
                    key, _, value = param.partition("=")
                    data[key.strip()] = _unescape(value.strip())

        segments.append({"type": cq_type, "data": data})
        last_end = match.end()

    # Remaining text
    if last_end < len(message_str):
        text = _unescape(message_str[last_end:])
        if text:
            segments.append({"type": "text", "data": {"text": text}})

    return segments


def build_cq_code(segments: list[dict[str, Any]]) -> str:
    """Build a CQ-code string from a list of message segment dicts.

    Each segment is in OneBot array format: {"type": "...", "data": {...}}.
    """
    parts: list[str] = []

    for seg in segments:
        seg_type = seg.get("type", "text")
        data = seg.get("data", {})

        if seg_type == "text":
            parts.append(data.get("text", ""))
        elif seg_type == "reply":
            # Reply is often prepended, format as CQ code
            params = ",".join(f"{k}={_escape(str(v))}" for k, v in data.items() if v is not None)
            parts.append(f"[CQ:reply,{params}]")
        else:
            params = ",".join(f"{k}={_escape(str(v))}" for k, v in data.items() if v is not None)
            if params:
                parts.append(f"[CQ:{seg_type},{params}]")
            else:
                parts.append(f"[CQ:{seg_type}]")

    return "".join(parts)


def extract_plain_text(message: str | list[dict[str, Any]]) -> str:
    """Extract plain text from a message (string or array format)."""
    if isinstance(message, str):
        segments = parse_cq_code(message)
    else:
        segments = message

    text_parts: list[str] = []
    for seg in segments:
        if seg.get("type") == "text":
            text_parts.append(seg.get("data", {}).get("text", ""))
    return "".join(text_parts).strip()


def has_meaningful_text(text: str) -> bool:
    """文本是否含可被模型理解的内容(字母/数字/汉字等), 而非纯空白/标点/表情。

    上游网关会以 400 拒绝"内容为空或只有空白/标点"的请求, 因此发请求前
    必须区分"有实质内容"与"只有符号"。Unicode 类别 L(字母)、N(数字)、
    S(符号中的 emoji/货币等)中, 只有 L/N 视为有实质语义; 纯 emoji 串
    (如 👍👀)属于 So, 对文本模型同样没有可回应内容, 归为无实质内容。
    """
    import unicodedata

    for ch in text or "":
        if unicodedata.category(ch)[0] in ("L", "N"):
            return True
    return False


def extract_at_targets(message: str | list[dict[str, Any]]) -> list[str]:
    """返回消息里被 @ 的 QQ 号列表(按出现顺序, 去重)。

    纯字符串消息会先按 CQ 码解析; `at` 段的 ``data.qq`` 为 "all" 时跳过
    (@全体不是对某个 bot 的点名)。无法解析的段忽略, 不抛异常。
    """
    if isinstance(message, str):
        try:
            segments = parse_cq_code(message)
        except Exception:  # noqa: BLE001 - 畸形 CQ 码不应中断主链
            return []
    elif isinstance(message, list):
        segments = message
    else:
        return []

    targets: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict) or seg.get("type") != "at":
            continue
        qq = str((seg.get("data") or {}).get("qq") or "").strip()
        if not qq or qq == "all" or qq in targets:
            continue
        targets.append(qq)
    return targets


def extract_image_urls(message: str | list[dict[str, Any]]) -> list[str]:
    """Extract usable image references from a message.

    Supports:
      - data["url"] (OneBot 标准字段, NapCat 等通常有值)
      - data["file"] 以 "base64://" 开头 (部分实现不发 url,只给 base64)
    返回可直接传给视觉模型的内容引用 (http url 或 data URI)。
    """
    import base64 as _base64

    if isinstance(message, str):
        segments = parse_cq_code(message)
    else:
        segments = message

    urls: list[str] = []
    for seg in segments:
        if seg.get("type") != "image":
            continue
        data = seg.get("data", {}) or {}
        url = str(data.get("url", "") or "").strip()
        if url:
            urls.append(url)
            continue
        file_ref = str(data.get("file", "") or "")
        if file_ref.startswith("base64://"):
            b64 = file_ref[len("base64://"):]
            mime = "image/jpeg"
            try:
                head = _base64.b64decode(b64[:64])
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
            urls.append(f"data:{mime};base64,{b64}")
    return urls


def empty_message_placeholder(chat_type: str, message: str | list[dict[str, Any]]) -> str:
    """空白/仅@消息的提示性标注(替代空 content, 避免 LLM 收到空 user 消息)。

    chat_type: "group" / "private"。message 为原始消息段(或字符串)。
    有非文本段时(表情/语音/视频等)按类型说明, 否则视为纯 @ 或真空消息。
    """
    if isinstance(message, str):
        segments = parse_cq_code(message) if message else []
    elif isinstance(message, list):
        segments = message
    else:
        segments = []
    non_text = [
        seg.get("type") for seg in segments
        if isinstance(seg, dict)
        and seg.get("type") not in ("text", "reply", "at")
    ]
    if "face" in non_text:
        return "（用户只发送了表情，没有附加文字）"
    if "record" in non_text:
        return "（用户只发送了语音，没有附加文字）"
    if "video" in non_text:
        return "（用户只发送了视频，没有附加文字）"
    if "json" in non_text:
        return "（用户只发送了卡片/结构化消息，没有文字）"
    if "forward" in non_text:
        return "（用户转发了合并转发消息，没有附加文字）"
    if "file" in non_text:
        return "（用户只发送了一个文件，没有附加文字）"
    if non_text:
        return f"（用户只发送了非文本内容（{'/'.join(non_text)}），没有附带文字）"
    if chat_type == "group":
        return "（该用户在群里 @ 了机器人，但未附带任何文字内容）"
    return "（用户发送了一条空白消息，未包含文字或附件）"


def symbol_only_placeholder(chat_type: str, text: str) -> str:
    """纯符号消息(标点/emoji, 无字母数字)的标注文本。

    上游网关会拒绝"只有空白/标点"的请求; 这类消息本身也没有可回应的语义,
    用一条说明替代原文, 既让请求合法, 也让模型知道用户只发了符号/表情。
    """
    stripped = (text or "").strip()
    if not stripped:
        return empty_message_placeholder(chat_type, "")
    return f"（用户只发送了表情/符号，没有文字内容：{stripped}）"
