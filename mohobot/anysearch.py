"""Anysearch 实时联网搜索客户端 — 移植自 astrbot_plugin_anysearch (client.py)。

直接调用 Anysearch MCP API (https://api.anysearch.com/mcp, JSON-RPC):
- search: 通用网页搜索(支持 freshness/content_types 垂直检索)
- batch_search: 批量查询(1-5 个)
- extract: 网页正文提取
使用 httpx(项目既有依赖)替代 aiohttp。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger


class AnySearchError(RuntimeError):
    """Raised when AnySearch returns an HTTP or JSON-RPC error."""


class AnySearchClient:
    endpoint = "https://api.anysearch.com/mcp"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        timeout: int = 30,
        http_client_factory=None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        if base_url and base_url.strip():
            self.endpoint = base_url.strip().rstrip("/")
        self.timeout = timeout
        # 可注入的 HTTP 客户端工厂(测试用); 默认 httpx.AsyncClient
        self._http_client_factory = http_client_factory or httpx.AsyncClient

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        text = ""
        try:
            async with self._http_client_factory(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    self.endpoint,
                    json=payload,
                    headers=self._headers(),
                )
                text = response.text
                if response.status_code >= 400:
                    raise AnySearchError(
                        f"AnySearch HTTP {response.status_code}: {text[:500]}"
                    )
        except AnySearchError:
            raise
        except httpx.TimeoutException as exc:
            raise AnySearchError("AnySearch request timed out.") from exc
        except httpx.HTTPError as exc:
            raise AnySearchError(f"AnySearch connection failed: {exc}") from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AnySearchError(f"AnySearch returned invalid JSON: {text[:500]}") from exc

        if "error" in data:
            error = data["error"]
            if isinstance(error, dict):
                message = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                message = str(error)
            raise AnySearchError(f"AnySearch API error: {message}")

        result = data.get("result", {})
        content = result.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return str(item.get("text", ""))

        return json.dumps(result, ensure_ascii=False, indent=2)

    async def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        freshness: str = "",
        content_types: list[str] | None = None,
    ) -> str:
        """通用网页搜索。freshness: day/week/month/year; content_types: web/news/doc。"""
        arguments: dict[str, Any] = {"query": query}
        if max_results is not None:
            arguments["max_results"] = max_results
        if freshness:
            arguments["freshness"] = freshness
        if content_types:
            arguments["content_types"] = content_types
        return await self._call("search", arguments)

    async def batch_search(self, queries: list[dict[str, Any]]) -> str:
        """批量搜索: [{"query": "...", "max_results": N}, ...] 最多 5 个。"""
        return await self._call("batch_search", {"queries": queries})

    async def extract(self, url: str) -> str:
        """提取网页正文。"""
        return await self._call("extract", {"url": url})

    async def safe_search(self, query: str, max_results: int = 5) -> str:
        """供回复流水线使用的容错搜索: 失败返回空串(降级, 不阻断回复)。"""
        try:
            return await self.search(query, max_results=max_results)
        except AnySearchError as e:
            logger.warning(f"AnySearch 搜索降级({query[:30]}...): {e}")
            return ""
