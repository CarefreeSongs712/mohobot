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
from typing import Any, Optional

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


class KnowledgeService:
    """虚拟歌手知识库(RAG)服务。"""

    def __init__(
        self,
        config,
        fallback_api_key: str = "",
        embedding_provider: EmbeddingProvider | None = None,
        rerank_provider: RerankProvider | None = None,
    ):
        self._cfg = config
        self._fallback_api_key = (fallback_api_key or "").strip()
        # provider 注入点(测试用); 缺省 startup 时按配置构建
        self._embedding = embedding_provider
        self._rerank = rerank_provider
        self._providers_builtin = embedding_provider is None
        self._store: KnowledgeIndexStore | None = None
        self._lock = asyncio.Lock()          # reindex 互斥
        self._chunks: list[dict] = []        # 与矩阵行一一对应
        self._matrix: np.ndarray | None = None
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
        await asyncio.to_thread(self._store.open)
        await asyncio.to_thread(self._refresh_lookup)
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

    async def close(self) -> None:
        self._state["ready"] = False
        if self._embedding is not None and self._providers_builtin:
            try:
                await self._embedding.close()
            except Exception:
                pass
        if self._rerank is not None and self._providers_builtin:
            try:
                await self._rerank.close()
            except Exception:
                pass
        if self._store is not None:
            await asyncio.to_thread(self._store.close)
        self._embedding = self._rerank = None
        self._store = None
        self._matrix = None
        self._chunks = []

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

        stored = await asyncio.to_thread(self._store.list_file_states)
        stored_model = await asyncio.to_thread(self._store.get_meta, "embedding_model")
        model_changed = stored_model is not None and stored_model != self._cfg.embedding_model

        # 待重嵌文件: 新文件 / mtime+size 变化 / 强制 / embedding 模型变更
        changed = [
            path for path in file_states
            if force or model_changed or path not in stored
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
                vectors = await embed_in_batches(
                    self._embedding,
                    [f"{c.title}\n{c.text}" for _, c in pending],
                )
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
                        self._cfg.embedding_model, indexed_at,
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
                            self._cfg.embedding_model, _now(),
                        )
        for path in removed:
            await asyncio.to_thread(self._store.remove_file, path)

        await asyncio.to_thread(self._store.set_meta, "embedding_model", self._cfg.embedding_model)
        await asyncio.to_thread(self._store.set_meta, "last_indexed_at", _now())
        await asyncio.to_thread(self._refresh_lookup)
        await self._load_matrix()

        stats = {
            "files": len(file_states),
            "chunks": len(self._chunks),
            "reembedded_files": len(changed),
            "embedded_chunks": embedded_count,
            "removed_files": len(removed),
            "elapsed_sec": round(time.monotonic() - started, 2),
            "model_changed": model_changed,
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

    async def _load_matrix(self) -> None:
        """全量载入 chunks 并构建归一化向量矩阵(行内积即余弦相似度)。"""
        rows = await asyncio.to_thread(self._store.load_chunks)
        if not rows:
            self._chunks, self._matrix = [], None
            return
        dim = None
        vectors: list[np.ndarray] = []
        chunks: list[dict] = []
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
        if not vectors:
            self._chunks, self._matrix = [], None
            return
        matrix = np.vstack(vectors).astype(np.float32, copy=False)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._matrix = matrix / norms
        self._chunks = chunks

    # ── 检索 ──────────────────────────────────────────────────

    async def search(self, query: str, top_n: int | None = None) -> dict:
        """语义检索: 返回 {query, results: [{title, source, score, text}], ...}。"""
        if self._embedding is None:
            return {"error": "知识库服务未初始化"}
        query = (query or "").strip()
        if not query:
            return {"error": "查询不能为空"}
        if self._state["indexing"]:
            return {"error": "知识库索引重建中, 请稍后再试"}
        if not self._state["ready"] or self._matrix is None or not self._chunks:
            return {"error": self._state["error"] or "知识库尚未建立索引"}

        try:
            vec = (await self._embedding.embed([query]))[0]
        except Exception as exc:
            logger.warning(f"知识库查询嵌入失败: {exc}")
            return {"error": f"查询嵌入失败: {exc}"}
        q = np.asarray(vec, dtype=np.float32)
        if q.shape[0] != self._matrix.shape[1]:
            return {"error": "查询向量维度与索引不一致(embedding 模型已变更?), 请重建索引"}
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
        scores = self._matrix @ q

        top_k = max(1, int(self._cfg.search_top_k or 20))
        eff_top_n = max(1, int(top_n or self._cfg.search_top_n or 5))
        k = min(max(top_k, eff_top_n), len(self._chunks))
        idx = np.argsort(-scores)[:k]
        candidates = [(self._chunks[int(i)], float(scores[int(i)])) for i in idx]

        reranked = False
        if self._rerank is not None and len(candidates) > 1:
            try:
                ranked = await self._rerank.rerank(
                    query, [c["text"] for c, _ in candidates], top_n=eff_top_n,
                )
                if ranked:
                    candidates = [
                        (candidates[i][0], score) for i, score in ranked
                    ]
                    reranked = True
            except Exception as exc:
                logger.warning(f"知识库 rerank 失败, 回退 embedding 排序: {exc}")

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
            "total_chunks": len(self._chunks),
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
                for provider in (old_embedding, old_rerank):
                    if provider is not None:
                        try:
                            await provider.close()
                        except Exception:
                            pass
            else:
                logger.warning("知识库配置变更后缺少 base_url/model/api_key, 保留原 provider")
        if config.embedding_model != (await asyncio.to_thread(
                self._store.get_meta, "embedding_model") if self._store else None):
            asyncio.create_task(self._reindex_after_model_change())

    async def _reindex_after_model_change(self) -> None:
        logger.info("embedding 模型已变更, 后台重建知识库索引")
        await self.reindex(force=True)
