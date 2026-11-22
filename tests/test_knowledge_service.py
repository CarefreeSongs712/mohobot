"""知识库服务回归: 增量索引 / 检索 / rerank 降级 / 别名查询 / 热同步(全程假 provider, 无网络)。"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.knowledge.service import KnowledgeService
from mohobot.models.config import KnowledgeConfig


class FakeEmbedding:
    """确定性假 embedding: 文本 hash 种子向量; 记录调用次数。"""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.calls = 0
        self.embedded_texts: list[str] = []

    def _vec(self, text: str) -> list[float]:
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.standard_normal(self.dim).astype(np.float32)
        n = float(np.linalg.norm(v))
        return (v / n if n > 0 else v).tolist()

    async def embed(self, texts):
        self.calls += 1
        self.embedded_texts.extend(texts)
        return [self._vec(t) for t in texts]

    async def close(self):
        pass


class FakeRerank:
    """假 rerank: fail=True 抛错(测降级); reverse=True 反转候选顺序(机械验证生效)。"""

    def __init__(self, fail: bool = False, reverse: bool = False):
        self.fail = fail
        self.reverse = reverse
        self.calls = 0

    async def rerank(self, query, documents, top_n=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("rerank 服务不可用")
        order = list(range(len(documents)))
        if self.reverse:
            order = order[::-1]
        scored = [(i, 1.0 / (pos + 1)) for pos, i in enumerate(order)]
        return scored[:top_n] if top_n else scored

    async def close(self):
        pass


def _write_kb(tmp: Path) -> None:
    (tmp / "虚拟歌手").mkdir(parents=True, exist_ok=True)
    (tmp / "虚拟歌手" / "洛天依.md").write_text(
        "# 洛天依\n## 简介\n世界首位中文 VOCALOID 虚拟歌手。\n", encoding="utf-8",
    )
    (tmp / "03-名曲.md").write_text(
        "# 代表名曲\n## 普通DISCO\n中V破圈起点, ilem 作品。\n"
        "## 权御天下\n洛天依古风旗帜之作。\n", encoding="utf-8",
    )
    (tmp / "index.json").write_text(json.dumps({
        "singers": [{
            "name": "洛天依", "en_name": "Luo Tianyi", "aliases": ["洛神", "吃货殿下"],
            "company": "上海禾念", "debut_date": "2012-07-12",
            "representative_songs": ["普通DISCO"], "file": "虚拟歌手/洛天依.md",
            "summary": "中V一姐。",
        }],
        "songs": [], "producers": [],
    }, ensure_ascii=False), encoding="utf-8")


def _service(tmp: Path, **overrides) -> tuple[KnowledgeService, FakeEmbedding, FakeRerank]:
    cfg = KnowledgeConfig(
        enabled=True,
        source_dir=str(tmp / "kb"),
        index_dir=str(tmp / "idx"),
        search_top_k=5, search_top_n=3,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    emb, rerank = FakeEmbedding(), FakeRerank()
    return KnowledgeService(cfg, embedding_provider=emb, rerank_provider=rerank), emb, rerank


async def test_reindex_incremental_and_search():
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "kb").mkdir()
            _write_kb(tmp / "kb")
            svc, emb, rerank = _service(tmp)
            try:
                await svc.startup()
                stats = await svc.reindex()
                assert "error" not in stats, stats
                assert stats["files"] == 3 and stats["chunks"] == 4, stats
                assert stats["reembedded_files"] == 3
                assert svc._state["ready"]

                # 无变化 → 零重嵌
                stats2 = await svc.reindex()
                assert stats2["reembedded_files"] == 0 and stats2["embedded_chunks"] == 0
                calls_after_first = emb.calls

                # 改一个文件 → 仅该文件重嵌(「权御天下」换成「勾指起誓」, 块数不变)
                (tmp / "kb" / "03-名曲.md").write_text(
                    "# 代表名曲\n## 普通DISCO\n中V破圈起点, ilem 作品。\n"
                    "## 勾指起誓\nilem 2019 神话曲。\n", encoding="utf-8",
                )
                stats3 = await svc.reindex()
                assert stats3["reembedded_files"] == 1, stats3
                assert stats3["chunks"] == 4 and stats3["embedded_chunks"] == 2
                assert emb.calls > calls_after_first

                # 删文件 → 索引同步移除
                (tmp / "kb" / "虚拟歌手" / "洛天依.md").unlink()
                stats4 = await svc.reindex()
                assert stats4["removed_files"] == 1 and stats4["chunks"] == 3

                # 检索: 基本契约(top_n 截断 + rerank 生效标记)
                result = await svc.search("普通DISCO 是谁的歌")
                assert "error" not in result, result
                assert result["reranked"] is True
                assert len(result["results"]) == 3  # top_n=3
                assert all(set(r) >= {"title", "source", "score", "text"} for r in result["results"])
                assert result["total_chunks"] == 3

                # 强制重建: 全部重嵌
                stats5 = await svc.reindex(force=True)
                assert stats5["reembedded_files"] == 2  # 剩 2 个文件
            finally:
                await svc.close()


async def test_rerank_failure_falls_back_to_embedding_order():
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "kb").mkdir()
            _write_kb(tmp / "kb")
            svc, _, rerank = _service(tmp)
            try:
                await svc.startup()
                await svc.reindex()
                # rerank 失败 → 回退 embedding 排序
                rerank.fail = True
                result = await svc.search("洛天依", top_n=4)
                assert "error" not in result, result
                assert result["reranked"] is False
                assert len(result["results"]) == 4
                emb_titles = [r["title"] for r in result["results"]]

                # rerank 反转候选顺序 → 结果顺序应随之反转
                rerank.fail = False
                rerank.reverse = True
                result2 = await svc.search("洛天依", top_n=4)
                assert result2["reranked"] is True
                titles2 = [r["title"] for r in result2["results"]]
                assert titles2 == emb_titles[::-1], (emb_titles, titles2)
            finally:
                await svc.close()


async def test_lookup_alias_hits_and_miss_hint():
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "kb").mkdir()
            _write_kb(tmp / "kb")
            svc, _, _ = _service(tmp)
            try:
                await svc.startup()
                await svc.reindex()

                hit = svc.lookup("洛神")
                assert hit["matches"][0]["name"] == "洛天依"
                assert hit["matches"][0]["type"] == "singer"
                assert hit["matches"][0]["company"] == "上海禾念"

                hit2 = svc.lookup("LUO TIANYI")  # 英文名, casefold
                assert hit2["matches"][0]["name"] == "洛天依"

                miss = svc.lookup("初音未来")
                assert "error" in miss and "knowledge_search" in miss["hint"]
            finally:
                await svc.close()


async def test_not_ready_and_empty_source_errors():
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 知识源目录不存在: startup 可成功, 索引后 ready=False
            svc, _, _ = _service(tmp)
            try:
                await svc.startup()
                stats = await svc.reindex()
                assert "error" in stats
                result = await svc.search("随便查查")
                assert "error" in result
                status = await svc.service_status()
                assert status["ready"] is False and status["enabled"] is True
            finally:
                await svc.close()

            # 空 knowledge api_key + 无 fallback: startup 失败
            cfg = KnowledgeConfig(
                enabled=True, source_dir=str(tmp / "kb"), index_dir=str(tmp / "idx2"),
                embedding_api_key="", embedding_base_url="https://x.invalid/v1",
            )
            svc2 = KnowledgeService(cfg, fallback_api_key="")
            try:
                await svc2.startup()
                raise AssertionError("缺 key 时 startup 应失败")
            except ValueError:
                pass


async def test_sync_config_rebuilds_provider_and_reindexes():
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "kb").mkdir()
            _write_kb(tmp / "kb")
            svc, emb, rerank = _service(tmp)
            try:
                await svc.startup()
                await svc.reindex()

                new_cfg = KnowledgeConfig(
                    enabled=True,
                    source_dir=str(tmp / "kb"), index_dir=str(tmp / "idx"),
                    embedding_model="BAAI/other-model",
                    embedding_base_url="https://x.invalid/v1",
                    embedding_api_key="k2",
                )
                await svc.sync_config(new_cfg)
                # 注入 provider 模式不重建客户端, 但模型变更仍触发后台重建
                before = svc._state["last_stats"]
                for _ in range(200):
                    st = svc._state["last_stats"]
                    if st is not None and st is not before and not svc._state["indexing"]:
                        break
                    await asyncio.sleep(0.02)
                assert svc._cfg.embedding_model == "BAAI/other-model"
                assert svc._state["last_stats"]["model_changed"] is True
            finally:
                await svc.close()


async def test_model_label_follows_actual_provider():
        """meta/行标签必须反映 provider 真实模型: 换模型但 provider 重建失败时,
        强制重建仍用旧模型嵌入, meta 不得标成新模型(否则配置齐后漏触发重建)。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "kb").mkdir()
            _write_kb(tmp / "kb")
            cfg = KnowledgeConfig(
                enabled=True, source_dir=str(tmp / "kb"), index_dir=str(tmp / "idx"),
            )
            emb = FakeEmbedding()
            emb.model = "model-v1"  # provider 自报模型
            svc = KnowledgeService(cfg, embedding_provider=emb, rerank_provider=FakeRerank())
            try:
                await svc.startup()
                await svc.reindex()
                meta = await asyncio.to_thread(svc._store.get_meta, "embedding_model")
                assert meta == "model-v1", meta

                # 配置改为 v2; 注入模式不重建 provider(模拟重建失败) → 仍用 v1 嵌入
                new_cfg = KnowledgeConfig(
                    enabled=True, source_dir=str(tmp / "kb"), index_dir=str(tmp / "idx"),
                    embedding_model="model-v2",
                )
                await svc.sync_config(new_cfg)
                before = svc._state["last_stats"]
                for _ in range(200):
                    st = svc._state["last_stats"]
                    if st is not None and st is not before and not svc._state["indexing"]:
                        break
                    await asyncio.sleep(0.02)
                meta2 = await asyncio.to_thread(svc._store.get_meta, "embedding_model")
                assert meta2 == "model-v1", meta2
            finally:
                await svc.close()


