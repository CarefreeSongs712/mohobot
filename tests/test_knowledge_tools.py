"""知识库工具注册回归: 注册/摘除/执行契约(假服务, 无网络)。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.knowledge import tools as knowledge_tools
from mohobot.services.llm_tools import registry


class FakeService:
    def __init__(self):
        self.search_calls = []
        self.lookup_calls = []

    async def search(self, query, top_n=None):
        self.search_calls.append((query, top_n))
        return {
            "query": query,
            "results": [{"title": "洛天依 · 简介", "source": "虚拟歌手/洛天依.md",
                         "score": 0.87, "text": "世界首位中文VOCALOID虚拟歌手。"}],
            "total_chunks": 42, "reranked": False,
        }

    def lookup(self, name):
        self.lookup_calls.append(name)
        return {"query": name, "matches": [{"type": "singer", "name": "洛天依"}], "hint": ""}


async def test_register_execute_unregister():
        # 防御: 若其他用例遗留注册先摘除
        knowledge_tools.unregister_knowledge_tools()
        assert not registry.contains("knowledge_search")
        assert not registry.contains("knowledge_lookup")

        fake = FakeService()
        knowledge_tools.register_knowledge_tools(fake)
        assert registry.contains("knowledge_search")
        assert registry.contains("knowledge_lookup")

        # 工具 schema 描述包含主题与别名示例(供模型判断何时调用)
        schemas = {s["function"]["name"]: s["function"] for s in registry.schemas()}
        assert "虚拟歌手" in schemas["knowledge_search"]["description"]
        assert "洛神" in schemas["knowledge_lookup"]["description"]
        assert schemas["knowledge_search"]["parameters"]["required"] == ["query"]

        # 经 registry.execute 走真实调用契约
        out = json.loads(await registry.execute(
            "knowledge_search", json.dumps({"query": "洛天依是谁", "top_k": 3}),
        ))
        assert fake.search_calls == [("洛天依是谁", 3)]
        assert out["results"][0]["title"] == "洛天依 · 简介"

        out2 = json.loads(await registry.execute(
            "knowledge_lookup", json.dumps({"name": "洛神"}),
        ))
        assert fake.lookup_calls == ["洛神"]
        assert out2["matches"][0]["name"] == "洛天依"

        # 参数校验: query 过短
        out3 = json.loads(await registry.execute(
            "knowledge_search", json.dumps({"query": "洛"}),
        ))
        assert "error" in out3

        # 摘除后不再广告、不再可用
        knowledge_tools.unregister_knowledge_tools()
        assert not registry.contains("knowledge_search")
        assert not registry.contains("knowledge_lookup")
        out4 = json.loads(await registry.execute(
            "knowledge_search", json.dumps({"query": "洛天依"}),
        ))
        assert "未知工具" in out4["error"]


async def test_register_is_idempotent_and_service_swap_takes_effect():
        knowledge_tools.unregister_knowledge_tools()
        fake1, fake2 = FakeService(), FakeService()
        knowledge_tools.register_knowledge_tools(fake1)
        knowledge_tools.register_knowledge_tools(fake2)  # 幂等: 不抛 duplicate
        # handler 现取 _service → 新实例生效, 无需重挂
        out = json.loads(await registry.execute(
            "knowledge_search", json.dumps({"query": "测试查询"}),
        ))
        assert fake2.search_calls and not fake1.search_calls
        assert out["query"] == "测试查询"
        knowledge_tools.unregister_knowledge_tools()


async def test_service_none_returns_error_json():
        knowledge_tools.unregister_knowledge_tools()
        # 未注册服务时直接调用 handler(不经过 registry, 模拟降级竞态)
        out = json.loads(await knowledge_tools.knowledge_search("洛天依"))
        assert out == {"error": "知识库未启用"}
        out2 = json.loads(await knowledge_tools.knowledge_lookup("洛天依"))
        assert out2 == {"error": "知识库未启用"}
