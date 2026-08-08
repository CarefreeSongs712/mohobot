"""向量存储 — 移植自 Agent-LuoTianyi (src/system/database/vector_store.py)。

使用 ChromaDB PersistentClient + OpenAI 兼容 embedding 接口。
若 chromadb 未安装或未配置 api_key,自动降级为"空存储"(检索返回空,
写入仅记录到数据库正本),保证主流程不被阻断。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


class BaseDocument(ABC):
    content: str = ""
    id: Optional[str] = None
    metadata: Dict[str, Any] = {}

    @abstractmethod
    def get_content(self) -> str:
        pass

    @abstractmethod
    def get_metadata(self) -> Dict[str, Any]:
        pass


class Document(BaseDocument):
    def __init__(self, content: str, metadata: Dict, id: Optional[str] = None):
        self.content = content
        self.metadata = metadata
        if "user_id" not in self.metadata:
            raise ValueError("文档的metadata中必须包含'user_id'字段")
        self.id = id

    def get_content(self) -> str:
        return self.content

    def get_metadata(self) -> Dict[str, Any]:
        return self.metadata


class VectorStore(ABC):
    @abstractmethod
    def add_documents(self, documents: List[BaseDocument]) -> List[str]:
        pass

    @abstractmethod
    async def search(self, user_id: str, query: str, k: int = 5, **kwargs) -> List[Tuple[BaseDocument, float]]:
        pass

    @abstractmethod
    def delete_documents(self, doc_ids: List[str]) -> bool:
        pass

    @abstractmethod
    def delete_user_records(self, user_id: str) -> int:
        pass


class EmptyVectorStore(VectorStore):
    """无 Chroma/无 embedding 配置时的降级实现: 检索为空,写入 no-op。"""

    def __init__(self, config: Dict[str, Any] | None = None):
        self._config = config or {}
        logger.warning("Vector store DEGRADED to EmptyVectorStore (chromadb missing or not configured)")

    def add_documents(self, documents: List[BaseDocument]) -> List[str]:
        return [str(uuid.uuid4()) for _ in documents]

    async def search(self, user_id: str, query: str, k: int = 5, **kwargs) -> List[Tuple[BaseDocument, float]]:
        return []

    def delete_documents(self, doc_ids: List[str]) -> bool:
        return True

    def delete_user_records(self, user_id: str) -> int:
        return 0


class ChromaVectorStore(VectorStore):
    """ChromaDB 实现 (Native PersistentClient)。"""

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self.persist_directory = (
            config.get("vector_store_path")
            or config.get("persist_dir")
            or "./data/database/vector_store"
        )
        os.makedirs(self.persist_directory, exist_ok=True)
        self.collection_name = config.get("collection_name", "mohobot_memory")

        # embedding 配置兼容两种写法:
        #   embedding_model: "BAAI/bge-large-zh-v1.5" (字符串)
        #   embedding_model: {model, api_key, base_url} (字典, 洛天依风格)
        emb_cfg = config.get("embedding_model", {})
        if isinstance(emb_cfg, dict):
            self.embedding_model = emb_cfg.get("model", "BAAI/bge-large-zh-v1.5")
            self.embedding_api_key = emb_cfg.get("api_key", "")
            self.embedding_base_url = emb_cfg.get("base_url", "https://api.siliconflow.cn/v1")
        else:
            self.embedding_model = str(emb_cfg or "BAAI/bge-large-zh-v1.5")
            self.embedding_api_key = config.get("embedding_api_key", "")
            self.embedding_base_url = config.get(
                "embedding_base_url", "https://api.siliconflow.cn/v1",
            )

        max_workers = int(config.get("vector_store_threads", 4))
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chroma")

        import chromadb
        from chromadb.config import Settings

        self._client = chromadb.PersistentClient(
            path=self.persist_directory,
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=_OpenAICompatEmbedding(
                model=self.embedding_model,
                base_url=self.embedding_base_url,
                api_key=self.embedding_api_key,
            ),
            metadata={"description": "Mohobot Memory"},
        )
        logger.info(f"Chroma vector store ready: {self.collection_name}")

    def add_documents(self, documents: List[BaseDocument]) -> List[str]:
        for doc in documents:
            if "user_id" not in doc.get_metadata():
                raise ValueError("文档的metadata中必须包含'user_id'字段")
        ids = [str(uuid.uuid4()) for _ in documents]
        self._collection.add(
            documents=[doc.get_content() for doc in documents],
            metadatas=[doc.get_metadata() for doc in documents],
            ids=ids,
        )
        return ids

    async def search(self, user_id: str, query: str, k: int = 5, **kwargs) -> List[Tuple[BaseDocument, float]]:
        try:
            def _do_query():
                return self._collection.query(
                    query_texts=[query],
                    n_results=k,
                    where={"user_id": user_id} if "where" not in kwargs else kwargs.get("where"),
                )

            results = await asyncio.get_event_loop().run_in_executor(self._executor, _do_query)
            search_results: List[Tuple[BaseDocument, float]] = []
            if results.get("ids"):
                ids = results["ids"][0]
                documents = results["documents"][0]
                metadatas = results["metadatas"][0]
                distances = results["distances"][0]
                for i in range(len(ids)):
                    doc = Document(documents[i], metadatas[i], id=ids[i])
                    score = 1.0 / (1.0 + distances[i])
                    search_results.append((doc, score))
            return search_results
        except Exception as e:
            logger.error(f"Chroma search failed: {e}")
            return []

    def delete_documents(self, doc_ids: List[str]) -> bool:
        try:
            self._collection.delete(ids=doc_ids)
            return True
        except Exception as e:
            logger.error(f"Chroma delete failed: {e}")
            return False

    def delete_user_records(self, user_id: str) -> int:
        try:
            results = self._collection.query(
                query_texts=[" "],
                where={"user_id": user_id},
                n_results=10000,
            )
            if results.get("ids"):
                doc_ids = results["ids"][0]
                if doc_ids:
                    self._collection.delete(ids=doc_ids)
                return len(doc_ids)
            return 0
        except Exception as e:
            logger.error(f"Chroma delete_user_records failed: {e}")
            return 0


class _OpenAICompatEmbedding:
    """OpenAI 兼容 embedding 函数(Chroma EmbeddingFunction 接口)。"""

    def __init__(self, model: str, base_url: str, api_key: str):
        import chromadb.utils.embedding_functions as ef
        self._impl = ef.OpenAIEmbeddingFunction(
            api_key=api_key,
            model_name=model,
            api_base=base_url,
        )

    def __call__(self, input):
        return self._impl(input)


def create_vector_store(config: Dict[str, Any] | None) -> VectorStore:
    """根据配置创建向量存储;不可用时降级为空实现。"""
    config = config or {}
    try:
        emb_cfg = config.get("embedding_model", {})
        if isinstance(emb_cfg, dict):
            has_embedding_key = bool(emb_cfg.get("api_key") or config.get("embedding_api_key"))
        else:
            has_embedding_key = bool(config.get("embedding_api_key"))
        if has_embedding_key and config.get("enabled", True):
            return ChromaVectorStore(config)
        logger.warning("Vector store: no embedding api_key configured, using EmptyVectorStore")
        return EmptyVectorStore(config)
    except Exception as e:
        logger.error(f"Vector store init failed ({e}), using EmptyVectorStore")
        return EmptyVectorStore(config)
