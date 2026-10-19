"""新闻/动态知识库回归测试 — 审核门禁、只读渲染、提示词注入与工具契约。

核心安全边界:
- 未审核(pending)/已拒绝(rejected)条目**绝不能**出现在工具结果、注入片段或上下文;
- 过期条目不再返回;
- 默认关闭时完全不产生注入;
- 提交永远进 pending。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import GlobalConfig, NewsConfig  # noqa: E402
from mohobot.news import NewsService, STATUS_PENDING  # noqa: E402


def _service(td: str, **cfg_kwargs) -> NewsService:
    cfg = NewsConfig(enabled=True, **cfg_kwargs)
    svc = NewsService(cfg, db_path=Path(td) / "news.db")
    svc.open()
    return svc


# ── 1. 审核门禁 ──────────────────────────────────────────────

async def test_pending_never_returned_before_review():
    """提交后未审核: recent_news / search_news / 注入都必须为空。"""
    with tempfile.TemporaryDirectory() as td:
        svc = _service(td)
        try:
            item = await svc.submit("新歌发布", content="天依新歌上线", source="bilibili")
            assert item.status == STATUS_PENDING

            assert await svc.recent_news(limit=10) == []
            assert await svc.search_news(["新歌"]) == []
            assert await svc.build_injection() == ""

            pending = await svc.list_pending()
            assert len(pending) == 1 and pending[0]["title"] == "新歌发布"
        finally:
            svc.close()


async def test_approve_makes_item_visible():
    with tempfile.TemporaryDirectory() as td:
        svc = _service(td)
        try:
            item = await svc.submit("新歌发布", content="天依新歌上线")
            assert await svc.approve(item.id, reviewer="admin") is True

            recent = await svc.recent_news(limit=10)
            assert len(recent) == 1 and recent[0].title == "新歌发布"
            assert "新歌发布" in await svc.build_injection()
        finally:
            svc.close()


async def test_reject_keeps_item_hidden():
    with tempfile.TemporaryDirectory() as td:
        svc = _service(td)
        try:
            item = await svc.submit("不实消息", content="谣言")
            assert await svc.reject(item.id, reviewer="admin") is True
            assert await svc.recent_news(limit=10) == []
            # 已审结的条目不能再被审一次
            assert await svc.approve(item.id) is False
        finally:
            svc.close()


async def test_review_requires_pending_status():
    """重复审核第二次应失败(避免已拒绝条目被偷偷放行)。"""
    with tempfile.TemporaryDirectory() as td:
        svc = _service(td)
        try:
            item = await svc.submit("标题", content="内容")
            assert await svc.approve(item.id) is True
            assert await svc.approve(item.id) is False
        finally:
            svc.close()


# ── 2. 过期与去重 ────────────────────────────────────────────

async def test_expired_items_not_returned():
    """过期条目自动不再返回(但保留在库中可审计)。"""
    with tempfile.TemporaryDirectory() as td:
        svc = _service(td)
        try:
            item = await svc.submit(
                "过期新闻", content="旧闻", expires_at=time.time() - 1,
            )  # 已过期
            await svc.approve(item.id)
            assert await svc.recent_news(limit=10) == []
            assert await svc.build_injection() == ""
            # 仍在库里
            assert (await svc.stats()).get("approved") == 1
        finally:
            svc.close()


async def test_duplicate_url_not_stored_twice():
    with tempfile.TemporaryDirectory() as td:
        svc = _service(td)
        try:
            a = await svc.submit("动态A", url="https://b23.tv/x")
            b = await svc.submit("动态A(重复)", url="https://b23.tv/x")
            assert a.id == b.id
            assert len(await svc.list_pending()) == 1
        finally:
            svc.close()


# ── 3. 注入文本 ──────────────────────────────────────────────

async def test_injection_respects_limit_and_header():
    with tempfile.TemporaryDirectory() as td:
        svc = _service(td, inject_limit=2, inject_header="【测试表头】")
        try:
            for i in range(4):
                item = await svc.submit(f"新闻{i}", content=f"内容{i}")
                await svc.approve(item.id)
            block = await svc.build_injection()
            assert block.startswith("【测试表头】"), block
            assert len(block.splitlines()) == 3, block  # 表头 + 2 条
        finally:
            svc.close()


async def test_disabled_service_produces_no_injection():
    """默认关闭: 即使库里有已审核条目也不注入。"""
    with tempfile.TemporaryDirectory() as td:
        svc = _service(td)
        item = await svc.submit("新歌", content="内容")
        await svc.approve(item.id)
        svc.sync_config(NewsConfig(enabled=False))
        assert await svc.build_injection() == ""
        svc.close()


# ── 4. LLM 工具契约 ──────────────────────────────────────────

async def test_tools_only_expose_approved_items():
    from mohobot.news import tools as news_tools

    with tempfile.TemporaryDirectory() as td:
        svc = _service(td)
        try:
            pending = await svc.submit("待审条目", content="未过审不应出现")
            approved = await svc.submit("已审条目", content="可以出现")
            await svc.approve(approved.id)

            news_tools.set_news_service(svc)
            import json
            payload = json.loads(await news_tools.news_recent(limit=10))
            titles = [i["title"] for i in payload["items"]]
            assert titles == ["已审条目"], titles
            assert pending.title not in titles

            payload2 = json.loads(await news_tools.news_search("已审"))
            assert len(payload2["items"]) == 1
        finally:
            news_tools.set_news_service(None)
            svc.close()


async def test_tool_without_service_reports_unavailable():
    from mohobot.news import tools as news_tools

    news_tools.set_news_service(None)
    import json
    payload = json.loads(await news_tools.news_recent())
    assert "error" in payload


def test_tools_registry_idempotent():
    """重复装配/摘除不得因重名报错。"""
    from mohobot.news.tools import register_news_tools, unregister_news_tools
    from mohobot.services.llm_tools import registry

    register_news_tools()
    register_news_tools()
    names = registry.schemas()
    assert any(t["function"]["name"] == "news_recent" for t in names), names
    unregister_news_tools()
    unregister_news_tools()
    names_after = registry.schemas()
    assert not any(t["function"]["name"] == "news_recent" for t in names_after)


# ── 5. 配置契约 ──────────────────────────────────────────────

def test_news_config_defaults_and_roundtrip():
    cfg = GlobalConfig()
    assert cfg.news.enabled is False, "默认必须关闭, 不改变既有部署行为"
    assert cfg.news.inject_limit == 3

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.yaml"
        cfg.news.enabled = True
        cfg.news.inject_limit = 5
        cfg.news.inject_window_days = 3
        cfg.save(path)
        loaded = GlobalConfig.load(path)
        assert loaded.news.enabled is True
        assert loaded.news.inject_limit == 5
        assert loaded.news.inject_window_days == 3
        assert cfg.to_dict()["news"]["inject_limit"] == 5


# ── 6. 注入不写入上下文(边界) ────────────────────────────────

async def test_injection_is_request_scoped_not_persisted():
    """注入只进本轮 messages, 不改 context 列表本身。"""
    from types import SimpleNamespace

    from mohobot.llm_service import LLMService

    svc = object.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=GlobalConfig().llm)
    svc._song_annotator = None
    svc._image_cache = None
    svc._tools_schemas = []
    svc._knowledge_service = None

    calls = {"n": 0}

    async def annotator():
        calls["n"] += 1
        return "【近期动态】\n- [2026-10-19] 新歌发布"

    svc._news_annotator = annotator

    event = SimpleNamespace(
        message=[{"type": "text", "data": {"text": "最近有什么新歌"}}],
        user_id=1, group_id=2, sender=SimpleNamespace(card="", nickname="u"),
    )
    context: list = []
    messages = await svc._prepare_messages("bot", event, context, None)
    assert calls["n"] == 1
    assert "新歌发布" in messages[0]["content"]
    assert context == [], "注入不得写回 context"


async def main() -> None:
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
        print(f"PASS {name}")
    print("\nALL NEWS TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
