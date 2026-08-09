"""Anysearch 联网搜索接入测试:
1. AnySearchClient JSON-RPC 调用(fake httpx)
2. beta 模式: fact_constraints → 并行搜索 → extra_knowledge 注入
3. LLMService 旧路径工具 anysearch_search
4. /搜索 插件(搜索/extract/batch/未配置降级)
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeTransport:
    """拦截 httpx.AsyncClient.post 的响应。"""

    def __init__(self, responder=None, **kwargs):
        self._responder = responder or (lambda payload: (200, make_result("ok")))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        status, text = await self._responder(kw.get("json"))
        return FakeResponse(status, text)


class FakeResponse:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        pass


def make_result(text: str) -> str:
    return json.dumps({"result": {"content": [{"type": "text", "text": text}]}})


async def test_client() -> None:
    import mohobot.anysearch as mod
    from mohobot.anysearch import AnySearchClient, AnySearchError

    calls = []

    async def responder(payload):
        calls.append(payload)
        name = payload["params"]["name"]
        assert payload["method"] == "tools/call"
        if name == "search":
            assert payload["params"]["arguments"]["query"] == "天气"
            return 200, make_result("今天晴, 25°C")
        if name == "extract":
            return 200, make_result("网页正文内容...")
        if name == "batch_search":
            return 200, make_result("批量结果")
        return 500, "boom"

    client = AnySearchClient(api_key="k", base_url="http://fake/mcp", timeout=5)
    client._http_client_factory = lambda **kw: FakeTransport(responder)

    r = await client.search("天气", max_results=5)
    assert r == "今天晴, 25°C"
    r2 = await client.extract("http://x.com")
    assert r2 == "网页正文内容..."
    r3 = await client.batch_search([{"query": "a"}, {"query": "b"}])
    assert r3 == "批量结果"
    # Authorization 头
    assert calls[0]["jsonrpc"] == "2.0"
    # safe_search 失败降级为空串
    async def fail(payload):
        return 500, "err"
    client2 = AnySearchClient(api_key="k", base_url="http://fake/mcp", timeout=5)
    client2._http_client_factory = lambda **kw: FakeTransport(fail)
    r4 = await client2.safe_search("q")
    assert r4 == ""
    print("[1] AnySearchClient JSON-RPC OK, calls:", len(calls))


async def test_beta_fact_search() -> None:
    """beta 模式: CharacterSubconscious 用 anysearch 搜 fact_constraints。"""
    from mohobot.agent.character_mind import CharacterSubconscious

    class FakeAnySearch:
        async def safe_search(self, query, max_results=5):
            return f"【结果】{query}的百科资料"

    mind = CharacterSubconscious.__new__(CharacterSubconscious)
    mind.anysearch = FakeAnySearch()
    mind.song_knowledge = None  # 测试环境不加载歌曲知识库
    mind.logger = __import__("loguru").logger

    hits = await mind.search_fact_constraints_for_topic(["最新科技新闻", "洛天依出道日期"])
    assert len(hits) == 2, hits
    assert "【结果】最新科技新闻的百科资料" in hits[0]
    assert "[搜索:" in hits[0]

    # 超过 2 个查询只搜前 2 个
    hits2 = await mind.search_fact_constraints_for_topic(["a", "b", "c", "d"])
    assert len(hits2) == 2

    # 歌曲类约束 → 走知识库(未配置 → 降级为空, 不报错)
    hits3 = await mind.search_fact_constraints_for_topic(["《千年食谱颂》"])
    assert hits3 == []

    # 无 client / 空查询 → 空列表(降级)
    mind.anysearch = None
    assert await mind.search_fact_constraints_for_topic(["x"]) == []
    assert await mind.search_fact_constraints_for_topic([]) == []
    print("[2] beta 模式 fact 搜索注入 OK")


async def test_llm_tool() -> None:
    """旧路径: LLMService._execute_tool 调 anysearch_search。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.config import GlobalConfig
    import tempfile, os, yaml

    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "g.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump({
            "llm": {"chat_api_key": "k", "chat_base_url": "http://x", "chat_model": "m"},
            "anysearch": {"enabled": True, "api_key": "ak", "base_url": "http://fake/mcp"},
        }, f)
    cfg = GlobalConfig.load(path)
    svc = LLMService(cfg)
    assert svc._anysearch_client is not None

    class FakeAS:
        async def safe_search(self, query, max_results=5):
            return f"结果:{query}"

    svc._anysearch_client = FakeAS()
    out = await svc._execute_tool("anysearch_search", json.dumps({"query": "东京天气"}))
    assert out == "结果:东京天气"
    # 无 key 时工具被移除
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump({
            "llm": {"chat_api_key": "k", "chat_base_url": "http://x", "chat_model": "m"},
        }, f)
    svc2 = LLMService(GlobalConfig.load(path))
    names = [t["function"]["name"] for t in svc2._tools_schemas]
    assert "anysearch_search" not in names
    print("[3] LLMService anysearch 工具 OK")


async def test_search_plugin() -> None:
    """/搜索 插件: 搜索/extract/batch/未配置降级。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins"))
    from anysearch import Plugin

    class FakeEvent:
        message = [{"type": "text", "data": {"text": ""}}]

    class FakeClient:
        async def search(self, query, max_results=None):
            return f"SEARCH:{query}"

        async def extract(self, url):
            return f"EXTRACT:{url}"

        async def batch_search(self, queries):
            return f"BATCH:{len(queries)}"

    p = Plugin()
    Plugin.inject_anysearch_client(FakeClient())

    async def run(text):
        ev = FakeEvent()
        ev.message = [{"type": "text", "data": {"text": text}}]
        return await p.on_message("b", ev, {})

    handled, out = await run("/搜索 东京天气")
    assert handled and out.endswith("SEARCH:东京天气"), out
    handled, out = await run("/搜索 extract http://x.com/a")
    assert handled and out.endswith("EXTRACT:http://x.com/a"), out
    handled, out = await run("/搜索 batch 苹果,香蕉")
    assert handled and out.endswith("BATCH:2"), out
    handled, out = await run("/搜索")
    assert handled and "用法" in out
    # 未配置 → 提示
    Plugin.inject_anysearch_client(None)
    handled, out = await run("/搜索 测试")
    assert handled and "未配置" in out
    # 非触发词
    handled, _ = await run("随便聊聊")
    assert handled is False
    print("[4] /搜索 插件 OK")


async def main() -> None:
    await test_client()
    await test_beta_fact_search()
    await test_llm_tool()
    await test_search_plugin()
    print("\nALL ANYSEARCH TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
