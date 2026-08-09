"""Unit tests for audit fixes:
1. BotManager reconnect race — stale unregister must not remove new instance
2. json_update atomic read-modify-write — concurrent appends lose nothing
3. send_to_bot / _send_tracked future cleanup
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def test_reconnect_race() -> None:
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.models.config import BotConfig

    bm = BotManager(data_dir=tempfile.mkdtemp(prefix="bm_"))
    old = BotInstance("123", None, BotConfig(qq=123), bound=True)
    new = BotInstance("123", None, BotConfig(qq=123), bound=True)
    # 旧连接在线,新连接注册(替换实例)
    bm._bots["123"] = old
    bm._bots["123"] = new
    # 旧连接关闭 → 不得误删新实例
    bm.unregister(old)
    assert bm.get("123") is new, "stale unregister must keep the new instance"
    # 当前实例关闭 → 正常移除
    bm.unregister(new)
    assert bm.get("123") is None
    print("[1] reconnect race OK")


async def test_json_update_atomicity() -> None:
    from mohobot.file_store import json_read, json_update

    tmp = tempfile.mkdtemp(prefix="ju_")
    path = Path(tmp) / "ctx.json"
    await json_write_initial(path)

    # 并发 20 次 append,每次加 1 条 → 最终必须有 20 条(读改写竞态会丢)
    async def append_one(i: int):
        def _up(data):
            ctx = data if isinstance(data, list) else []
            ctx.append({"i": i})
            return ctx
        await json_update(path, _up, default=[])

    await asyncio.gather(*[append_one(i) for i in range(20)])
    final = await json_read(path)
    assert isinstance(final, list) and len(final) == 20, f"lost updates: {len(final)}"
    print("[2] json_update atomicity OK")


async def json_write_initial(path: Path) -> None:
    from mohobot.file_store import json_write
    await json_write(path, [])


async def test_future_cleanup() -> None:
    from mohobot.bot_manager import BotManager

    bm = BotManager(data_dir=tempfile.mkdtemp(prefix="fc_"))
    fut = bm.create_response_future("bot_001", "echo1")
    assert "echo1" in bm._pending_responses["bot_001"]
    bm.remove_response_future("bot_001", "echo1")
    assert "echo1" not in bm._pending_responses.get("bot_001", {})
    bm._pending_sent["e2"] = ("1", "private", "2")
    bm.drop_pending_sent("e2")
    assert "e2" not in bm._pending_sent
    print("[3] future cleanup OK")


async def test_chroma_where_and_embed_api() -> None:
    """Chroma 兼容性: 多字段 where 需 $and 包装; embedding 函数需
    embed_query/embed_documents 方法(chromadb >= 1.5)。"""
    from mohobot.agent.vector_store import ChromaVectorStore, _OpenAICompatEmbedding

    # 1. where 规范化
    n = ChromaVectorStore._normalize_where
    assert n(None) is None
    assert n({"user_id": "1"}) == {"user_id": "1"}
    assert n({"user_id": "1", "owner_character_id": "2"}) == {
        "$and": [{"user_id": "1"}, {"owner_character_id": "2"}],
    }

    # 2. embedding 包装类必须实现 chromadb 1.5 协议方法
    emb = _OpenAICompatEmbedding("m", "http://x", "k")
    assert emb.name() == "openai_compat"
    for method in ("embed_query", "embed_documents"):
        assert callable(getattr(emb, method, None)), f"缺少 {method}()"

    # 3. search 传入多字段 where 时被 $and 包装(用假 collection 验证)
    class FakeCollection:
        def __init__(self):
            self.last_where = None

        def query(self, **kwargs):
            self.last_where = kwargs.get("where")
            return {"ids": [], "documents": [], "metadatas": [], "distances": []}

    fake = FakeCollection()
    vs = ChromaVectorStore.__new__(ChromaVectorStore)  # 跳过真实初始化
    vs._collection = fake
    vs._executor = None

    async def _fake_executor(fn):
        return fn()

    import mohobot.agent.vector_store as vs_mod
    orig = vs_mod.asyncio.get_event_loop().run_in_executor
    vs_mod.asyncio.get_event_loop().run_in_executor = lambda *a, **k: _fake_executor(a[1])

    try:
        await vs.search("u1", "q", where={"user_id": "u1", "owner_character_id": "b1"})
        assert fake.last_where == {"$and": [{"user_id": "u1"}, {"owner_character_id": "b1"}]}, fake.last_where
        await vs.search("u1", "q")
        assert fake.last_where == {"user_id": "u1"}, fake.last_where
    finally:
        vs_mod.asyncio.get_event_loop().run_in_executor = orig
    print("[4] chroma where $and + embed_query/embed_documents OK")


async def main() -> None:
    await test_reconnect_race()
    await test_json_update_atomicity()
    await test_future_cleanup()
    await test_chroma_where_and_embed_api()
    print("\nALL AUDIT FIX TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
