"""旧索引缺少向量空间身份时，必须先重嵌再发布当前 provider 身份。"""

import asyncio
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.knowledge.service import KnowledgeService
from mohobot.models.config import KnowledgeConfig
from mohobot.services.task_supervisor import TaskSupervisor


class _Embedding:
    def __init__(self, base_url, model="shared-model", api_key="test-key"):
        self.model, self.base_url, self.api_key = model, base_url, api_key
        self.calls = 0
        self.fail = False

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("test embedding unavailable")
        vector = [0.0, 1.0] if "b.example" in self.base_url else [1.0, 0.0]
        return [list(vector) for _ in texts]

    async def close(self):
        pass


def _config(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.md").write_text("# Entry\n## Facts\nStable source text.\n", encoding="utf-8")
    return KnowledgeConfig(
        enabled=True, source_dir=str(source), index_dir=str(tmp_path / "index.db"),
        embedding_model="shared-model", embedding_base_url="https://a.example/v1",
        embedding_api_key="test-key", rerank_model="",
    )


def _remove_fingerprint(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DELETE FROM meta WHERE key='embedding_fingerprint'")
        connection.commit()


async def _legacy_index(tmp_path):
    config = _config(tmp_path)
    service = KnowledgeService(config, embedding_provider=_Embedding(config.embedding_base_url))
    try:
        await service.startup()
        assert (await service.reindex())["embedded_chunks"] == 1
        states = service._store.list_file_states()
    finally:
        await service.close()
    _remove_fingerprint(config.index_dir)
    return config, states


def _vectors(service):
    return [np.frombuffer(row["embedding"], dtype=np.float32).tolist()
            for row in service._store.load_chunks()]


async def test_legacy_index_reembeds_unchanged_source_for_same_model_new_endpoint(tmp_path):
    config, states = await _legacy_index(tmp_path)
    config = replace(config, embedding_base_url="https://b.example/v1")
    provider = _Embedding(config.embedding_base_url)
    service = KnowledgeService(config, embedding_provider=provider)
    try:
        await service.startup()
        assert service._store.get_meta("embedding_fingerprint") is None
        assert service._store.list_file_states() == states
        stats = await service.reindex()
        assert stats["reembedded_files"] == 1
        assert stats["embedded_chunks"] == 1 and provider.calls == 1
        assert _vectors(service) == [[0.0, 1.0]]
        assert service._store.get_meta("embedding_fingerprint") == "shared-model@https://b.example/v1"
        assert service._store.list_file_states() == states
        repeat = await service.reindex()
        assert repeat["reembedded_files"] == 0 and provider.calls == 1
        result = await service.search("stable facts")
        assert result["results"][0]["score"] == 1.0
    finally:
        await service.close()


async def test_failed_legacy_reembedding_does_not_publish_new_identity(tmp_path):
    config, _ = await _legacy_index(tmp_path)
    config = replace(config, embedding_base_url="https://b.example/v1")
    provider = _Embedding(config.embedding_base_url)
    provider.fail = True
    service = KnowledgeService(config, embedding_provider=provider)
    try:
        await service.startup()
        stats = await service.reindex()
        assert stats.get("error") == "test embedding unavailable"
        assert service._store.get_meta("embedding_fingerprint") is None
        assert _vectors(service) == [[1.0, 0.0]]
        assert not service._state["ready"]
        assert "error" in await service.search("stable facts")
        provider.fail = False
        retry = await service.reindex()
        assert retry["embedded_chunks"] == 1
        assert _vectors(service) == [[0.0, 1.0]]
        assert service._store.get_meta("embedding_fingerprint") == "shared-model@https://b.example/v1"
    finally:
        await service.close()


async def test_known_identity_key_rotation_reuses_vectors_and_legacy_sync_rebuilds(tmp_path):
    import mohobot.knowledge.service as service_module

    original_provider = service_module.EmbeddingProvider
    service_module.EmbeddingProvider = _Embedding
    supervisor = TaskSupervisor()
    config = _config(tmp_path)
    service = KnowledgeService(config, task_supervisor=supervisor)
    try:
        await service.startup()
        await service.reindex()
        first_provider = service._embedding
        first_stats = service._state["last_stats"]
        # 正确身份已知且仅 key 变化，不改变空间，不重嵌。
        await service.sync_config(replace(config, embedding_api_key="rotated-test-key"))
        await asyncio.sleep(0)
        assert service._embedding is not first_provider
        assert service._embedding.api_key == "rotated-test-key"
        assert service._embedding.calls == 0
        assert service._state["last_stats"] is first_stats
        assert (await service.reindex())["reembedded_files"] == 0
        assert service._embedding.calls == 0

        _remove_fingerprint(config.index_dir)
        await service.sync_config(replace(config, embedding_base_url="https://b.example/v1"))
        async with asyncio.timeout(3):
            while (service._store.get_meta("embedding_fingerprint")
                   != "shared-model@https://b.example/v1" or service._state["indexing"]):
                await asyncio.sleep(0.01)
        assert service._embedding.calls == 1
        assert _vectors(service) == [[0.0, 1.0]]
    finally:
        await supervisor.cancel_owner("knowledge")
        await service.close()
        await supervisor.shutdown()
        service_module.EmbeddingProvider = original_provider
