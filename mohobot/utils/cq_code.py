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