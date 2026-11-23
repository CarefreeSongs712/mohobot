"""模型网关：视觉标注、检索改写、文本嵌入，统一走 OpenAI 兼容接口。

每个角色可以在插件配置里单独指定模型 / 地址 / 密钥；留空时回退到 mohobot
全局 LLM 配置（``config/global.yaml`` 的 ``llm`` 段及 ``MOHOBOT_*_API_KEY``
环境变量），回退规则与 mohobot 自身一致：
- 视觉：vision_model，地址 vision_base_url → chat_base_url，密钥 vision → chat
- 改写：chat_model，chat_base_url / chat 密钥
- 嵌入：mohobot 没有嵌入模型，必须在插件里指定模型名；地址/密钥可回退到 chat

地址与密钥成对回退：插件里填了地址就只用插件里的密钥，避免把全局密钥发给
另一个服务商。
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .log import logger

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class ModelNotConfigured(RuntimeError):
    """该角色没有可用的模型配置。"""


@dataclass(frozen=True)
class ModelEndpoint:
    role: str
    model: str
    base_url: str
    api_key: str
    # 请求维度也是向量空间的一部分：同名同地址但 dimensions 不同，得到的向量
    # 不可混用。endpoint 在解析时快照该值，热切换后旧请求仍按旧维度发送，
    # 不会把新配置的维度带到旧端点（issue #129 / PR #158 审查 N2）。
    dimensions: int = 0

    @property
    def signature(self) -> str:
        host = self.base_url or "default"
        base = f"{self.model}@{host}"
        return f"{base}#{self.dimensions}" if self.dimensions else base


def _image_data_uri(path: str) -> str:
    with open(path, "rb") as handle:
        content = handle.read()
    mime = _MIME_BY_EXT.get(Path(path).suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def _clean(value: Any) -> str:
    return str(value or "").strip()


class ModelGateway:
    """按角色解析模型配置并复用 AsyncOpenAI 客户端。"""

    ROLES = ("vision", "rewrite", "embedding")

    def __init__(
        self,
        config_getter: Callable[[], Any],
        llm_service_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._llm_service_getter = llm_service_getter or (lambda: None)
        self._clients: dict[tuple[str, str], Any] = {}
        self._embedding_dims: dict[str, int] = {}

    # ── 配置解析 ───────────────────────────────────────────

    def _plugin_cfg(self) -> Any:
        return self._config_getter()

    def _global_llm(self) -> Any:
        service = self._llm_service_getter()
        return getattr(getattr(service, "_cfg", None), "llm", None)

    def _global_chat_pair(self, llm: Any) -> tuple[str, str]:
        base = _clean(getattr(llm, "chat_base_url", ""))
        key = _clean(getattr(llm, "chat_api_key", "")) or _clean(
            os.environ.get("MOHOBOT_LLM_API_KEY")
        )
        return base, key

    def endpoint(self, role: str) -> ModelEndpoint | None:
        cfg = self._plugin_cfg()
        llm = self._global_llm()
        chat_base, chat_key = self._global_chat_pair(llm)

        if role == "vision":
            model = _clean(getattr(cfg, "vision_model", "")) or _clean(
                getattr(llm, "vision_model", "")
            )
            base = _clean(getattr(cfg, "vision_base_url", ""))
            if base:
                key = _clean(getattr(cfg, "vision_api_key", ""))
            else:
                base = _clean(getattr(llm, "vision_base_url", "")) or chat_base
                key = (
                    _clean(getattr(llm, "vision_api_key", ""))
                    or _clean(os.environ.get("MOHOBOT_VISION_API_KEY"))
                    or chat_key
                )
        elif role == "rewrite":
            model = _clean(getattr(cfg, "meme_query_rewrite_model", "")) or _clean(
                getattr(llm, "chat_model", "")
            )
            base = _clean(getattr(cfg, "meme_query_rewrite_base_url", ""))
            key = _clean(getattr(cfg, "meme_query_rewrite_api_key", "")) if base else chat_key
            base = base or chat_base
        elif role == "embedding":
            model = _clean(getattr(cfg, "embedding_model", ""))
            base = _clean(getattr(cfg, "embedding_base_url", ""))
            key = _clean(getattr(cfg, "embedding_api_key", "")) if base else chat_key
            base = base or chat_base
        else:
            raise ValueError(f"unknown model role: {role}")

        if not model:
            return None
        dimensions = self._embedding_dimensions() if role == "embedding" else 0
        return ModelEndpoint(
            role=role,
            model=model,
            base_url=base,
            api_key=key,
            dimensions=dimensions,
        )

    def describe(self, role: str) -> str:
        endpoint = self.endpoint(role)
        return endpoint.signature if endpoint else ""

    def _client(self, endpoint: ModelEndpoint) -> Any:
        cache_key = (endpoint.base_url, endpoint.api_key)
        client = self._clients.get(cache_key)
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=endpoint.api_key or "EMPTY",
                base_url=endpoint.base_url or None,
            )
            self._clients[cache_key] = client
        return client

    def _require(self, role: str) -> ModelEndpoint:
        endpoint = self.endpoint(role)
        if endpoint is None:
            raise ModelNotConfigured(f"未配置 {role} 模型")
        return endpoint

    async def _record_usage(self, model: str, usage: Any, kind: str) -> None:
        """记入 mohobot 用量统计（面板「数据总览」按模块汇总）；失败静默。"""
        recorder = getattr(self._llm_service_getter(), "_record_usage", None)
        if recorder is None or usage is None:
            return
        try:
            await recorder(model, usage, "", None, module="meme_stealer", kind=kind)
        except Exception as e:
            logger.debug(f"[Stealer] 用量记录失败: {e}")

    # ── 调用 ───────────────────────────────────────────────

    def _vision_max_tokens(self) -> int:
        try:
            return int(getattr(self._global_llm(), "vision_max_tokens", 2048) or 2048)
        except (TypeError, ValueError):
            return 2048

    async def vision(self, prompt: str, image_path: str) -> str:
        endpoint = self._require("vision")
        data_uri = await asyncio.to_thread(_image_data_uri, image_path)
        response = await self._client(endpoint).chat.completions.create(
            model=endpoint.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            max_tokens=self._vision_max_tokens(),
            temperature=0.2,
        )
        await self._record_usage(endpoint.model, getattr(response, "usage", None), "vision")
        choice = response.choices[0] if response.choices else None
        return str(getattr(getattr(choice, "message", None), "content", "") or "").strip()

    async def complete(self, prompt: str, *, role: str = "rewrite", max_tokens: int = 150) -> str:
        endpoint = self._require(role)
        response = await self._client(endpoint).chat.completions.create(
            model=endpoint.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        await self._record_usage(endpoint.model, getattr(response, "usage", None), role)
        choice = response.choices[0] if response.choices else None
        return str(getattr(getattr(choice, "message", None), "content", "") or "").strip()

    def _embedding_dimensions(self) -> int:
        try:
            return max(0, int(getattr(self._plugin_cfg(), "embedding_dimensions", 0) or 0))
        except (TypeError, ValueError):
            return 0

    async def embed(
        self, text: str, *, endpoint: ModelEndpoint | None = None
    ) -> list[float]:
        """调用嵌入接口。

        ``endpoint`` 由调用方捕获时，一律使用该快照，不再重新读取配置；否则
        配置在请求前后被切换会把另一个服务的向量写进当前代次（issue #129）。
        """
        endpoint = endpoint or self._require("embedding")
        kwargs: dict[str, Any] = {"model": endpoint.model, "input": text}
        # 只使用 endpoint 快照里的维度，绝不重新读实时配置：热切换端点与维度
        # 同时发生时，在途旧请求必须仍按旧维度发给旧端点（PR #158 审查 N2）。
        if endpoint.dimensions:
            kwargs["dimensions"] = endpoint.dimensions
        response = await self._client(endpoint).embeddings.create(**kwargs)
        await self._record_usage(endpoint.model, getattr(response, "usage", None), "embedding")
        data = getattr(response, "data", None) or []
        vector = list(getattr(data[0], "embedding", []) or []) if data else []
        if vector:
            self._embedding_dims[endpoint.signature] = len(vector)
        return vector

    def embedding_dim(self, endpoint: ModelEndpoint | None = None) -> int:
        endpoint = endpoint or self.endpoint("embedding")
        if endpoint is None:
            return 0
        # 已记录的实际维度优先，否则用 endpoint 快照的配置维度；不再回退实时
        # 配置，否则旧端点会读出新配置的维度（PR #158 审查 N2）。
        return self._embedding_dims.get(endpoint.signature) or endpoint.dimensions

    def embedding_provider(self) -> "EmbeddingProvider | None":
        endpoint = self.endpoint("embedding")
        return EmbeddingProvider(self, endpoint) if endpoint else None

    async def close(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            try:
                await client.close()
            except Exception as e:
                logger.debug(f"[Stealer] 关闭模型客户端失败: {e}")


class EmbeddingProvider:
    """EmbeddingService 使用的嵌入接口（get_embedding / get_dim）。

    provider 创建时捕获 ``endpoint``，之后嵌入/维度查询都只用这个快照，保证
    写入向量与 provider 签名属于同一个服务来源；配置热切换后旧 provider 不会
    改调新端点（issue #129）。
    """

    def __init__(self, gateway: ModelGateway, endpoint: ModelEndpoint) -> None:
        self._gateway = gateway
        self.endpoint = endpoint
        self.signature = endpoint.signature
        self.provider_config = {"id": endpoint.signature}

    async def get_embedding(self, text: str) -> list[float]:
        return await self._gateway.embed(text, endpoint=self.endpoint)

    def get_dim(self) -> int:
        return self._gateway.embedding_dim(self.endpoint)

    async def probe(self) -> int:
        """首次使用前探测向量维度，供维度变化检测。"""
        if self.get_dim():
            return self.get_dim()
        try:
            await self.get_embedding("维度探测")
        except Exception as e:
            logger.warning(f"[Embedding] 嵌入模型 {self.endpoint.signature} 调用失败: {e}")
        return self.get_dim()
