"""embedding / rerank API 客户端(硅基流动等 OpenAI 兼容供应商)。

- embedding: OpenAI 兼容 /v1/embeddings, 用 openai SDK, 分批请求。
- rerank: /rerank 不是 OpenAI 标准接口(硅基流动/Jina/Cohere 各家同形),
  用 httpx 直调 POST {base_url}/rerank, 响应 results[].index/relevance_score。

两个 provider 只做传输, 不做重试/降级 — 失败向上抛, 由 service
决定降级路径(rerank 失败回退 embedding 排序; embedding 失败整个
检索/索引动作失败并在工具层转成错误 JSON)。
"""

from __future__ import annotations

import asyncio
from typing import Sequence

_CHUNK_BATCH = 16  # 单次 embeddings 请求的文本条数上限(控制单请求体积)


class EmbeddingProvider:
    """OpenAI 兼容 embedding 客户端。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        from openai import AsyncOpenAI
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入; 返回与输入等长的向量列表(乱序响应按 index 还原)。"""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _CHUNK_BATCH):
            batch = list(texts[start:start + _CHUNK_BATCH])
            resp = await self._client.embeddings.create(model=self.model, input=batch)
            ordered = sorted(resp.data, key=lambda d: d.index)
            if len(ordered) != len(batch):
                raise RuntimeError(
                    f"embedding 响应数量不符: 期望 {len(batch)}, 得到 {len(ordered)}"
                )
            vectors.extend(item.embedding for item in ordered)
        return vectors

    async def close(self) -> None:
        await self._client.close()


class RerankProvider:
    """/rerank 重排客户端(httpx 直调, 非 OpenAI 标准接口)。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        import httpx
        self.model = model
        self._endpoint = base_url.rstrip("/") + "/rerank"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout,
        )

    async def rerank(
        self, query: str, documents: Sequence[str], top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        """返回 [(文档下标, 相关性得分)] 按得分降序。"""
        payload: dict = {"model": self.model, "query": query, "documents": list(documents)}
        if top_n:
            payload["top_n"] = top_n
        resp = await self._client.post(self._endpoint, json=payload)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return [
            (int(item["index"]), float(item.get("relevance_score") or 0.0))
            for item in results
            if isinstance(item, dict) and "index" in item
        ]

    async def close(self) -> None:
        await self._client.aclose()


async def embed_in_batches(
    provider: EmbeddingProvider, texts: Sequence[str], batch_size: int = 64,
) -> list[list[float]]:
    """大批量嵌入: 按 batch_size 分组顺序调用, 组间让出事件循环。

    索引重建一次可能几百块, 一次全量提交体积过大; 这里在 provider
    内部小批(_CHUNK_BATCH)之上再做大组切分, 便于失败定位与内存平稳。
    """
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        if start:
            await asyncio.sleep(0)
        vectors.extend(await provider.embed(texts[start:start + batch_size]))
    return vectors
