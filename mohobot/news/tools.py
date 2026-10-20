"""新闻库 LLM 工具 — news_recent / news_search。

与 knowledge/tools.py 同机制: 注册进全局 LLMToolRegistry, 服务实例由
main.py 注入; 禁用时摘除工具。

**只读**: 工具只能查询已审核通过的条目, 不能提交或审核(提交/审核属
管理员与采集器职责, 避免模型自行把内容写进知识面)。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from mohobot.services.llm_tools import LLMTool, registry, tool_schema

_service: Optional[Any] = None
_REGISTERED = False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def set_news_service(service: Any) -> None:
    global _service
    _service = service


async def news_recent(limit: int = 3) -> str:
    """获取最近已审核的相关动态/新闻。"""
    if _service is None:
        return _json({"error": "新闻库未启用"})
    try:
        limit = max(1, min(int(limit or 3), 8))
    except (TypeError, ValueError):
        limit = 3
    items = await _service.recent_news(limit=limit)
    return _json({
        "items": [
            {
                "title": i.title,
                "summary": i.content[:300],
                "source": i.author_name or i.source,
                "url": i.url,
                "published_at": i.published_at,
                "tags": i.tags,
            }
            for i in items
        ],
        "note": "仅含管理员已审核通过且未过期的条目" if items else "暂无已审核动态",
    })


async def news_search(keywords: str, limit: int = 3) -> str:
    """按关键词检索已审核的动态/新闻。"""
    if _service is None:
        return _json({"error": "新闻库未启用"})
    try:
        limit = max(1, min(int(limit or 3), 8))
    except (TypeError, ValueError):
        limit = 3
    keys = [k for k in str(keywords or "").replace("，", ",").split(",") if k.strip()]
    if not keys:
        return _json({"error": "keywords 不能为空"})
    items = await _service.search_news(keys, limit=limit)
    return _json({
        "items": [
            {
                "title": i.title,
                "summary": i.content[:300],
                "source": i.author_name or i.source,
                "url": i.url,
                "published_at": i.published_at,
            }
            for i in items
        ],
        "keywords": keys,
    })


async def news_submit(
    title: str,
    content: str = "",
    source_url: str = "",
    author: str = "",
) -> str:
    """把聊天中出现的、值得记录的动态/新闻提交为**待审**条目。

    仅供"用户在聊天里提到了一条近期动态/资讯"时记录线索; 提交后仍需管理员
    在 WebUI 审核通过才会被注入上下文或工具检索到。
    """
    if _service is None:
        return _json({"error": "新闻库未启用"})
    title = str(title or "").strip()
    if not title:
        return _json({"error": "title 不能为空"})
    try:
        item = await _service.submit(
            title,
            content=str(content or "").strip(),
            source="chat_extracted",
            url=str(source_url or "").strip(),
            author_name=str(author or "").strip(),
            tags=["聊天提取"],
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"error": f"提交失败: {exc}"})
    return _json({
        "status": "pending_review",
        "id": item.id,
        "note": "已提交管理员审核；审核通过前不会出现在上下文或检索结果中",
    })


_TOOLS = [
    LLMTool(
        tool_schema(
            "news_recent",
            "获取虚拟歌手/官方账号最近的相关动态与新闻(已由管理员审核)。"
            "当用户问「最近有什么新消息/新歌/官号发了什么」或需要聊近期热点时使用。",
            {"limit": {"type": "integer", "description": "返回条数, 1-8, 默认 3"}},
            [],
        ),
        news_recent,
    ),
    LLMTool(
        tool_schema(
            "news_submit",
            "把聊天中提到的近期动态/新闻记录为待审条目（不会立即生效，需管理员审核）。"
            "仅当用户明确提到值得记录的真实资讯时使用；不要凭猜测编造新闻。",
            {
                "title": {"type": "string", "description": "一句话标题/核心事实"},
                "content": {"type": "string", "description": "可选。补充细节或原文摘录"},
                "source_url": {"type": "string", "description": "可选。来源链接"},
                "author": {"type": "string", "description": "可选。发布者名称"},
            },
            ["title"],
        ),
        news_submit,
    ),
    LLMTool(
        tool_schema(
            "news_search",
            "按关键词检索已审核的动态/新闻(如歌名、活动名、UP 主名)。",
            {
                "keywords": {"type": "string", "description": "关键词, 多个用逗号分隔"},
                "limit": {"type": "integer", "description": "返回条数, 1-8, 默认 3"},
            },
            ["keywords"],
        ),
        news_search,
    ),
]


def register_news_tools() -> None:
    """注册工具(幂等: 进程内重启/重复装配不会因重名报错)。"""
    global _REGISTERED
    for tool in _TOOLS:
        if registry.contains(tool.name):
            registry.unregister(tool.name)
        registry.register(tool)
    _REGISTERED = True


def unregister_news_tools() -> None:
    global _REGISTERED
    for tool in _TOOLS:
        registry.unregister(tool.name)
    _REGISTERED = False
