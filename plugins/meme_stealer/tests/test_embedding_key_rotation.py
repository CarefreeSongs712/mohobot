"""凭据热更新须换代 provider，同时保留同一向量空间的已有数据。"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from aiohttp import web

from meme_stealer_core.app import StealerApp


PLUGIN_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("in_flight", [False, True])
async def test_embedding_key_rotation_through_plugin_config_hook(tmp_path, in_flight):
    accepted_key = "test-old-key"
    requests = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def embeddings(request):
        body = await request.json()
        authorization = request.headers.get("Authorization", "")
        requests.append((body["input"], authorization))
        authorized = authorization == f"Bearer {accepted_key}"
        if body["input"] == "在途查询" and authorization == "Bearer test-old-key":
            started.set()
            await release.wait()
        if not authorized:
            return web.json_response({"error": {"message": "revoked test key"}}, status=401)
        return web.json_response({
            "object": "list",
            "model": body["model"],
            "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}],
        })

    server = web.Application()
    server.router.add_post("/v1/embeddings", embeddings)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    app = StealerApp({
        "embedding_model": "test-embed",
        "embedding_base_url": f"http://127.0.0.1:{port}/v1",
        "embedding_api_key": accepted_key,
        "enable_embedding_search": True,
    }, tmp_path / "data", plugin_dir=PLUGIN_DIR)
    spec = importlib.util.spec_from_file_location("meme_key_rotation_entry", PLUGIN_DIR / "main.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    plugin = entry.Plugin()
    plugin.app = app
    service = app.meme_selector.embedding_service
    pending = None
    try:
        await service.initialize()
        path = str(tmp_path / "test.png")
        record = {"path": path, "desc": "开心的测试表情"}
        assert await app.db_service.insert_batch([record]) == 1
        assert await service.insert_emoji(path, record)
        assert [p for p, _ in await service.search("开心")] == [path]
        stored_before = app.db_service.load_embeddings_by_sig("fallback")
        provider_before = service._provider
        generation_before = service._generation
        if in_flight:
            pending = asyncio.create_task(service.search("在途查询"))
            await asyncio.wait_for(started.wait(), 2)

        accepted_key = "test-new-key"
        plugin.on_config_update({"embedding_api_key": accepted_key})
        await asyncio.wait_for(app._embedding_task, 5)

        # 凭据不是向量空间身份：轮换不能删除或重新计算已有向量。
        assert app.db_service.load_embeddings_by_sig("fallback") == stored_before
        assert service._provider is not provider_before
        assert service._generation > generation_before
        assert provider_before.endpoint.api_key == "test-old-key"
        assert service._provider.endpoint.api_key == accepted_key
        if pending is not None:
            release.set()
            assert [p for p, _ in await asyncio.wait_for(pending, 5)] == [path]
            assert ("在途查询", "Bearer test-new-key") in requests

        boundary = len(requests)
        assert [p for p, _ in await service.search("开心")] == [path]
        assert await service.insert_emoji(path, record)
        assert requests[boundary:]
        assert all(auth == "Bearer test-new-key" for _, auth in requests[boundary:])
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await app.terminate()
        await runner.cleanup()
