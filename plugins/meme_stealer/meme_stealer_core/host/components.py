"""OneBot v11 消息段与插件内部消息组件之间的转换。

核心代码按「组件链」处理消息（Plain / Image），发送时再转换回 OneBot
数组格式的消息段，由 mohobot 的 WSServer 发出。
"""

from __future__ import annotations

import base64
import binascii
import os
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .log import logger

ImageResolver = Callable[["Image"], Awaitable[str]]


def decode_inline_image(ref: str) -> bytes | None:
    """解码 ``base64://...`` 或 ``data:image/...;base64,...``；其他形式返回 None。"""
    value = str(ref or "").strip()
    if value.startswith("base64://"):
        payload = value[len("base64://"):]
    elif value.startswith("data:") and ";base64," in value[:100]:
        payload = value.split(",", 1)[1]
    else:
        return None
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


def is_inline_image(ref: str) -> bool:
    value = str(ref or "")
    return value.startswith("base64://") or (value.startswith("data:") and ";base64," in value[:100])


def sniff_image_ext(content: bytes) -> str:
    if content[:6] in (b"GIF89a", b"GIF87a"):
        return ".gif"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[:2] == b"BM":
        return ".bmp"
    return ".jpg"


class Plain:
    """纯文本段。"""

    type = "Plain"

    def __init__(self, text: str = "", **_: Any) -> None:
        self.text = str(text or "")

    def toDict(self) -> dict[str, Any]:
        return {"type": "text", "data": {"text": self.text}}

    def __repr__(self) -> str:
        return f"Plain({self.text[:40]!r})"


class Image:
    """图片段。

    ``file``/``url``/``sub_type``/``summary`` 直接取自 OneBot image 段。
    NapCat 的群图有时只有 ``file`` 文件名、没有可下载的 ``url``，此时由
    ``resolver``（OneBot ``get_image``）换成 data URI。
    """

    type = "Image"

    def __init__(
        self,
        file: str = "",
        url: str = "",
        path: str = "",
        *,
        sub_type: Any = None,
        summary: str = "",
        data: dict[str, Any] | None = None,
        resolver: ImageResolver | None = None,
        **_: Any,
    ) -> None:
        self.file = str(file or "")
        self.url = str(url or "")
        self.path = str(path or "")
        self.subType = sub_type
        self.sub_type = sub_type
        self.summary = str(summary or "")
        self.file_unique = ""
        self._data = dict(data or {})
        self._resolver = resolver

    @classmethod
    def fromBase64(cls, b64: str) -> "Image":
        return cls(file=f"base64://{b64}")

    @classmethod
    def fromFileSystem(cls, path: str) -> "Image":
        return cls(file=str(path), path=str(path))

    @classmethod
    def fromURL(cls, url: str) -> "Image":
        return cls(file=str(url), url=str(url))

    @classmethod
    def from_segment(cls, data: dict[str, Any], resolver: ImageResolver | None = None) -> "Image":
        data = data if isinstance(data, dict) else {}
        url = str(data.get("url", "") or "")
        path = str(data.get("path", "") or "")
        return cls(
            file=str(data.get("file", "") or ""),
            url=url,
            path=path,
            sub_type=data.get("sub_type", data.get("subType")),
            summary=str(data.get("summary", "") or ""),
            data=data,
            resolver=resolver,
        )

    def toDict(self) -> dict[str, Any]:
        data = dict(self._data)
        data.setdefault("file", self.file)
        if self.url:
            data["url"] = self.url
        if self.sub_type is not None:
            data["sub_type"] = self.sub_type
        if self.summary:
            data["summary"] = self.summary
        return {"type": "image", "data": data}

    def has_fetchable_ref(self) -> bool:
        """是否已有本地文件、http(s) URL 或内嵌 base64 可直接取图。"""
        for ref in (self.path, self.file, self.url):
            if not ref:
                continue
            if ref.startswith(("http://", "https://")) or is_inline_image(ref):
                return True
            if os.path.isfile(ref):
                return True
        return False

    async def resolve_url(self) -> str:
        """没有可直接下载的引用时，经 resolver 换成 data URI 并写回 ``url``。"""
        if self.has_fetchable_ref():
            return self.url or self.path or self.file
        if self._resolver is None:
            return ""
        try:
            resolved = str(await self._resolver(self) or "")
        except Exception as e:
            logger.debug(f"[Stealer] get_image 解析图片失败: {e}")
            return ""
        if resolved:
            self.url = resolved
        return resolved

    async def convert_to_file_path(self) -> str:
        """返回本地文件路径；内嵌 base64 会落成临时文件（调用方负责清理）。"""
        for ref in (self.path, self.file):
            if ref and os.path.isfile(ref):
                return ref
        await self.resolve_url()
        for ref in (self.url, self.file):
            content = decode_inline_image(ref)
            if not content:
                continue
            fd, temp_path = tempfile.mkstemp(suffix=sniff_image_ext(content))
            try:
                os.write(fd, content)
            finally:
                os.close(fd)
            self.path = temp_path
            return temp_path
        return ""

    def __repr__(self) -> str:
        ref = self.url or self.file or self.path
        return f"Image({ref[:60]!r})"


