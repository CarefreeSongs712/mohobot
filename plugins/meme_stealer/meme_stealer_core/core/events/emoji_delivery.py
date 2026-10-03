"""表情包投递：把库里的图片按 OneBot 图片段发出。"""

import asyncio
import base64
import os
from typing import Any

from ...host import StealerEvent, logger


def _read_base64(file_path: str) -> str:
    with open(file_path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


async def send_qq_image_as_sticker(
    event: StealerEvent,
    file_path: str,
    summary: str = "[动画表情]",
    plugin: Any = None,
) -> str | None:
    """发送一张表情；返回发送方式（``qq_sticker`` / ``image``），失败返回 None。

    图片一律以 ``base64://`` 内嵌，协议端（NapCat 等）无需访问插件目录。
    ``send_meme_as_qq_sticker`` 开启时设置 ``sub_type=1``，NapCat 会按
    自定义表情展示；``send_meme_as_gif`` 开启时先统一转成 GIF。
    """
    if not file_path or not os.path.isfile(file_path):
        return None
    try:
        encoded = ""
        if plugin is not None and getattr(plugin, "send_meme_as_gif", False):
            renderer = getattr(plugin, "image_render_service", None)
            if renderer is not None:
                encoded = await renderer.file_to_gif_base64(file_path) or ""
        if not encoded:
            encoded = await asyncio.to_thread(_read_base64, file_path)
    except Exception as e:
        logger.warning(f"[Stealer] 读取表情文件失败 {file_path}: {e}")
        return None

    as_sticker = bool(getattr(plugin, "send_meme_as_qq_sticker", True)) if plugin else True
    data: dict[str, Any] = {"file": f"base64://{encoded}"}
    if as_sticker:
        # NapCat: 0 为普通图片，1 为自定义表情；summary 是消息列表里显示的摘要。
        data["sub_type"] = 1
        data["summary"] = summary
    try:
        sent = await event.send_segments([{"type": "image", "data": data}])
    except Exception as e:
        logger.warning(f"[Stealer] 发送表情失败: {e}")
        return None
    if not sent:
        return None
    return "qq_sticker" if as_sticker else "image"
