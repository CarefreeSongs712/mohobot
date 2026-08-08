"""Mohobot 实时联网搜索插件 — 直接调用 Anysearch API。

用法:
  /搜索 关键词           通用网页搜索
  /搜索 extract URL      网页正文提取
  /搜索 batch 词1,词2    批量搜索(最多 5 个)
  /搜索 help             查看用法

API Key 在 config/global.yaml 的 anysearch.api_key 配置(main.py 注入)。
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger


class Plugin:
    """Anysearch 实时联网搜索: 通用搜索 / 正文提取 / 批量搜索。"""

    info = {
        "commands": [
            {"name": "搜索", "desc": "实时联网搜索: /搜索 关键词; /搜索 extract URL; /搜索 batch 词1,词2"},
        ],
    }

    _anysearch_client = None

    @classmethod
    def inject_anysearch_client(cls, client) -> None:
        """注入 AnySearchClient(由 main.py 调用; 未配置 key 时为 None)。"""
        cls._anysearch_client = client

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        # 提取纯文本
        text = ""
        if isinstance(event.message, str):
            text = event.message.strip()
        elif isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
            text = text.strip()

        if not (text.startswith("/搜索") or text.startswith("/search")):
            return (False, None)

        client = self._anysearch_client
        if client is None:
            return (True, "实时搜索未配置 — 请在 config/global.yaml 的 anysearch.api_key 填入 API Key 后重启。")

        query = text[len("/搜索"):].strip() if text.startswith("/搜索") else text[len("/search"):].strip()
        if not query:
            return (True, self._usage_text())

        parts = query.split(maxsplit=1)
        action = parts[0].lower() if parts else ""

        if action in {"help", "帮助", "用法"}:
            return (True, self._usage_text())

        if action in {"extract", "网页", "提取", "网页提取"}:
            url = (parts[1] if len(parts) > 1 else "").strip()
            if not url or not self._is_safe_url(url):
                return (True, "用法: /搜索 extract https://example.com/page\n只允许提取公开的 http(s) 网页。")
            try:
                result = await client.extract(url)
            except Exception as e:
                return (True, f"网页提取失败: {e}")
            return (True, self._format_result("📄 网页正文", result))

        if action in {"batch", "批量", "批量搜索"}:
            raw = (parts[1] if len(parts) > 1 else "").strip()
            items = [x.strip() for x in re.split(r"[,\n，]", raw) if x.strip()][:5]
            if not items:
                return (True, "用法: /搜索 batch 关键词1,关键词2,关键词3")
            try:
                result = await client.batch_search(
                    [{"query": q, "max_results": 3} for q in items]
                )
            except Exception as e:
                return (True, f"批量搜索失败: {e}")
            return (True, self._format_result("🔎 批量搜索", result))

        # 普通搜索
        try:
            result = await client.search(query, max_results=5)
        except Exception as e:
            return (True, f"搜索失败: {e}")
        return (True, self._format_result(f"🔎 搜索结果: {query}", result))

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        return bool(re.match(r"^https?://", url, re.IGNORECASE))

    @staticmethod
    def _format_result(title: str, result: str) -> str:
        """搜索结果可能很长, 截断到合理长度。"""
        text = (result or "").strip()
        if not text:
            return f"{title}\n(无结果)"
        if len(text) > 3000:
            text = text[:3000] + "\n…(已截断)"
        return f"{title}\n{text}"

    def _usage_text(self) -> str:
        return (
            "Anysearch 实时联网搜索用法:\n"
            "/搜索 关键词\n"
            "/搜索 extract https://example.com/page\n"
            "/搜索 batch 关键词1,关键词2,关键词3\n"
            "/搜索 help"
        )
