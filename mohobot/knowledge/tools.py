"""知识库 LLM 工具 — knowledge_search / knowledge_lookup。

注册进全局 LLMToolRegistry(与 plugins/song_tools 同机制)。服务实例
由 main.py 在装配成功后注入(_service 持有); 进程内重启换实例时
handler 每次调用现取 _service, 无需重挂; 服务降级/禁用时由 main
调用 unregister_knowledge_tools() 摘除, 工具不再对模型广告。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from mohobot.services.llm_tools import LLMTool, registry, tool_schema

_service: Optional[Any] = None
_REGISTERED = False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def knowledge_search(query: str, top_k: int = 5) -> str:
    """在虚拟歌手知识库中语义检索相关知识片段。"""
    if _service is None:
        return _json({"error": "知识库未启用"})
    query = str(query or "").strip()
    if len(query) < 2:
        return _json({"error": "query 至少需要 2 个字符"})
    try:
        top_k = max(1, min(int(top_k or 5), 8))
    except (TypeError, ValueError):
        top_k = 5
    return _json(await _service.search(query, top_n=top_k))


async def knowledge_lookup(name: str) -> str:
    """按名字或别名精确查询虚拟歌手/歌曲/创作者档案。"""
    if _service is None:
        return _json({"error": "知识库未启用"})
    return _json(_service.lookup(str(name or "")))


_TOOLS = [
    LLMTool(
        tool_schema(
            "knowledge_search",
            "在中文虚拟歌手知识库中语义检索(歌手条目/代表名曲/P主/公司引擎/"
            "历史大事记等)。回答虚拟歌手相关问题时优先使用; 每块结果含标题、"
            "来源文件与正文截断, 需要更多细节可换关键词再检索。",
            {
                "query": {"type": "string", "description": "检索查询, 简洁明确的主题词或问题"},
                "top_k": {"type": "integer", "description": "返回结果数量, 1-8, 默认 5"},
            },
            ["query"],
        ),
        knowledge_search,
    ),
    LLMTool(
        tool_schema(
            "knowledge_lookup",
            "按名字或别名精确查询中文虚拟歌手档案(歌手/歌曲/P主), "
            "返回结构化资料(引擎沿革/运营方/声源/代表曲等)。"
            "支持别名, 如「洛神」「404」「土妹妹」「小天使」。",
            {
                "name": {"type": "string", "description": "歌手/歌曲/P主的名字或常见别名"},
            },
            ["name"],
        ),
        knowledge_lookup,
    ),
]


def register_knowledge_tools(service) -> None:
    """注入服务实例并注册工具(幂等; 换服务实例无需重挂)。"""
    global _service, _REGISTERED
    _service = service
    if _REGISTERED:
        return
    for tool in _TOOLS:
        registry.register(tool)
    _REGISTERED = True


def unregister_knowledge_tools() -> None:
    """摘除工具(服务禁用/降级时调用; 幂等)。"""
    global _service, _REGISTERED
    if _REGISTERED:
        for tool in _TOOLS:
            registry.unregister(tool.name)
    _REGISTERED = False
    _service = None