# ── #129 检索代次 / 快照隔离 ─────────────────────────────────────────


class GatedEmbedding(FakeEmbedding):
    """embed 阻塞到 gate 放开, 用于在 await 期间制造热切换/重建竞态。"""

    def __init__(self, dim: int = 8):
        super().__init__(dim)
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def embed(self, texts):
        self.started.set()
        await self.gate.wait()
        return await super().embed(texts)


async def _prepared_service(tmp: Path, *, dim: int = 8):
    (tmp / "kb").mkdir(exist_ok=True)
    _write_kb(tmp / "kb")
    svc, emb, rerank = _service(tmp)
    await svc.startup()
    await svc.reindex()
    assert svc._state["ready"]
    return svc, emb, rerank


async def test_reindex_same_model_new_endpoint_reembeds_all():
    """同名模型换服务端点(同维也是不同向量空间) → 全量重嵌并更新指纹。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "kb").mkdir()
        _write_kb(tmp / "kb")
        cfg = KnowledgeConfig(
            enabled=True, source_dir=str(tmp / "kb"), index_dir=str(tmp / "idx"),
        )
        emb = FakeEmbedding()
        emb.model = "shared-model"
        emb.base_url = "https://a.example/v1"
        svc = KnowledgeService(cfg, embedding_provider=emb, rerank_provider=FakeRerank())
        try:
            await svc.startup()
            stats1 = await svc.reindex()
            assert stats1["reembedded_files"] == 3, stats1
            fp1 = await asyncio.to_thread(svc._store.get_meta, "embedding_fingerprint")
            assert fp1 == "shared-model@https://a.example/v1", fp1

            emb.base_url = "https://b.example/v1"  # 同名模型换端点
            stats2 = await svc.reindex()
            assert stats2["model_changed"] is False, stats2
            assert stats2["embedding_space_changed"] is True, stats2
            assert stats2["reembedded_files"] == 3, stats2
            assert stats2["embedded_chunks"] == 4, stats2
            fp2 = await asyncio.to_thread(svc._store.get_meta, "embedding_fingerprint")
            assert fp2 == "shared-model@https://b.example/v1", fp2

            stats3 = await svc.reindex()
            assert stats3["reembedded_files"] == 0 and stats3["embedded_chunks"] == 0
        finally:
            await svc.close()


async def test_search_retries_with_new_snapshot_after_hot_swap():
    """查询 embedding 在途时 provider/矩阵被替换: 丢弃旧查询向量并用新快照重试。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        svc, _emb, _rerank = await _prepared_service(tmp)
        try:
            gated = GatedEmbedding()
            svc._embedding = gated
            task = asyncio.create_task(svc.search("洛天依", top_n=3))
            await asyncio.wait_for(gated.started.wait(), 2)
            # 在 search await 期间模拟热切换 + 全量重建
            new_emb = FakeEmbedding()
            svc._embedding = new_emb
            svc._bump_generation()
            await svc.reindex(force=True)
            gated.gate.set()
            result = await asyncio.wait_for(task, 2)
            assert "error" not in result, result
            assert result["total_chunks"] == 4, result
            # 旧 provider 的结果没有被采用, 新 provider 为查询重新算过向量
            assert new_emb.calls >= 1, new_emb.calls
        finally:
            await svc.close()


