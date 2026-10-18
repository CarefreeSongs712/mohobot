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
