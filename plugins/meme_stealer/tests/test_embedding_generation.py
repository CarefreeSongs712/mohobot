"""嵌入代次隔离（issue #129）：模型/缓存切换与在途检索、写入的竞态。"""

import asyncio
import types

import numpy as np

from meme_stealer_core.core.search.embedding_service import STORE_SIG, EmbeddingService


class FakeEmbeddingDB:
    """最小 SQLite 向量表替身：只实现 EmbeddingService 用到的接口。"""

    def __init__(self, model_sig: str = ""):
        self.rows: dict[str, tuple[bytes, int, str]] = {}
        self.meta: dict[str, str] = {}
        if model_sig:
            self.meta["embedding_model_sig"] = model_sig

    def load_embeddings_by_sig(self, model_sig: str):
        return [
            {"path": path, "vector": blob, "dim": dim}
            for path, (blob, dim, sig) in self.rows.items()
            if sig == model_sig
        ]

    def upsert_embedding(self, path, vector_blob, dim, model_sig):
        self.rows[path] = (vector_blob, int(dim), model_sig)

    def clear_all_embeddings(self):
        self.rows.clear()

    def get_meta_value(self, key):
        return self.meta.get(key)

    def set_meta_value(self, key, value):
        self.meta[key] = value


class GatedProvider:
    """可阻塞的嵌入 provider；记录捕获的签名与调用次数。"""

    def __init__(self, signature: str, *, vectors=None, default=None, gated=False):
        self.signature = signature
        self.provider_config = {"id": signature}
        self._vectors = dict(vectors or {})
        self._default = default
        self.calls = 0
        self.started = asyncio.Event()
        self.gate = asyncio.Event() if gated else None

    async def get_embedding(self, text):
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        vec = self._vectors.get(text)
        if vec is None:
            vec = self._default if self._default is not None else [0.0, 0.0, 0.0, 0.0]
        return list(vec)


class FakeModels:
    def __init__(self, provider, signature: str):
        self.provider = provider
        self.signature = signature

    def describe(self, role: str) -> str:
        return self.signature

    def embedding_provider(self):
        return self.provider


def _plugin(models, db):
    return types.SimpleNamespace(
        enable_embedding_search=True,
        plugin_config=types.SimpleNamespace(category_info={}, character_info={}),
        models=models,
        db_service=db,
    )


async def test_search_retries_when_cache_invalidated_during_embed():
    vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    db = FakeEmbeddingDB("emb-a@https://a/v1")
    db.upsert_embedding("/a.png", vec.tobytes(), 4, STORE_SIG)
    provider = GatedProvider("emb-a@https://a/v1", vectors={"查询": vec.tolist()}, gated=True)
    service = EmbeddingService(_plugin(FakeModels(provider, provider.signature), db))

    task = asyncio.create_task(service.search("查询"))
    await asyncio.wait_for(provider.started.wait(), 2)
    # 模拟在途期间缓存失效（回填/写入/清理都会走到这里）：矩阵变 None
    service.invalidate_cache()
    provider.gate.set()

    results = await asyncio.wait_for(task, 2)
    assert [path for path, _ in results] == ["/a.png"]
    assert provider.calls == 2  # 旧代次快照被丢弃，用新矩阵重试


async def test_search_uses_new_generation_after_model_switch_during_embed():
    old_vec = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    new_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    db = FakeEmbeddingDB("emb-a@https://a/v1")
    db.upsert_embedding("/old.png", old_vec.tobytes(), 4, STORE_SIG)
    provider_a = GatedProvider("emb-a@https://a/v1", vectors={"查询": old_vec.tolist()}, gated=True)
    models = FakeModels(provider_a, provider_a.signature)
    service = EmbeddingService(_plugin(models, db))

    task = asyncio.create_task(service.search("查询"))
    await asyncio.wait_for(provider_a.started.wait(), 2)

    # 配置切到另一向量空间：旧向量被清空，新空间写入新向量
    provider_b = GatedProvider("emb-b@https://b/v1", vectors={"查询": new_vec.tolist()})
    models.provider, models.signature = provider_b, provider_b.signature
    assert service._get_provider() is provider_b
    db.upsert_embedding("/new.png", new_vec.tobytes(), 4, STORE_SIG)
    service.invalidate_cache()

    provider_a.gate.set()
    results = await asyncio.wait_for(task, 2)
    assert [path for path, _ in results] == ["/new.png"]
    assert provider_a.calls == 1
    assert provider_b.calls == 1


async def test_insert_emoji_retries_and_writes_new_generation_vector():
    vec_a = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.0, 0.0, 1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    db = FakeEmbeddingDB("emb-a@https://a/v1")
    provider_a = GatedProvider("emb-a@https://a/v1", default=vec_a.tolist(), gated=True)
    models = FakeModels(provider_a, provider_a.signature)
    service = EmbeddingService(_plugin(models, db))

    task = asyncio.create_task(service.insert_emoji("/x.png", {"desc": "猫猫"}))
    await asyncio.wait_for(provider_a.started.wait(), 2)

    provider_b = GatedProvider("emb-b@https://b/v1", default=vec_b.tolist())
    models.provider, models.signature = provider_b, provider_b.signature
    service._get_provider()  # 切换代次：清空旧空间向量并接受 B 空间

    provider_a.gate.set()
    assert await asyncio.wait_for(task, 2) is True

    assert list(db.rows) == ["/x.png"]
    blob, dim, _sig = db.rows["/x.png"]
    assert dim == 6
    assert np.frombuffer(blob, dtype=np.float32).tolist() == vec_b.tolist()
    assert provider_a.calls == 1
    assert provider_b.calls == 1


async def test_insert_emoji_skips_write_when_generation_keeps_changing():
    class UnstableProvider:
        def __init__(self):
            self.signature = "emb-unstable@https://a/v1"
            self.provider_config = {"id": self.signature}
            self.service = None
            self.calls = 0

        async def get_embedding(self, text):
            self.calls += 1
            self.service.invalidate_cache()  # 每次 awai 后都换代
            return [1.0, 0.0, 0.0, 0.0]

    db = FakeEmbeddingDB("emb-unstable@https://a/v1")
    provider = UnstableProvider()
    service = EmbeddingService(_plugin(FakeModels(provider, provider.signature), db))
    provider.service = service

    assert await service.insert_emoji("/x.png", {"desc": "猫猫"}) is False
    assert provider.calls == 2
    assert db.rows == {}
    assert await service.search("查询") == []


async def test_search_returns_empty_without_provider_after_close():
    vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    db = FakeEmbeddingDB("emb-a@https://a/v1")
    db.upsert_embedding("/a.png", vec.tobytes(), 4, STORE_SIG)
    provider = GatedProvider("emb-a@https://a/v1", vectors={"查询": vec.tolist()}, gated=True)
    service = EmbeddingService(_plugin(FakeModels(provider, provider.signature), db))

    task = asyncio.create_task(service.search("查询"))
    await asyncio.wait_for(provider.started.wait(), 2)
    await service.close()
    provider.gate.set()

    assert await asyncio.wait_for(task, 2) == []