async def test_search_rejects_cross_space_provider_without_rebuild():
    """热切换后重建完成前: 拒绝用 B 的查询向量和 A 的矩阵算相似度。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        svc, _emb, _rerank = await _prepared_service(tmp)
        try:
            new_emb = FakeEmbedding()
            new_emb.model = "other-model"
            new_emb.base_url = "https://other.example/v1"
            svc._embedding = new_emb
            svc._bump_generation()
            result = await svc.search("洛天依")
            assert "error" in result, result
            assert result.get("retryable") is True, result
        finally:
            await svc.close()


async def test_search_inflight_close_returns_error_not_attribute_error():
    """关闭时在途检索必须返回明确错误, 不能读 None.shape / 旧矩阵。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        svc, _emb, _rerank = await _prepared_service(tmp)
        gated = GatedEmbedding()
        svc._embedding = gated
        gated._service = svc
        task = asyncio.create_task(svc.search("洛天依"))
        await asyncio.wait_for(gated.started.wait(), 2)
        await svc.close()
        gated.gate.set()
        result = await asyncio.wait_for(task, 2)
        assert "error" in result, result


async def test_search_inflight_dimension_change_retries():
    """在途检索期间索引换成不同维度: 重试或可重试错误, 不抛 AttributeError。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        svc, _emb, _rerank = await _prepared_service(tmp)
        try:
            gated = GatedEmbedding()
            svc._embedding = gated
            task = asyncio.create_task(svc.search("洛天依"))
            await asyncio.wait_for(gated.started.wait(), 2)
            new_emb = FakeEmbedding(dim=12)
            svc._embedding = new_emb
            svc._bump_generation()
            await svc.reindex(force=True)
            gated.gate.set()
            result = await asyncio.wait_for(task, 2)
            assert "error" not in result, result
            assert result["total_chunks"] == 4, result
        finally:
            await svc.close()


def test_select_top_k_matches_full_sort():
    """top-k 选择用 argpartition, 结果必须与全排序 top-k 一致(10k 行)。"""
    rng = np.random.default_rng(7)
    scores = rng.standard_normal(10_000).astype(np.float32)
    expected = np.argsort(-scores)[:5].tolist()
    assert KnowledgeService._select_top_k(scores, 5).tolist() == expected
    assert KnowledgeService._select_top_k(scores, 10_000).shape[0] == 10_000


async def test_sync_config_same_model_new_base_url_triggers_rebuild(tmp_path):
    """内置 provider 模式下只看模型名会漏掉"同名换端点"; 指纹变更必须重建。"""
    # tests/_run_all.py 只支持 tmp_path, 不能用 monkeypatch fixture; 手工还原。
    import mohobot.knowledge.service as service_mod

    original_provider_cls = service_mod.EmbeddingProvider
    created: list[FakeEmbedding] = []

    def fake_provider(*, base_url, api_key, model, timeout=60.0):
        provider = FakeEmbedding()
        provider.model = model
        provider.base_url = base_url
        created.append(provider)
        return provider

    def make_cfg(base_url: str) -> KnowledgeConfig:
        return KnowledgeConfig(
            enabled=True,
            source_dir=str(tmp_path / "kb"), index_dir=str(tmp_path / "idx"),
            embedding_model="embed-m", embedding_base_url=base_url,
            embedding_api_key="k", rerank_model="",
        )

    (tmp_path / "kb").mkdir()
    _write_kb(tmp_path / "kb")

    service_mod.EmbeddingProvider = fake_provider
    svc = KnowledgeService(make_cfg("https://a.example/v1"))
    try:
        await svc.startup()
        stats1 = await svc.reindex()
        assert stats1["reembedded_files"] == 3, stats1
        assert created and created[0].base_url == "https://a.example/v1"

        # 同名模型换端点 → 后台重建
        await svc.sync_config(make_cfg("https://b.example/v1"))
        fp = ""
        for _ in range(300):
            fp = await asyncio.to_thread(
                svc._store.get_meta, "embedding_fingerprint",
            )
            if fp == "embed-m@https://b.example/v1" and not svc._state["indexing"]:
                break
            await asyncio.sleep(0.01)
        assert fp == "embed-m@https://b.example/v1", fp
        stats2 = svc._state["last_stats"]
        assert stats2["embedding_space_changed"] is True, stats2
        assert stats2["model_changed"] is False, stats2
        assert stats2["reembedded_files"] == 3, stats2

        # 配置未再变化 → 不应重复重建
        await svc.sync_config(make_cfg("https://b.example/v1"))
        await asyncio.sleep(0.1)
        assert svc._state["last_stats"] is stats2
    finally:
        await svc.close()
        service_mod.EmbeddingProvider = original_provider_cls


async def test_retired_providers_are_capped_on_repeated_hot_reload(tmp_path):
    """#137: 连续热更新换 provider 时旧客户端有上限, 超出部分延迟关闭且 close 后不泄漏。"""
    import mohobot.knowledge.service as service_mod

    original_provider_cls = service_mod.EmbeddingProvider
    created: list = []

    class TrackingEmbedding(FakeEmbedding):
        def __init__(self):
            super().__init__()
            self.model = ""
            self.base_url = ""
            self.closed = False

        async def close(self):
            self.closed = True

    def fake_provider(*, base_url, api_key, model, timeout=60.0):
        provider = TrackingEmbedding()
        provider.model = model
        provider.base_url = base_url
        created.append(provider)
        return provider

    def make_cfg(base_url: str) -> KnowledgeConfig:
        return KnowledgeConfig(
            enabled=True,
            source_dir=str(tmp_path / "kb"), index_dir=str(tmp_path / "idx"),
            embedding_model="embed-m", embedding_base_url=base_url,
            embedding_api_key="k", rerank_model="",
        )

    (tmp_path / "kb").mkdir()
    _write_kb(tmp_path / "kb")

    service_mod.EmbeddingProvider = fake_provider
    svc = KnowledgeService(make_cfg("https://a.example/v1"))
    try:
        await svc.startup()
        await svc.reindex()
        for i in range(12):
            await svc.sync_config(make_cfg(f"https://h{i}.example/v1"))
        assert len(svc._retired_providers) <= service_mod._MAX_RETIRED_PROVIDERS
        # 超出上限的旧 provider 排队延迟关闭, 不会被无限保留
        assert len(svc._retired_tasks) >= 1
    finally:
        await svc.close()
        service_mod.EmbeddingProvider = original_provider_cls

    assert not svc._retired_tasks
    assert all(p.closed for p in created), [p.closed for p in created]


