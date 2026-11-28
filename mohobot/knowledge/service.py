"""知识库服务 — 索引编排 + 检索编排。

生命周期(main.py 装配, 失败降级):
    startup()   打开索引库、构建 API provider、加载别名表
    reindex()   启动时由 task_supervisor 后台跑一次; 面板可手动触发
    search()    knowledge_search 工具入口: query 嵌入 → 余弦 top-k
                → (配置了 rerank 时)重排 → 截断 top_n
    lookup()    knowledge_lookup 工具入口: index.json 名称/别名精确匹配
    sync_config() WebUI 热同步; embedding 模型变更自动触发重建
    close()

降级语义: 检索/索引失败只影响本子系统(工具层拿到错误 JSON),
不阻塞、不影响主聊天路径。ready=False(未建索引/索引中)时工具
返回明确错误, 模型可改走 song_* 或 anysearch_search。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from mohobot.knowledge.chunker import (
    Chunk, chunk_file, scan_source_dir,
)
from mohobot.knowledge.index_store import KnowledgeIndexStore
from mohobot.knowledge.providers import EmbeddingProvider, RerankProvider, embed_in_batches

_EMBEDDING_KEY_ENV = "MOHOBOT_EMBEDDING_API_KEY"
_RERANK_KEY_ENV = "MOHOBOT_RERANK_API_KEY"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 换配置后保留给在途调用收尾的旧 provider 上限(#137): 超出后延迟关闭,
# 避免长期运行反复热更新时旧客户端无界累积。
_MAX_RETIRED_PROVIDERS = 8
_RETIRED_PROVIDER_CLOSE_DELAY = 60.0
# 检索发现 provider 身份与索引不符时, 后台自愈重建的冷却时间(#158 N4):
# 第一次仍 fail-closed 返回可重试错误, 但不会再要求人工重建。
_IDENTITY_REBUILD_COOLDOWN = 30.0
# 后台重建失败的重试退避: 首次失败 5s, 之后 10s, 不做无限风暴(#158 N4)。
_REINDEX_RETRY_ATTEMPTS = 3
_REINDEX_RETRY_DELAY = 5.0


class KnowledgeService:
    """虚拟歌手知识库(RAG)服务。"""

    def __init__(
        self,
        config,
        fallback_api_key: str = "",
        embedding_provider: EmbeddingProvider | None = None,
        rerank_provider: RerankProvider | None = None,
        task_supervisor=None,
    ):
        self._cfg = config
        self._fallback_api_key = (fallback_api_key or "").strip()
        # provider 注入点(测试用); 缺省 startup 时按配置构建
        self._embedding = embedding_provider
        self._rerank = rerank_provider
        self._providers_builtin = embedding_provider is None
        self._supervisor = task_supervisor
        self._store: KnowledgeIndexStore | None = None
        self._retired_providers: list = []   # 换配置后保留到 close 统一回收(同 LLMService 约定)
        self._retired_tasks: dict[asyncio.Task, Any] = {}  # 延迟关闭任务 → 旧 provider(#137)
        # 旧 provider 在途使用计数: 延迟关闭前必须清零, 避免关掉正在请求的客户端(#158 N3)
        self._provider_inflight: dict[Any, int] = {}
        self._identity_rebuild_at: float = 0.0  # 上次身份自愈重建时间(monotonic, #158 N4)
        self._lock = asyncio.Lock()          # reindex 互斥
        self._chunks: list[dict] = []        # 与矩阵行一一对应
        self._matrix: np.ndarray | None = None
        # 检索快照代次: 矩阵/块列表/provider 身份任一变化即递增。
        # search() 在 await embedding 前捕获快照, await 后校验代次, 避免旧查询向量
        # 与新矩阵/新 provider 混代使用(同维换模型会静默错排)。
        self._generation: int = 0
        self._index_identity: tuple[str, str] = ("", "")  # 构建矩阵所用 embedding (model, base_url)
        self._lookup_entries: list[dict] = []  # index.json 名称/别名条目
        self._active_signature: tuple = ()   # 构建 provider 时的配置指纹
        self._state: dict[str, Any] = {
            "ready": False, "indexing": False, "error": None,
            "last_stats": None, "last_indexed_at": None,
        }

    # ── 生命周期 ──────────────────────────────────────────────

    async def startup(self) -> None:
        if self._providers_builtin:
            embedding_key = (
                (self._cfg.embedding_api_key or "").strip()
                or os.environ.get(_EMBEDDING_KEY_ENV, "").strip()
                or self._fallback_api_key
            )
            base_url = (self._cfg.embedding_base_url or "").strip()
            if not base_url:
                raise ValueError("knowledge.embedding_base_url 未配置")
            if not embedding_key:
                raise ValueError(
                    "embedding api_key 未配置(knowledge.embedding_api_key / "
                    f"{_EMBEDDING_KEY_ENV} / chat key 兜底均不可用)"
                )
            if not (self._cfg.embedding_model or "").strip():
                raise ValueError("knowledge.embedding_model 未配置")
            self._embedding = EmbeddingProvider(
                base_url=base_url, api_key=embedding_key,
                model=self._cfg.embedding_model,
            )
            self._rerank = self._build_rerank(embedding_key, base_url)
        # 注入 provider(测试)时不校验/不重建, 指纹仅用于 sync_config 变更检测
        self._active_signature = self._provider_signature()

        self._store = KnowledgeIndexStore(self._cfg.index_dir)
        try:
            await asyncio.to_thread(self._store.open)
            await asyncio.to_thread(self._refresh_lookup)
        except Exception:
            # 索引库打不开时回收已建好的 API 客户端再向上抛(main 降级)
            await self._close_providers()
            raise
        logger.info(
            f"知识库服务已初始化: source={self._cfg.source_dir} "
            f"index={self._cfg.index_dir} embedding={self._cfg.embedding_model} "
            f"rerank={self._cfg.rerank_model or '(关闭)'}"
        )

    def _build_rerank(self, embedding_key: str, embedding_base_url: str) -> RerankProvider | None:
        model = (self._cfg.rerank_model or "").strip()
        if not model:
            return None
        key = (
            (self._cfg.rerank_api_key or "").strip()
            or os.environ.get(_RERANK_KEY_ENV, "").strip()
            or embedding_key
        )
        base_url = (self._cfg.rerank_base_url or "").strip() or embedding_base_url
        return RerankProvider(base_url=base_url, api_key=key, model=model)

    def _provider_signature(self) -> tuple:
        return (
            self._cfg.embedding_model, self._cfg.embedding_base_url,
            self._cfg.embedding_api_key, self._cfg.rerank_model,
            self._cfg.rerank_base_url, self._cfg.rerank_api_key,
        )

    def _embedding_identity(self, provider=None) -> tuple[str, str]:
        """实际用于向量化的 (model, base_url)。

        provider 自报优先(注入 provider 可能没有 model/base_url 属性); 缺失时回退
        当前配置。base_url 归一化掉末尾斜杠, 只影响比较。
        """
        provider = self._embedding if provider is None else provider
        if provider is None:
            return ("", "")
        model = str(getattr(provider, "model", "") or "").strip() \
            or str(getattr(self._cfg, "embedding_model", "") or "").strip()
        base_url = str(getattr(provider, "base_url", "") or "").strip() \
            or str(getattr(self._cfg, "embedding_base_url", "") or "").strip()
        return (model, base_url.rstrip("/"))

    @staticmethod
    def _identity_fingerprint(identity: tuple[str, str]) -> str:
        model, base_url = identity
        return f"{model}@{base_url or 'default'}"

    def _bump_generation(self) -> None:
        """让在途检索快照失效(矩阵/块/provider 身份即将或已经变化)。"""
        self._generation += 1

    def _acquire_provider(self, provider) -> None:
        """登记一次在途 provider 使用, 延迟关闭任务据此避免提前关客户端(#158 N3)。"""
        if provider is None:
            return
        self._provider_inflight[provider] = self._provider_inflight.get(provider, 0) + 1

    def _release_provider(self, provider) -> None:
        if provider is None:
            return
        remaining = self._provider_inflight.get(provider, 0) - 1
        if remaining > 0:
            self._provider_inflight[provider] = remaining
        else:
            self._provider_inflight.pop(provider, None)

    def _retire_providers(self, *providers) -> None:
        """登记换配置后的旧 provider, 超出上限的延迟关闭(#137 / 审查 N3)。

        旧客户端要留给可能仍在途的调用收尾, 但连续热更新不能无界累积;
        超过上限的部分注册为受监管后台任务, 空闲且无在途使用时才关闭。
        """
        for provider in providers:
            if provider is not None:
                self._retired_providers.append(provider)
        overflow = len(self._retired_providers) - _MAX_RETIRED_PROVIDERS
        if overflow <= 0:
            return
        retired = self._retired_providers[:overflow]
        del self._retired_providers[:overflow]
        for provider in retired:
            self._schedule_retired_close(provider)

    def _schedule_retired_close(self, provider) -> None:
        """排队延迟关闭旧 provider: 优先注册 task_supervisor(owner=knowledge)。"""
        coro = self._close_retired_when_idle(provider)
        task: asyncio.Task | None = None
        if self._supervisor is not None:
            try:
                task = self._supervisor.create_task(
                    coro, name="knowledge-retired-provider", owner="knowledge",
                )
            except Exception:
                # 监管器已停止(create_task 会关闭 coro): 交回 close() 统一回收。
                self._retired_providers.append(provider)
                return
        if task is None:
            try:
                task = asyncio.create_task(coro)
            except RuntimeError:  # 无运行中的 event loop: 交给 close 统一回收
                self._retired_providers.append(provider)
                return
        self._retired_tasks[task] = provider
        task.add_done_callback(self._retired_close_done)

    def _retired_close_done(self, task: asyncio.Task) -> None:
        provider = self._retired_tasks.pop(task, None)
        if provider is None:
            # close() 已接管该客户端, 不再重复登记。
            return
        try:
            closed = not task.cancelled() and task.result()
        except Exception:
            closed = False
        if not closed:
            # main 先 cancel_owner("knowledge") 再 close(); 即使任务尚未开始
            # 或在等待空闲/关闭期间被取消, 服务仍须持有客户端直到统一回收。
            self._retired_providers.append(provider)

    async def _close_retired_when_idle(
        self, provider, delay: float = _RETIRED_PROVIDER_CLOSE_DELAY,
    ) -> bool:
        """至少空闲 delay 秒且在途使用清零后才关闭, 防止关掉在途请求的客户端。"""
        while True:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            if not self._provider_inflight.get(provider):
                break
        try:
            await provider.close()
        except Exception:
            return False
        return True

    async def _close_providers(self) -> None:
        providers = [self._embedding, self._rerank, *self._retired_providers]
        self._retired_providers = []
        for provider in providers:
            if provider is None or not self._providers_builtin:
                continue
            try:
                await provider.close()
            except Exception:
                pass

    async def close(self) -> None:
        # 先等在途 reindex 收尾(共享 asyncio 锁), 避免关闭索引库时半途写入
        async with self._lock:
            self._state["ready"] = False
            self._bump_generation()   # 让在途 search 的快照立即失效, 不再读已清空的矩阵
            tasks = list(self._retired_tasks)
            pending = [self._retired_tasks.pop(t) for t in tasks]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # 取消的延迟关闭任务所持旧客户端在此统一回收, 避免泄漏
            for provider in pending:
                try:
                    await provider.close()
                except Exception:
                    pass
            await self._close_providers()
            if self._store is not None:
                await asyncio.to_thread(self._store.close)
            self._provider_inflight.clear()
            self._embedding = self._rerank = None
            self._store = None
            self._matrix = None
            self._chunks = []
            self._index_identity = ("", "")

    # ── 索引 ──────────────────────────────────────────────────

    async def reindex(self, force: bool = False) -> dict:
        """扫描知识源 → 增量嵌入入库 → 重建内存矩阵。可并发调用, 内部互斥。"""
        if self._store is None or self._embedding is None:
            return {"error": "知识库服务未初始化"}
        async with self._lock:
            if self._state["indexing"]:
                return {"error": "索引重建已在进行中"}
            self._state["indexing"] = True
            try:
                return await self._reindex_locked(force)
            except Exception as exc:
                self._state["error"] = str(exc)
                logger.warning(f"知识库索引重建失败: {exc}")
                return {"error": str(exc)}
            finally:
                self._state["indexing"] = False

    async def _reindex_locked(self, force: bool) -> dict:
        started = time.monotonic()
        source_dir = self._cfg.source_dir
        if not Path(source_dir).is_dir():
            self._state["error"] = f"知识源目录不存在: {source_dir}"
            return {"error": self._state["error"]}

        files = await asyncio.to_thread(scan_source_dir, source_dir)
        file_states = {path: (mtime, size) for path, _, mtime, size in files}
        all_chunks: dict[str, list[Chunk]] = {}
        for path, abs_path, _, _ in files:
            all_chunks[path] = await asyncio.to_thread(chunk_file, abs_path, path)

        # 捕获本次重建使用的 provider: sync_config 可能并发替换 self._embedding,
        # 重建中途读 live 属性会混用旧模型标签与新 provider 向量(异代写入)。
        embedding = self._embedding
        embedding_identity = self._embedding_identity(embedding)
        fingerprint = self._identity_fingerprint(embedding_identity)

        # 实际用于嵌入的模型名: provider 自报优先。热同步换模型但 provider 重建
        # 失败(如缺 key)时 provider 仍是旧模型 —— meta/行标签必须反映真实模型,
        # 否则标记成新模型会导致下次配置齐后漏触发重建, 索引混入旧模型向量。
        embed_model = embedding_identity[0] or self._cfg.embedding_model
        stored = await asyncio.to_thread(self._store.list_file_states)
        stored_model = await asyncio.to_thread(self._store.get_meta, "embedding_model")
        stored_fingerprint = await asyncio.to_thread(
            self._store.get_meta, "embedding_fingerprint",
        )
        model_changed = bool(stored_model) and stored_model != embed_model
        # 旧索引只有模型名时无法证明服务来源；缺失指纹也必须重嵌，不能把
        # 来源不明的旧向量直接标成当前空间。同名换端点(甚至同维)同样重嵌。
        identity_changed = stored_fingerprint != fingerprint
        reembed_all = model_changed or identity_changed

        # 待重嵌文件: 新文件 / mtime+size 变化 / 强制 / embedding 向量空间变更
        changed = [
            path for path in file_states
            if force or reembed_all or path not in stored
            or stored[path] != file_states[path]
        ]
        removed = [path for path in stored if path not in file_states]

        embedded_count = 0
        if changed:
            pending: list[tuple[str, Chunk]] = [
                (path, chunk) for path in changed for chunk in all_chunks[path]
            ]
            if pending:
                # 嵌入输入带标题(标题不含在正文里, 带上才能命中"按名字搜"的查询)
                # 在途使用期间 sync_config 可能把该 provider 移出活跃列表, 计数
                # 保证延迟关闭任务不会关掉正在嵌入的客户端(#158 N3)。
                self._acquire_provider(embedding)
                try:
                    vectors = await embed_in_batches(
                        embedding,
                        [f"{c.title}\n{c.text}" for _, c in pending],
                    )
                finally:
                    self._release_provider(embedding)
                by_file: dict[str, list[dict]] = {}
                for (path, chunk), vec in zip(pending, vectors):
                    by_file.setdefault(path, []).append({
                        "chunk_id": chunk.chunk_id,
                        "title": chunk.title,
                        "text": chunk.text,
                        "embedding": np.asarray(vec, dtype=np.float32).tobytes(),
                    })
                indexed_at = _now()
                for path in changed:
                    rows = by_file.get(path, [])
                    mtime, size = file_states[path]
                    await asyncio.to_thread(
                        self._store.replace_file,
                        path, mtime, size, rows,
                        embed_model, indexed_at,
                    )
                    embedded_count += len(rows)
            else:
                # 文件被清空/只剩跳过文件: 也要刷新 files 状态, 避免残留
                for path in changed:
                    if path not in all_chunks or not all_chunks[path]:
                        mtime, size = file_states[path]
                        await asyncio.to_thread(
                            self._store.replace_file,
                            path, mtime, size, [],
                            embed_model, _now(),
                        )
        for path in removed:
            await asyncio.to_thread(self._store.remove_file, path)

        await asyncio.to_thread(self._store.set_meta, "embedding_model", embed_model)
        await asyncio.to_thread(
            self._store.set_meta, "embedding_fingerprint", fingerprint,
        )
        await asyncio.to_thread(self._store.set_meta, "last_indexed_at", _now())
        await asyncio.to_thread(self._refresh_lookup)
        await self._load_matrix(embedding_identity)

        stats = {
            "files": len(file_states),
            "chunks": len(self._chunks),
            "reembedded_files": len(changed),
            "embedded_chunks": embedded_count,
            "removed_files": len(removed),
            "elapsed_sec": round(time.monotonic() - started, 2),
            "model_changed": model_changed,
            "embedding_space_changed": reembed_all,
        }
        self._state["error"] = None
        self._state["last_stats"] = stats
        self._state["last_indexed_at"] = _now()
        if not self._chunks:
            self._state["error"] = "知识源为空(未产出任何知识块)"
        else:
            self._state["ready"] = True
        logger.info(f"知识库索引完成: {stats}")
        return stats

    async def _load_matrix(self, identity: tuple[str, str] | None = None) -> None:
        """全量载入 chunks 并构建归一化向量矩阵(行内积即余弦相似度)。

        先在局部变量里建好完整快照, 再一次性替换并递增代次; 在途 search 捕获的
        旧快照因此不会与新旧混合的成员搭配使用。identity 是本次写库所用 provider
        的 (model, base_url), search 会用它确认查询向量与矩阵属于同一向量空间。
        """
        rows = await asyncio.to_thread(self._store.load_chunks)
        chunks: list[dict] = []
        matrix: np.ndarray | None = None
        if rows:
            dim = None
            vectors: list[np.ndarray] = []
            for row in rows:
                if not row.get("embedding"):
                    continue
                vec = np.frombuffer(row["embedding"], dtype=np.float32)
                if dim is None:
                    dim = vec.shape[0]
                elif vec.shape[0] != dim:
                    raise RuntimeError("索引中存在维度不一致的向量(可能模型被更换), 请强制重建")
                vectors.append(vec)
                chunks.append({k: row[k] for k in ("chunk_id", "source", "title", "text")})
            if vectors:
                matrix = np.vstack(vectors).astype(np.float32, copy=False)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                matrix = matrix / norms
        # 整组赋值; asyncio 单线程下中间无 await, 但仍是唯一写入点
        self._chunks = chunks
        self._matrix = matrix
        self._index_identity = identity if identity is not None else self._embedding_identity()
        self._bump_generation()

    @staticmethod
    def _select_top_k(scores: np.ndarray, k: int) -> np.ndarray:
        """取分数最高的 k 个下标(按分数降序), 避免全量排序 O(n log n)。"""
        n = int(scores.shape[0])
        k = max(1, min(int(k), n))
        if k >= n:
            return np.argsort(-scores)
        selected = np.argpartition(-scores, k - 1)[:k]
        return selected[np.argsort(-scores[selected], kind="stable")]

    # ── 检索 ──────────────────────────────────────────────────

    async def search(self, query: str, top_n: int | None = None) -> dict:
        """语义检索: 返回 {query, results: [{title, source, score, text}], ...}。"""
        query = (query or "").strip()
        if not query:
            return {"error": "查询不能为空"}

        # 最多重试一次: await embedding 期间索引/provider 若被替换, 旧查询向量绝不能
        # 与新矩阵混用(同维换向量空间会静默错排)。捕获快照 → await → 校验代次/身份。
        for attempt in range(2):
            provider = self._embedding
            if provider is None:
                return {"error": "知识库服务未初始化"}
            if self._state["indexing"]:
                return {"error": "知识库索引重建中, 请稍后再试", "retryable": True}
            matrix, chunks = self._matrix, self._chunks
            if not self._state["ready"] or matrix is None or not chunks:
                return {"error": self._state["error"] or "知识库尚未建立索引"}
            generation = self._generation
            # 热切换配置后、重建完成前: 用 B 的查询向量和 A 的矩阵算相似度是错的,
            # 宁可返回可重试错误。身份为空(旧索引未记录)时不做该判断。
            if self._index_identity and \
                    self._embedding_identity(provider) != self._index_identity:
                # fail-closed 但不等人工: 冷却期内后台自动重建自愈(#158 N4)。
                self._schedule_identity_recovery()
                return {
                    "error": "embedding 配置已变更, 索引正在重建, 请稍后再试",
                    "retryable": True,
                }

            self._acquire_provider(provider)
            try:
                vec = (await provider.embed([query]))[0]
            except Exception as exc:
                logger.warning(f"知识库查询嵌入失败: {exc}")
                return {"error": f"查询嵌入失败: {exc}"}
            finally:
                self._release_provider(provider)

            if (
                self._generation != generation
                or self._embedding is not provider
                or self._matrix is not matrix
                or self._chunks is not chunks
            ):
                if attempt == 0:
                    logger.info("知识库检索期间索引/provider 已变更, 用新快照重试")
                    continue
                return {
                    "error": "知识库索引在检索期间已更新, 请重试",
                    "retryable": True,
                }
            break

        q = np.asarray(vec, dtype=np.float32)
        if q.shape[0] != matrix.shape[1]:
            return {"error": "查询向量维度与索引不一致(embedding 模型已变更?), 请重建索引"}
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
        scores = matrix @ q

        top_k = max(1, int(self._cfg.search_top_k or 20))
        eff_top_n = max(1, int(top_n or self._cfg.search_top_n or 5))
        k = min(max(top_k, eff_top_n), len(chunks))
        idx = self._select_top_k(scores, k)
        candidates = [(chunks[int(i)], float(scores[int(i)])) for i in idx]

        reranked = False
        # 在途 rerank 也计入 provider 使用: 热切换后旧 rerank 的延迟关闭任务
        # 必须等本次请求结束, 否则 httpx client 被提前关闭会让 rerank 降级(#158 复审)。
        rerank_provider = self._rerank
        if rerank_provider is not None and len(candidates) > 1:
            self._acquire_provider(rerank_provider)
            try:
                ranked = await rerank_provider.rerank(
                    query, [c["text"] for c, _ in candidates], top_n=eff_top_n,
                )
                if ranked:
                    candidates = [
                        (candidates[i][0], score) for i, score in ranked
                    ]
                    reranked = True
            except Exception as exc:
                logger.warning(f"知识库 rerank 失败, 回退 embedding 排序: {exc}")
            finally:
                self._release_provider(rerank_provider)

        max_chars = max(100, int(self._cfg.max_chars_per_result or 800))
        results = [
            {
                "title": chunk["title"],
                "source": chunk["source"],
                "score": round(score, 3),
                "text": chunk["text"][:max_chars],
            }
            for chunk, score in candidates[:eff_top_n]
        ]
        return {
            "query": query,
            "results": results,
            "total_chunks": len(chunks),
            "reranked": reranked,
        }

    # ── 别名精确查询(knowledge_lookup) ────────────────────────

    def _refresh_lookup(self) -> None:
        """从 index.json 重建名称/别名条目(启动与每次索引后调用)。"""
        self._lookup_entries = []
        index_path = Path(self._cfg.source_dir) / "index.json"
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        for kind, key in (("singer", "singers"), ("song", "songs"), ("producer", "producers")):
            entries = data.get(key) or []
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or entry.get("title") or "").strip()
                if not name:
                    continue
                aliases = [str(a) for a in (entry.get("aliases") or []) if str(a).strip()]
                self._lookup_entries.append({
                    "type": kind,
                    "name": name,
                    "en_name": str(entry.get("en_name") or "").strip(),
                    "aliases": aliases,
                    "data": entry,
                })

    def lookup(self, name: str) -> dict:
        """名称/别名精确查询歌手/歌曲/P主档案; 未命中给语义检索提示。"""
        query = (name or "").strip().casefold()
        if not query:
            return {"error": "name 不能为空"}
        if not self._lookup_entries:
            return {
                "error": "档案索引不可用(index.json 缺失或为空)",
                "hint": "可改用 knowledge_search 语义检索",
            }
        matches: list[dict] = []
        for entry in self._lookup_entries:
            keys = {entry["name"].casefold(), entry["en_name"].casefold()}
            keys.update(a.casefold() for a in entry["aliases"])
            keys.discard("")
            if query in keys:
                matches.append(entry)
        if not matches:
            for entry in self._lookup_entries:
                if query in entry["name"].casefold():
                    matches.append(entry)
        matches = matches[:3]
        if not matches:
            return {
                "error": f"未找到「{name}」对应的歌手/歌曲/创作者",
                "hint": "别名未命中时可改用 knowledge_search 语义检索",
            }
        return {
            "query": name,
            "matches": [
                {"type": m["type"], **m["data"]} for m in matches
            ],
            "hint": "如需更多细节(正文条目/相关歌曲), 可用 knowledge_search 检索",
        }

    # ── 状态与热同步 ──────────────────────────────────────────

    async def service_status(self) -> dict:
        chunk_count = 0
        file_count = 0
        if self._store is not None:
            try:
                chunk_count = await asyncio.to_thread(self._store.count_chunks)
                file_count = len(await asyncio.to_thread(self._store.list_file_states))
            except Exception:
                pass
        return {
            "enabled": True,
            "configured": True,
            "ready": self._state["ready"],
            "indexing": self._state["indexing"],
            "error": self._state["error"],
            "chunks": chunk_count,
            "files": file_count,
            "last_indexed_at": self._state["last_indexed_at"],
            "last_stats": self._state["last_stats"],
            "embedding_model": self._cfg.embedding_model,
            "embedding_base_url": self._cfg.embedding_base_url,
            "rerank_model": self._cfg.rerank_model or "",
            "rerank_enabled": self._rerank is not None,
            "source_dir": self._cfg.source_dir,
        }

    async def sync_config(self, config) -> None:
        """WebUI 热同步: 原位拷贝字段(保持与 main 共享配置同一对象),
        provider 相关字段变更时重建客户端; embedding 模型变更时后台强制重建索引。"""
        from dataclasses import fields as _fields
        for f in _fields(config):
            setattr(self._cfg, f.name, getattr(config, f.name))
        if self._providers_builtin and self._provider_signature() != self._active_signature:
            old_embedding, old_rerank = self._embedding, self._rerank
            embedding_key = (
                (config.embedding_api_key or "").strip()
                or os.environ.get(_EMBEDDING_KEY_ENV, "").strip()
                or self._fallback_api_key
            )
            base_url = (config.embedding_base_url or "").strip()
            if base_url and (config.embedding_model or "").strip() and embedding_key:
                self._embedding = EmbeddingProvider(
                    base_url=base_url, api_key=embedding_key,
                    model=config.embedding_model,
                )
                self._rerank = self._build_rerank(embedding_key, base_url)
                self._active_signature = self._provider_signature()
                # 在途 search 可能正拿着旧 provider 的查询向量: 立即失效快照。
                self._bump_generation()
                # 在途调用可能还持有旧客户端: 保留到 close 统一回收(同 LLMService 约定)
                self._retire_providers(old_embedding, old_rerank)
            else:
                logger.warning("知识库配置变更后缺少 base_url/model/api_key, 保留原 provider")

        store = self._store
        stored_model = stored_fingerprint = None
        if store is not None:
            try:
                stored_model = await asyncio.to_thread(store.get_meta, "embedding_model")
                stored_fingerprint = await asyncio.to_thread(
                    store.get_meta, "embedding_fingerprint",
                )
            except Exception:
                stored_model = stored_fingerprint = None

        declared_model = str(config.embedding_model or "").strip()
        if self._providers_builtin:
            # 重建指纹 = 实际服务来源 + 模型: 同名模型换端点(向量空间不同)也必须重建,
            # 不能只看配置里的模型名。
            current_fingerprint = self._identity_fingerprint(self._embedding_identity())
            # 缺失身份的旧索引不能仅凭模型同名跳过重建；key-only 轮换不改变
            # (model, endpoint)，因此已有正确指纹的向量仍可直接复用。
            need_reindex = (
                self._embedding is not None
                and stored_fingerprint != current_fingerprint
            )
        else:
            # 注入 provider(测试/外部装配): 保持按声明模型变更触发重建的既有语义
            need_reindex = declared_model != (stored_model or "")
        if need_reindex:
            self._schedule_model_reindex()

    def _schedule_identity_recovery(self) -> None:
        """检索发现 provider 身份与索引不符时, 后台按冷却时间重建自愈(#158 N4)。

        本次检索仍 fail-closed 返回可重试错误, 但不再要求人工重建: 冷却期内只
        排队一次后台 force reindex, 重建成功后重试检索即可恢复。重建失败的错误
        记录在 service_status()["error"], 冷却到期后的新检索会再次触发。
        """
        if self._state["indexing"]:
            return
        now = time.monotonic()
        if now - self._identity_rebuild_at < _IDENTITY_REBUILD_COOLDOWN:
            return
        self._identity_rebuild_at = now
        logger.info("知识库 provider 身份与索引不符, 后台触发重建自愈")
        self._schedule_model_reindex()

    def _schedule_model_reindex(self) -> None:
        """embedding 模型变更后的强制重建(受监管后台任务; 关闭期静默放弃)。

        失败按退避重试若干次(#158 N4): 重建期间网络/额度抖动不再永久停在
        fail-closed 状态; 仍失败则错误写入 state["error"], 下次热同步/自愈会再试。
        """

        async def _run() -> None:
            logger.info("后台重建知识库索引")
            for attempt in range(_REINDEX_RETRY_ATTEMPTS):
                stats = await self.reindex(force=True)
                if not stats.get("error"):
                    return
                if attempt + 1 >= _REINDEX_RETRY_ATTEMPTS:
                    logger.warning(
                        "知识库索引重建失败(已尝试 {} 次): {}",
                        _REINDEX_RETRY_ATTEMPTS, stats.get("error"),
                    )
                    return
                await asyncio.sleep(_REINDEX_RETRY_DELAY * (2 ** attempt))

        if self._supervisor is not None:
            try:
                self._supervisor.create_task(
                    _run(), name="knowledge-reindex", owner="knowledge",
                )
                return
            except Exception:
                pass
        asyncio.create_task(_run())