class Segment:
    """其他 OneBot 段（at、reply、face、mface …）原样保留。"""

    def __init__(self, seg_type: str, data: dict[str, Any] | None = None) -> None:
        self.type = str(seg_type or "")
        self.data = dict(data or {})

    def toDict(self) -> dict[str, Any]:
        return {"type": self.type, "data": dict(self.data)}

    def __repr__(self) -> str:
        return f"Segment({self.type!r})"


class MessageChain(list):
    """组件列表。"""


class MessageEventResult:
    """命令处理器 ``yield`` 出的结果，链式 API 与原插件一致。"""

    def __init__(self, chain: Iterable[Any] | None = None) -> None:
        self.chain: list[Any] = list(chain or [])

    def message(self, text: str) -> "MessageEventResult":
        self.chain.append(Plain(text))
        return self

    def file_image(self, path: str) -> "MessageEventResult":
        self.chain.append(Image.fromFileSystem(path))
        return self

    def base64_image(self, b64: str) -> "MessageEventResult":
        self.chain.append(Image.fromBase64(b64))
        return self

    def url_image(self, url: str) -> "MessageEventResult":
        self.chain.append(Image.fromURL(url))
        return self

    def stop_event(self) -> "MessageEventResult":
        return self

    def get_plain_text(self) -> str:
        return "".join(comp.text for comp in self.chain if isinstance(comp, Plain))

    def has_images(self) -> bool:
        return any(isinstance(comp, Image) for comp in self.chain)


def segments_to_components(
    segments: Iterable[dict[str, Any]], resolver: ImageResolver | None = None
) -> list[Any]:
    components: list[Any] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type", "") or "")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type == "text":
            components.append(Plain(str(data.get("text", "") or "")))
        elif seg_type == "image":
            components.append(Image.from_segment(data, resolver))
        else:
            components.append(Segment(seg_type, data))
    return components


def _image_file_source(image: Image) -> str:
    """发送用的 file 字段：本地文件统一转 base64，协议端无需访问插件目录。"""
    for ref in (image.file, image.url):
        if is_inline_image(ref):
            content = decode_inline_image(ref)
            return f"base64://{base64.b64encode(content).decode()}" if content else ""
    for ref in (image.path, image.file):
        if ref and os.path.isfile(ref):
            with open(ref, "rb") as handle:
                return f"base64://{base64.b64encode(handle.read()).decode()}"
    for ref in (image.url, image.file):
        if ref.startswith(("http://", "https://")):
            return ref
    return image.file or image.url


def to_onebot_segments(content: Any) -> list[dict[str, Any]]:
    """把文本 / 组件 / 组件链 / MessageEventResult 转成 OneBot 段列表。"""
    if content is None:
        return []
    if isinstance(content, MessageEventResult):
        items: Iterable[Any] = content.chain
    elif isinstance(content, str):
        items = [Plain(content)]
    elif isinstance(content, (list, tuple)):
        items = content
    else:
        items = [content]

    segments: list[dict[str, Any]] = []
    for comp in items:
        if isinstance(comp, Plain):
            if comp.text:
                segments.append({"type": "text", "data": {"text": comp.text}})
        elif isinstance(comp, Image):
            source = _image_file_source(comp)
            if not source:
                continue
            data: dict[str, Any] = {"file": source}
            if comp.sub_type is not None:
                data["sub_type"] = comp.sub_type
            if comp.summary:
                data["summary"] = comp.summary
            segments.append({"type": "image", "data": data})
        elif isinstance(comp, Segment):
            segments.append(comp.toDict())
        elif isinstance(comp, dict) and comp.get("type"):
            segments.append(comp)
        elif isinstance(comp, str) and comp:
            segments.append({"type": "text", "data": {"text": comp}})
    return segments
