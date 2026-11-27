"""Knowledge provider ownership with real clients, SQLite and loopback HTTP."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.knowledge.providers import EmbeddingProvider, RerankProvider
from mohobot.knowledge.service import KnowledgeService
from mohobot.models.config import KnowledgeConfig
from mohobot.services.task_supervisor import TaskSupervisor


@asynccontextmanager
async def _embedding_server(started=None, release=None):
    async def embeddings(request):
        payload = await request.json()
        if started is not None:
            started.set()
        if release is not None:
            await release.wait()
        return web.json_response({
            "object": "list", "model": payload["model"],
            "data": [{"object": "embedding", "index": i, "embedding": [1.0, 0.0]}
                     for i, _ in enumerate(payload["input"])],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    async def rerank(request):
        await request.json()
        return web.json_response({"results": [{"index": 0, "relevance_score": 1.0}]})

    app = web.Application()
    app.router.add_post("/v1/embeddings", embeddings)
    app.router.add_post("/v1/rerank", rerank)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        if release is not None:
            release.set()
        await runner.cleanup()


def _closed(provider):
    client = provider._client
    return client.is_closed() if isinstance(provider, EmbeddingProvider) else client.is_closed


async def test_real_retired_clients_survive_supervisor_cancellation(tmp_path):
    async with _embedding_server() as url:
        # Cover direct service shutdown, main's owner-first ordering, and cancellation
        # before the delayed coroutine has even started.
        for case in ("direct", "owner-first", "not-started"):
            supervisor = TaskSupervisor()
            service = KnowledgeService(KnowledgeConfig(
                source_dir=str(tmp_path / case / "source"),
                index_dir=str(tmp_path / case / "index"),
                embedding_base_url=url, embedding_api_key="local-test", embedding_model="local",
            ), task_supervisor=supervisor)
            await service.startup()
            active = service._embedding
            retired = [EmbeddingProvider(url, "local-test", "local") if i % 2 == 0
                       else RerankProvider(url, "local-test", "local") for i in range(10)]
            try:
                for provider in retired:
                    if isinstance(provider, EmbeddingProvider):
                        assert await provider.embed(["local request"]) == [[1.0, 0.0]]
                    else:
                        assert await provider.rerank("local", ["document"]) == [(0, 1.0)]
                service._retire_providers(*retired)
                assert len(service._retired_tasks) == 2
                if case == "owner-first":
                    await asyncio.sleep(0)
                    await supervisor.cancel_owner("knowledge")
                elif case == "not-started":
                    await supervisor.cancel_owner("knowledge")
                assert not any(_closed(provider) for provider in retired)
                await service.close()
                await service.close()
                assert all(_closed(provider) for provider in [active, *retired])
                assert not service._retired_tasks
                assert not service._retired_providers
            finally:
                await service.close()
                await supervisor.shutdown()
                for provider in retired:
                    await provider.close()


async def test_real_inflight_client_is_retained_until_request_finishes(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    async with _embedding_server(started, release) as url:
        service = KnowledgeService(KnowledgeConfig(index_dir=str(tmp_path / "index")))
        provider = EmbeddingProvider(url, "local-test", "local")
        service._acquire_provider(provider)

        async def request():
            try:
                return await provider.embed(["held request"])
            finally:
                service._release_provider(provider)

        request_task = asyncio.create_task(request())
        close_task = None
        try:
            await asyncio.wait_for(started.wait(), 5)
            close_task = asyncio.create_task(service._close_retired_when_idle(provider, delay=0.01))
            await asyncio.sleep(0.04)
            assert not close_task.done()
            assert not _closed(provider)
            release.set()
            assert await asyncio.wait_for(request_task, 5) == [[1.0, 0.0]]
            assert await asyncio.wait_for(close_task, 5)
            assert _closed(provider)
            assert not service._provider_inflight
        finally:
            release.set()
            await asyncio.gather(request_task, return_exceptions=True)
            if close_task is not None:
                await asyncio.gather(close_task, return_exceptions=True)
            await provider.close()
            await service.close()
