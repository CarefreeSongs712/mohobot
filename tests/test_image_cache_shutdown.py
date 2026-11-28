"""Image timeout fallback and shared-task shutdown regression tests."""

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.file_store import json_write
from mohobot.image_cache import ImageCache
from mohobot.llm_service import LLMService
from mohobot.models.config import GlobalConfig
from mohobot.models.onebot import Event


@asynccontextmanager
async def _slow_image():
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def image(request):
        requests.append(request.path)
        started.set()
        await release.wait()
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0z8AAAAASUVORK5CYII="
        )
        return web.Response(body=png, content_type="image/png")

    app = web.Application()
    app.router.add_get("/image", image)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/image", started, release, requests
    finally:
        release.set()
        await runner.cleanup()


async def _timeout_prompt(cached):
    with tempfile.TemporaryDirectory() as td:
        cache = ImageCache(td)
        async with _slow_image() as (url, started, release, requests):
            service = LLMService.__new__(LLMService)
            service._cfg = GlobalConfig()
            service._image_cache = cache
            service._vision_available = True
            service._song_annotator = None

            async def describe(*args):
                return "late description"

            service._vision_callback = lambda config: describe
            if cached:
                # A description without a usable local image still takes the download path.
                await json_write(cache._map_path, {url: {"description": cached}})
            event = Event.from_dict({
                "post_type": "message", "message_type": "private", "time": 1,
                "self_id": 101, "user_id": 202, "message_id": 1,
                "sender": {"user_id": 202, "nickname": "local"},
                "message": [{"type": "image", "data": {"url": url}}],
            })
            try:
                with patch("mohobot.llm_service._IMAGE_DESCRIBE_TIMEOUT", 0.05):
                    messages = await service._build_messages(
                        "bot_001", event, [], persona_content="local test",
                    )
                assert messages[-1]["content"] == (
                    f"[图片]（{cached}）" if cached else "（用户发送了图片）"
                )
                assert "coroutine object" not in str(messages)
            finally:
                release.set()
                tasks = list(cache._in_flight.values())
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await cache.close()


async def test_image_timeout_uses_text_placeholder():
    await _timeout_prompt("")


async def test_image_timeout_awaits_cached_description():
    await _timeout_prompt("cached cat")


async def test_close_drains_shared_task_after_first_waiter_cancelled():
    with tempfile.TemporaryDirectory() as td:
        cache = ImageCache(td)
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_load = cache._load_cache_map

        async def held_load():
            entered.set()
            try:
                await release.wait()
                return await original_load()
            finally:
                cancelled.set()

        cache._load_cache_map = held_load
        async with _slow_image() as (url, started, http_release, requests):
            leader = asyncio.create_task(cache.get_or_describe(url))
            follower = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                follower = asyncio.create_task(cache.get_or_describe(url))
                await asyncio.sleep(0)
                leader.cancel()
                with suppress(asyncio.CancelledError):
                    await leader
                tasks = list(cache._in_flight.values())
                await cache.close()
                assert all(task.done() for task in tasks), "close left shared work alive"
                assert cancelled.is_set()
                assert cache._in_flight == {}
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(follower, 2)
                release.set()
                http_release.set()
                await asyncio.sleep(0)
                assert requests == []
                assert list(cache._images_dir.iterdir()) == []
                assert not cache._map_path.exists()
                assert cache._http_client is None
                try:
                    await cache._get_client()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("closed cache recreated its HTTP client")
                try:
                    await cache.get_or_describe(url)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("closed cache accepted another image task")
            finally:
                release.set()
                http_release.set()
                for task in list(cache._in_flight.values()):
                    task.cancel()
                await asyncio.gather(
                    leader, *([follower] if follower is not None else []),
                    *list(cache._in_flight.values()), return_exceptions=True,
                )
                await cache.close()


async def test_close_cancels_active_http_and_closes_existing_client():
    with tempfile.TemporaryDirectory() as td:
        cache = ImageCache(td)
        async with _slow_image() as (url, started, release, requests):
            waiter = asyncio.create_task(cache.get_or_describe(url))
            try:
                await asyncio.wait_for(started.wait(), 2)
                client = cache._http_client
                tasks = list(cache._in_flight.values())
                await cache.close()
                assert all(task.done() for task in tasks)
                assert client.is_closed
                assert cache._http_client is None
                assert cache._in_flight == {}
                assert len(requests) == 1
                assert list(cache._images_dir.iterdir()) == []
                await cache.close()
            finally:
                release.set()
                for task in list(cache._in_flight.values()):
                    task.cancel()
                await asyncio.gather(waiter, *list(cache._in_flight.values()), return_exceptions=True)
                await cache.close()
