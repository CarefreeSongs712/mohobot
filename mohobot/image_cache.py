"""Image caching pipeline — download, phash dedup, LRU eviction.

Flow:
  1. On receiving an image message, check cache_map.json for hash match
  2. If phash match found (Hamming distance < 5), reuse cached description
  3. If miss, download image, compute phash, call vision model, store in cache

Cache size limit: 300 MB (LRU eviction).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import aiofiles
import aiofiles.os
import httpx
from loguru import logger

from mohobot.file_store import json_read, json_update, json_write

try:
    from PIL import Image
    import io
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from imagehash import phash as compute_phash
    HAS_IMAGEHASH = True
except ImportError:
    HAS_IMAGEHASH = False


class ImageCache:
    """Image cache with phash-based dedup and LRU eviction (300 MB limit)."""

    def __init__(self, cache_dir: str = "./data/cache"):
        self._images_dir = Path(cache_dir) / "images"
        self._map_path = Path(cache_dir) / "image_cache_map.json"
        self._max_size = 300 * 1024 * 1024  # 300 MB
        self._http_client: httpx.AsyncClient | None = None
        self._images_dir.mkdir(parents=True, exist_ok=True)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=5),
            )
        return self._http_client

    async def get_or_describe(
        self, image_url: str, vision_callback=None
    ) -> tuple[str, str]:
        """Get image description — either from cache or by downloading + vision.

        Returns:
            (local_path, description_text)
        """
        # Check cache map
        cache_map = await self._load_cache_map()
        cached = cache_map.get(image_url)
        if cached and cached.get("path") and cached.get("description"):
            local_path = cached["path"]
            # Verify file still exists
            if await aiofiles.os.path.exists(local_path):
                logger.debug(f"Image cache hit (URL): {image_url}")
                return local_path, cached["description"]

        # Need to download
        local_path = await self._download_image(image_url)
        if not local_path:
            return "", "[图片下载失败]"

        # Compute phash
        phash_val = ""
        if HAS_PIL and HAS_IMAGEHASH:
            try:
                async with aiofiles.open(local_path, "rb") as f:
                    data = await f.read()
                img = Image.open(io.BytesIO(data))
                phash_val = str(compute_phash(img))
            except Exception as e:
                logger.warning(f"phash computation failed: {e}")

        # Check phash against existing entries
        description = None
        if phash_val:
            for url, entry in cache_map.items():
                if entry.get("phash") and self._hamming_distance(
                    phash_val, entry["phash"]
                ) < 5:
                    description = entry.get("description", "")
                    logger.debug(f"Image cache hit (phash): {phash_val}")
                    break

        # If no phash hit and vision callback provided, call vision model
        if description is None and vision_callback:
            description = await vision_callback(image_url, local_path)
        elif description is None:
            description = "[图片]"

        # Update cache map (原子合并: 6 bot 并发描述不同图时不丢彼此的条目)
        entry = {
            "path": str(local_path),
            "phash": phash_val,
            "description": description or "[图片]",
            "cached_at": time.time(),
            "size": await self._file_size(local_path),
        }
        cache_map = await json_update(
            self._map_path,
            lambda cur: {**(cur if isinstance(cur, dict) else {}), image_url: entry},
            default={},
        )

        # Enforce LRU eviction
        await self._evict_if_needed(cache_map)

        return str(local_path), description or "[图片]"

    async def _download_image(self, url: str) -> str | None:
        """Download an image to the cache directory.

        支持 data URI(data:image/...;base64,...)与 http(s) URL。
        """
        if url.startswith("data:"):
            # data URI: base64 直接解码落盘(群聊图片经 get_image 归一化而来)
            try:
                import base64 as _base64
                header, _, b64 = url.partition(",")
                mime = header[5:].split(";")[0] if ";" in header else "image/jpeg"
                content = _base64.b64decode(b64)
            except Exception as e:
                logger.warning(f"data URI 解码失败: {e}")
                return None
            ext = self._ext_from_content_type(mime) or ".jpg"
            import hashlib
            url_hash = hashlib.md5(url.encode()).hexdigest()
            filepath = self._images_dir / f"{url_hash}{ext}"
            async with aiofiles.open(filepath, "wb") as f:
                await f.write(content)
            logger.debug(f"Image decoded from data URI → {filepath} ({len(content)} bytes)")
            return str(filepath)

        client = await self._get_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content
        except Exception as e:
            logger.warning(f"Image download failed: {url} — {e}")
            return None

        # Determine extension from content-type or URL
        content_type = response.headers.get("content-type", "")
        ext = self._ext_from_content_type(content_type) or ".jpg"

        # Generate filename from URL hash
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()
        filename = f"{url_hash}{ext}"
        filepath = self._images_dir / filename

        async with aiofiles.open(filepath, "wb") as f:
            await f.write(content)

        logger.debug(f"Image downloaded: {url} → {filepath} ({len(content)} bytes)")
        return str(filepath)

    async def _load_cache_map(self) -> dict[str, Any]:
        """Load the image cache map JSON."""
        data = await json_read(self._map_path)
        if data is None or not isinstance(data, dict):
            return {}
        return data

    async def _save_cache_map(self, data: dict) -> None:
        """Save the image cache map JSON."""
        await json_write(self._map_path, data)

    async def _evict_if_needed(self, cache_map: dict) -> None:
        """LRU eviction: remove oldest entries until under 300 MB."""
        total_size = sum(
            entry.get("size", 0) for entry in cache_map.values()
        )
        if total_size <= self._max_size:
            return

        # Sort by cached_at (oldest first)
        sorted_entries = sorted(
            cache_map.items(), key=lambda x: x[1].get("cached_at", 0)
        )

        freed = 0
        removed = []
        for url, entry in sorted_entries:
            if total_size - freed <= self._max_size:
                break
            # Delete local file
            filepath = entry.get("path", "")
            if filepath and await aiofiles.os.path.exists(filepath):
                try:
                    os.remove(filepath)
                    freed += entry.get("size", 0)
                    removed.append(url)
                except OSError as e:
                    logger.warning(f"Failed to remove cached image: {e}")

        # Remove from map (用磁盘最新合并: 并发更新的条目不丢)
        if removed:
            logger.info(
                f"LRU eviction: removed {len(removed)} images, "
                f"freed {freed // 1024} KB"
            )
            cache_map = await json_update(
                self._map_path,
                lambda cur: {
                    k: v for k, v in (cur if isinstance(cur, dict) else {}).items()
                    if k not in removed
                },
                default={},
            )

    @staticmethod
    def _hamming_distance(h1: str, h2: str) -> int:
        """Compute Hamming distance between two phash hex strings."""
        if len(h1) != len(h2):
            return 999
        return sum(bin(int(a, 16) ^ int(b, 16)).count("1") for a, b in zip(h1, h2))

    @staticmethod
    def _ext_from_content_type(content_type: str) -> str | None:
        mapping = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }
        return mapping.get(content_type.split(";")[0].strip())

    async def _file_size(self, filepath: str) -> int:
        try:
            stat = await aiofiles.os.stat(filepath)
            return stat.st_size
        except OSError:
            return 0

    async def close(self) -> None:
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None