class RecordingSupervisor:
    """替身 task_supervisor: 记录 create_task 的 name/owner, 便于断言注册路径。"""

    def __init__(self):
        self.created: list[dict] = []

    def create_task(self, coro, *, name, owner="application"):
        task = asyncio.create_task(coro)
        self.created.append({"name": name, "owner": owner, "task": task})
        return task


async def test_retired_provider_close_registered_with_supervisor(tmp_path):
    """#158 N3: 旧 provider 延迟关闭任务必须注册到 task_supervisor(owner=knowledge)。"""
    import mohobot.knowledge.service as service_mod

    original_provider_cls = service_mod.EmbeddingProvider
    created: list = []

    class TrackingEmbedding(FakeEmbedding):
        def __init__(self):
            super().__init__()
            self.model = ""
            self.base_url = ""
            self.closed = False

        async def close(self):
            self.closed = True

    def fake_provider(*, base_url, api_key, model, timeout=60.0):
        provider = TrackingEmbedding()
        provider.model = model
        provider.base_url = base_url
        created.append(provider)
        return provider

    def make_cfg(base_url: str) -> KnowledgeConfig:
        return KnowledgeConfig(
            enabled=True,
            source_dir=str(tmp_path / "kb"), index_dir=str(tmp_path / "idx"),
            embedding_model="embed-m", embedding_base_url=base_url,
            embedding_api_key="k", rerank_model="",
        )

    (tmp_path / "kb").mkdir()
    _write_kb(tmp_path / "kb")

    supervisor = RecordingSupervisor()
    service_mod.EmbeddingProvider = fake_provider
    svc = KnowledgeService(
        make_cfg("https://a.example/v1"), task_supervisor=supervisor,
    )
    try:
        await svc.startup()
        await svc.reindex()
        for i in range(12):
            await svc.sync_config(make_cfg(f"https://h{i}.example/v1"))
        retired = [c for c in supervisor.created if c["name"] == "knowledge-retired-provider"]
        assert retired, supervisor.created
        assert all(c["owner"] == "knowledge" for c in retired), supervisor.created
    finally:
        await svc.close()
        service_mod.EmbeddingProvider = original_provider_cls

    # close() 取消受监管任务后, 未关闭的旧客户端在 close() 中统一回收
    assert all(p.closed for p in created), [p.closed for p in created]


