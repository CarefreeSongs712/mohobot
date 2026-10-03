"""嵌入向量服务：封装模型网关的嵌入接口 + SQLite/numpy 向量库。

- 嵌入模型：插件配置 embedding_model（OpenAI 兼容 /embeddings 接口）
- 向量存储：SQLite emoji_embedding 表保存原始向量（不归一化），检索时在内存中
  做余弦相似度；冗余淘汰与 WebUI 语义空间直接读取原始向量
- 回填：启动时为已有表情补算向量；维度变化时清空重建
"""

from pathlib import Path
from typing import Any

import numpy as np

from ...host import logger

from ..processing.semantic_schema import EMBEDDING_TEXT_VERSION, build_meme_search_text

# 旧版本在 SQLite 降级路径写入的签名，沿用以免重复回填
STORE_SIG = "fallback"
MIN_SEARCH_SIMILARITY = 0.15


class EmbeddingService:
    """嵌入向量服务（SQLite + numpy 单一后端）。"""

    BACKFILL_BATCH_SIZE = 20

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._provider: Any | None = None
        self._provider_dim: int = 0

        # Provider 负缓存：同一配置下未找到时不再重复探测/打印日志
        self._provider_not_found: bool = False
        self._last_enable_embedding_search: bool | None = None
        self._last_embedding_sig: str | None = None

        # 内存矩阵：原始向量 + 归一化副本（检索用）
        self._raw_matrix: np.ndarray | None = None
        self._unit_matrix: np.ndarray | None = None
        self._paths: list[str] = []
        self._dim: int = 0
        self._loaded: bool = False

    # ═══════════════════════════════════════════════════
    #  Provider
    # ═══════════════════════════════════════════════════

    def _embedding_enabled(self) -> bool:
        """读取插件配置中的嵌入检索开关。"""
        return bool(getattr(self.plugin, "enable_embedding_search", False))

    def _current_signature(self) -> str:
        """当前嵌入模型签名（model@地址）；未配置返回空串。"""
        models = getattr(self.plugin, "models", None)
        return models.describe("embedding") if models is not None else ""

    def _reset_provider_state_if_changed(self) -> None:
        """配置开关或嵌入模型变化时重置 provider 探测状态。"""
        current_enable = self._embedding_enabled()
        current_sig = self._current_signature()
        if (
            self._last_enable_embedding_search != current_enable
            or self._last_embedding_sig != current_sig
        ):
            self._provider = None
            self._provider_dim = 0
            self._provider_not_found = False
            self.invalidate_cache()
            self._last_enable_embedding_search = current_enable
            self._last_embedding_sig = current_sig

    def _get_provider(self) -> Any | None:
        """获取嵌入接口；未开启或未配置嵌入模型时返回 None。"""
        if not self._embedding_enabled():
            return None

        self._reset_provider_state_if_changed()

        if self._provider is not None:
            return self._provider
        if self._provider_not_found:
            return None

        models = getattr(self.plugin, "models", None)
        provider = models.embedding_provider() if models is not None else None
        if provider is None:
            logger.info("[Embedding] 未配置嵌入模型（embedding_model），语义检索与冗余淘汰将降级")
            self._provider_not_found = True
            return None
        self._provider = provider
        self._provider_dim = self._get_provider_dim(provider)
        logger.info(f"[Embedding] 使用嵌入模型: {self._current_signature()}")
        return self._provider

    @staticmethod
    def _get_provider_dim(provider: Any) -> int:
        if hasattr(provider, "get_dim"):
            try:
                return int(provider.get_dim())
            except Exception:
                pass
        return 0

    # ═══════════════════════════════════════════════════
    #  可用性 / 初始化
    # ═══════════════════════════════════════════════════

    def is_available(self) -> bool:
        """嵌入检索是否可用（开关打开且找到 provider）。"""
        return self._get_provider() is not None

    async def initialize(self) -> None:
        if not self._embedding_enabled():
            return
        self._remove_legacy_faiss_files()
        await self._upgrade_corpus_if_needed()
        provider = self._get_provider()
        if provider is None:
            logger.info("[Embedding] 未就绪 — 请在插件配置中填写 embedding_model")
            return
        probe = getattr(provider, "probe", None)
        if callable(probe):
            self._provider_dim = int(await probe() or 0)
        self._check_model_signature()
        self._load_matrix()
        self._check_dimension()
        count = len(self._paths)
        logger.info(
            f"[Embedding] 向量库就绪 ✅ ({count} 条, dim={self._dim})"
            if count
            else "[Embedding] 向量库就绪 ✅ — 当前为空，新入库自动填充"
        )

    def _db(self):
        db = getattr(self.plugin, "db_service", None)
        return db if db is not None and hasattr(db, "load_embeddings_by_sig") else None

    def _load_matrix(self) -> None:
        """从 SQLite 加载原始向量；维度以多数派为准。"""
        if self._loaded:
            return
        self._raw_matrix = None
        self._unit_matrix = None
        self._paths = []
        self._dim = 0
        self._loaded = True

        db = self._db()
        if db is None:
            return
        try:
            rows = db.load_embeddings_by_sig(STORE_SIG)
        except Exception as e:
            logger.warning(f"[Embedding] 读取向量失败: {e}")
            return

        by_dim: dict[int, list[tuple[str, np.ndarray]]] = {}
        for r in rows or []:
            blob, path = r.get("vector"), r.get("path")
            if not blob or not path:
                continue
            vec = np.frombuffer(bytes(blob), dtype=np.float32)
            dim = int(r.get("dim", 0) or 0)
            if dim > 0 and len(vec) == dim:
                by_dim.setdefault(dim, []).append((str(path), vec))
        if not by_dim:
            return
        dim = max(by_dim, key=lambda d: len(by_dim[d]))
        items = by_dim[dim]
        raw = np.stack([vec for _, vec in items]).astype(np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        self._raw_matrix = raw
        self._unit_matrix = raw / np.where(norms == 0, 1.0, norms)
        self._paths = [path for path, _ in items]
        self._dim = dim

    def _check_dimension(self) -> bool:
        """provider 维度与已存向量不一致时清空重建。"""
        if self._dim == 0 or self._provider_dim == 0 or self._dim == self._provider_dim:
            return True
        logger.warning(
            f"[Embedding] 维度不匹配: 已存={self._dim}, provider={self._provider_dim}。"
            "旧向量将被清除并重建。"
        )
        db = self._db()
        if db is not None and hasattr(db, "clear_all_embeddings"):
            try:
                db.clear_all_embeddings()
            except Exception as e:
                logger.warning(f"[Embedding] 清除旧向量失败: {e}")
        self.invalidate_cache()
        return False

    def invalidate_cache(self) -> None:
        self._loaded = False
        self._raw_matrix = None
        self._unit_matrix = None
        self._paths = []

    # ═══════════════════════════════════════════════════
    #  文本 / 写入 / 删除
    # ═══════════════════════════════════════════════════

    def _category_info(self) -> dict[str, Any]:
        cfg = getattr(self.plugin, "plugin_config", None)
        info = getattr(cfg, "category_info", None) if cfg else None
        return info if isinstance(info, dict) else {}

    def _character_info(self) -> dict[str, Any]:
        cfg = getattr(self.plugin, "plugin_config", None)
        info = getattr(cfg, "character_info", None) if cfg else None
        return info if isinstance(info, dict) else {}

    def _build_search_text(self, entry: dict[str, Any]) -> str:
        """拼接嵌入文本：图上文字 + 角色 + 描述 + 标签 + 适用场景（+ 人工分类）。"""
        return build_meme_search_text(
            entry,
            category_info=self._category_info(),
            character_info=self._character_info(),
        )

    async def embed_text(self, text: str) -> np.ndarray | None:
        provider = self._get_provider()
        if provider is None or not str(text or "").strip():
            return None
        try:
            vec = await provider.get_embedding(str(text)[:4000])
        except Exception as e:
            logger.debug(f"[Embedding] get_embedding 失败: {e}")
            return None
        if not vec:
            return None
        return np.asarray(vec, dtype=np.float32)

    async def insert_emoji(self, path: str, entry: dict[str, Any]) -> bool:
        """写入单条表情向量；失败不阻塞入库流程。"""
        if not self._embedding_enabled():
            return False
        vec = await self.embed_text(self._build_search_text(entry))
        db = getattr(self.plugin, "db_service", None)
        if vec is None or db is None or not hasattr(db, "upsert_embedding"):
            return False
        try:
            db.upsert_embedding(path, vec.tobytes(), dim=len(vec), model_sig=STORE_SIG)
            self._loaded = False
            return True
        except Exception as e:
            logger.debug(f"[Embedding] upsert_embedding 失败: {e}")
            return False

    async def delete_by_path(self, path: str) -> bool:
        db = getattr(self.plugin, "db_service", None)
        if db is None or not hasattr(db, "delete_embedding"):
            return False
        db.delete_embedding(path)
        self._loaded = False
        return True

    # ═══════════════════════════════════════════════════
    #  读取 / 检索
    # ═══════════════════════════════════════════════════

    def load_vector_map(self) -> dict[str, np.ndarray]:
        """全部原始向量 {path: vector}，不依赖 provider 是否在线。"""
        paths, matrix = self.matrix_snapshot()
        if matrix is None:
            return {}
        return {path: matrix[i] for i, path in enumerate(paths)}

    def matrix_snapshot(self) -> tuple[list[str], np.ndarray | None]:
        """(路径列表, 原始向量矩阵)；向量库未变化时返回同一个矩阵对象，可用于缓存判断。"""
        self._load_matrix()
        return list(self._paths), self._raw_matrix

    async def search(self, query: str, k: int = 80) -> list[tuple[str, float]]:
        """余弦相似度 top-K。Returns: [(path, similarity)]，按相似度降序。"""
        if not query or not query.strip():
            return []
        self._load_matrix()
        if self._unit_matrix is None or not self._paths:
            return []
        qv = await self.embed_text(query[:2000])
        if qv is None:
            return []
        if len(qv) != self._unit_matrix.shape[1]:
            logger.warning(
                f"[Embedding] 维度不匹配: matrix={self._unit_matrix.shape[1]}, query={len(qv)}，"
                "请重建索引"
            )
            return []
        q_norm = float(np.linalg.norm(qv))
        if q_norm == 0:
            return []
        scores = self._unit_matrix @ (qv / q_norm)
        top_idx = np.argsort(scores)[::-1][: max(1, int(k))]
        return [
            (self._paths[int(i)], float(scores[i]))
            for i in top_idx
            if float(scores[i]) >= MIN_SEARCH_SIMILARITY
        ]

    # ═══════════════════════════════════════════════════
    #  回填
    # ═══════════════════════════════════════════════════

    async def backfill_existing(self, batch_size: int | None = None) -> int:
        """为缺少向量的已有表情批量补算向量。"""
        if not self._embedding_enabled() or self._get_provider() is None:
            return 0
        batch_size = batch_size or self.BACKFILL_BATCH_SIZE
        db = getattr(self.plugin, "db_service", None)
        if not db:
            return 0
        try:
            all_paths = db.get_all_paths()
            embedded = set(db.get_all_embedding_paths())
        except Exception as e:
            logger.warning(f"[Embedding] 回填失败 — 无法读取表情列表: {e}")
            return 0
        missing = [p for p in all_paths or [] if p not in embedded]
        if not missing:
            return 0

        logger.info(f"[Embedding] 回填开始: {len(missing)}/{len(all_paths)} 条缺少向量")
        try:
            idx = db.get_index_cache_readonly() if db.count_total() > 0 else {}
        except Exception:
            idx = {}

        written = 0
        for i in range(0, len(missing), batch_size):
            batch_written = 0
            for path in missing[i : i + batch_size]:
                entry = idx.get(path) or {}
                if not entry:
                    try:
                        entry = db.get_emoji(path) or {}
                    except Exception:
                        entry = {}
                if entry and await self.insert_emoji(path, entry):
                    batch_written += 1
            written += batch_written
            if batch_written:
                logger.info(
                    f"[Embedding] 回填进度: {min(i + batch_size, len(missing))}/{len(missing)}"
                )
        self.invalidate_cache()
        logger.info(f"[Embedding] 回填完成: 成功 {written}/{len(missing)}")
        return written

    async def _upgrade_corpus_if_needed(self) -> None:
        """语料格式变更时清掉旧向量，让 backfill 按新文档重建。"""
        db = getattr(self.plugin, "db_service", None)
        if not db or not hasattr(db, "get_meta_value"):
            return
        try:
            current = str(db.get_meta_value("embedding_text_version") or "")
        except Exception:
            current = ""
        if current == EMBEDDING_TEXT_VERSION:
            return

        logger.info(
            f"[Embedding] 语料版本 {current or 'v1'} -> {EMBEDDING_TEXT_VERSION}，重建文本向量"
        )
        if hasattr(db, "clear_all_embeddings"):
            try:
                db.clear_all_embeddings()
            except Exception as e:
                logger.debug(f"[Embedding] 清空 SQLite 向量失败: {e}")
        self.invalidate_cache()
        try:
            db.set_meta_value("embedding_text_version", EMBEDDING_TEXT_VERSION)
        except Exception as e:
            logger.debug(f"[Embedding] 写入语料版本失败: {e}")

    def _check_model_signature(self) -> None:
        """嵌入模型换了（即使维度相同，向量空间也不同）时清空旧向量，由回填重建。"""
        db = getattr(self.plugin, "db_service", None)
        current = self._current_signature()
        if not db or not current or not hasattr(db, "get_meta_value"):
            return
        try:
            stored = str(db.get_meta_value("embedding_model_sig") or "")
        except Exception:
            stored = ""
        if stored == current:
            return
        if stored:
            logger.info(f"[Embedding] 嵌入模型 {stored} -> {current}，重建文本向量")
            if hasattr(db, "clear_all_embeddings"):
                try:
                    db.clear_all_embeddings()
                except Exception as e:
                    logger.warning(f"[Embedding] 清除旧向量失败: {e}")
            self.invalidate_cache()
        try:
            db.set_meta_value("embedding_model_sig", current)
        except Exception as e:
            logger.debug(f"[Embedding] 写入嵌入模型签名失败: {e}")

    def _remove_legacy_faiss_files(self) -> None:
        """旧版本的 Faiss 索引文件已不再使用；向量会回填到 SQLite。"""
        data_dir = getattr(getattr(self.plugin, "plugin_config", None), "data_dir", None)
        if not data_dir:
            return
        for name in ("emoji_faiss.db", "emoji_faiss.index"):
            try:
                (Path(data_dir) / name).unlink(missing_ok=True)
            except OSError:
                pass

    async def close(self) -> None:
        self.invalidate_cache()
