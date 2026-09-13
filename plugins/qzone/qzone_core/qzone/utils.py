"""图片下载与归一化（移植自 astrbot_plugin_qzone_lite core/qzone/utils.py）。

含 SSRF 防护(拒绝内网/环回地址)与 base64:// data:image/ 解码。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from pathlib import Path
from typing import Sequence, Union
from urllib.parse import urlparse

import aiohttp
from loguru import logger

BytesOrStr = Union[str, bytes]


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _check_image_url_safety(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "invalid_scheme"

    host = (parsed.hostname or "").strip().lower()
    if not host or host == "localhost":
        return False, "invalid_host"

    try:
        ip_literal = ipaddress.ip_address(host)
        if _is_blocked_ip(ip_literal):
            return False, "blocked_ip_literal"
        return True, "ok"
    except ValueError:
        pass

    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
        )
        ips = {
            ipaddress.ip_address(info[4][0])
            for info in infos
            if info and len(info) > 4 and info[4]
        }
    except Exception as e:
        logger.warning(f"图片 URL DNS 解析失败 host={host}: {type(e).__name__}")
        return False, "dns_resolution_failed"

    for ip in ips:
        if _is_blocked_ip(ip):
            return False, "blocked_resolved_ip"
    return True, "ok"


async def download_file(url: str) -> bytes | None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    ok, reason = await _check_image_url_safety(url)
    if not ok:
        logger.warning(
            f"拒绝下载不安全图片 URL host: {host}, scheme: {parsed.scheme}, reason: {reason}"
        )
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(url) as response:
                response.raise_for_status()
                return await response.read()
    except Exception as e:
        logger.error(f"图片下载失败 host={host}: {type(e).__name__}")
        return None


def _decode_base64_image(data: str) -> bytes | None:
    payload = ""
    if data.startswith("base64://"):
        payload = data[len("base64://"):].strip()
    elif data.startswith("data:image/") and ";base64," in data:
        payload = data.split(";base64,", 1)[1].strip()
    if not payload:
        return None
    try:
        payload += "=" * (-len(payload) % 4)
        return base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return None


async def normalize_images(images: Sequence[BytesOrStr] | None) -> list[bytes]:
    if images is None:
        return []
    cleaned: list[bytes] = []
    for item in images:
        if isinstance(item, bytes):
            cleaned.append(item)
        elif isinstance(item, str):
            image_bytes = _decode_base64_image(item)
            if image_bytes is not None and len(image_bytes) > 0:
                cleaned.append(image_bytes)
                continue
            local_path = Path(item)
            if local_path.is_absolute():
                try:
                    file = await asyncio.to_thread(local_path.read_bytes)
                except (OSError, ValueError) as e:
                    logger.error(
                        f"Failed to read local image name={local_path.name}: "
                        f"{type(e).__name__}"
                    )
                else:
                    if file:
                        cleaned.append(file)
                continue
            file = await download_file(item)
            if file is not None:
                cleaned.append(file)
        else:
            raise TypeError(f"image 必须是 str 或 bytes，收到 {type(item)}")
    return cleaned