async def test_retired_client_not_closed_while_in_use(tmp_path):
    """#158 N3: 在途请求还没结束时, 延迟关闭任务不能关掉正在使用的客户端。"""
    svc, _emb, _rerank = await _prepared_service(tmp_path)
    try:
        provider = FakeEmbedding()
        provider.closed = False

        async def close():
            provider.closed = True

        provider.close = close
        svc._acquire_provider(provider)
        task = asyncio.create_task(
            svc._close_retired_when_idle(provider, delay=0.02),
        )
        await asyncio.sleep(0.08)
        assert provider.closed is False
        svc._release_provider(provider)
        await asyncio.wait_for(task, 1)
        assert provider.closed is True
    finally:
        await svc.close()


async def test_search_identity_mismatch_recovers_via_background_rebuild(tmp_path):
    """#158 N4: 身份不符时先 fail-closed, 后台自愈重建后无需人工介入即可恢复检索。"""
    svc, _emb, _rerank = await _prepared_service(tmp_path)
    try:
        new_emb = FakeEmbedding()
        new_emb.model = "other-model"
        new_emb.base_url = "https://other.example/v1"
        svc._embedding = new_emb
        svc._bump_generation()
        result = await svc.search("洛天依")
        assert result.get("retryable") is True, result
        expected = svc._embedding_identity(new_emb)
        for _ in range(300):
            if not svc._state["indexing"] and svc._index_identity == expected:
                break
            await asyncio.sleep(0.01)
        assert svc._index_identity == expected, svc._index_identity
        recovered = await svc.search("洛天依")
        assert "error" not in recovered, recovered
    finally:
        await svc.close()


async def test_background_reindex_retries_on_failure(tmp_path):
    """#158 N4: 后台重建失败会按退避重试, 不是一次失败就永久停住。"""
    import mohobot.knowledge.service as service_mod

    svc, _emb, _rerank = await _prepared_service(tmp_path)
    original_delay = service_mod._REINDEX_RETRY_DELAY
    service_mod._REINDEX_RETRY_DELAY = 0.01
    calls = 0

    async def flaky_reindex(force: bool = False):
        nonlocal calls
        calls += 1
        return {"error": "transient"} if calls < 3 else {"ok": True}

    try:
        svc.reindex = flaky_reindex
        svc._schedule_model_reindex()
        for _ in range(300):
            if calls >= 3:
                break
            await asyncio.sleep(0.01)
        assert calls == service_mod._REINDEX_RETRY_ATTEMPTS, calls
    finally:
        service_mod._REINDEX_RETRY_DELAY = original_delay
        await svc.close()
