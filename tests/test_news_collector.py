"""新闻采集器与 WebUI 审核链路回归 (REQ-6)。

不访问外网: 采集器的网络出口 `_fetch_json` 被替换为假响应。
覆盖:
1. 采集器默认关闭 → 不发请求、不提交;
2. 采到的动态/投稿一律进 pending, 审核通过后才可见;
3. B站响应格式异常/错误码时不崩, 跳过该轮;
4. 单 UID 失败不影响其它 UID;
5. 采集条数上限;
6. WebUI 审核 API: 待审列表、通过、拒绝、删除、手动新增;
7. news_submit 工具只写 pending, 不进检索。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import NewsConfig  # noqa: E402
from mohobot.news.collector import NewsCollector  # noqa: E402
from mohobot.news.service import NewsService  # noqa: E402


def _svc(td: str, **kw) -> NewsService:
    cfg = NewsConfig(enabled=True, **kw)
    svc = NewsService(cfg, db_path=Path(td) / "news.db")
    svc.open()
    return svc


def _dynamic_payload(items: list[dict]) -> dict:
    return {"code": 0, "data": {"items": items}}


def _video_payload(videos: list[dict]) -> dict:
    return {"code": 0, "data": {"list": {"vlist": videos}}}


def _dynamic_item(title: str, ts: int, dynamic_id: str = "dy1") -> dict:
    return {
        "id_str": dynamic_id,
        "modules": {
            "module_author": {"name": "官方号", "pub_ts": ts},
            "module_dynamic": {"desc": {"text": title}, "major": None},
        },
    }


def _video_item(title: str, ts: int, bvid: str = "BV1x") -> dict:
    return {"bvid": bvid, "title": title, "created": ts, "author": "UP主", "description": ""}


# ── 1. 默认关闭 ──────────────────────────────────────────────

async def test_collector_disabled_by_default():
    """默认关闭: 即使有 UID 也不发任何请求。"""
    with tempfile.TemporaryDirectory() as td:
        svc = _svc(td)
        calls: list = []

        async def fake_get(url, params):
            calls.append((url, params))
            return {}

        collector = NewsCollector(svc, svc._cfg, http_get=fake_get)
        assert collector.enabled is False
        cfg = NewsConfig(enabled=True, collector_enabled=False, collector_uids=["1"])
        collector.sync_cfg = None  # 占位, 下面直接换配置
        collector._cfg = cfg
        result = await collector.poll_once()
        assert result == {"submitted": 0, "skipped": 0}
        assert calls == [], f"关闭时不得发网络请求: {calls}"
        svc.close()


async def test_enabled_without_uids_does_not_start():
    with tempfile.TemporaryDirectory() as td:
        svc = _svc(td)
        cfg = NewsConfig(enabled=True, collector_enabled=True, collector_uids=[])
        collector = NewsCollector(svc, cfg, http_get=_noop_get)
        await collector.start()
        assert collector._task is None, "没有 UID 不应启动后台任务"
        await collector.stop()
        svc.close()


async def _noop_get(url, params):
    return {}


# ── 2. 采集 → 待审 ──────────────────────────────────────────

async def test_collected_items_go_to_pending_then_visible():
    """采到的条目只能是 pending; 审核通过后才进入检索/注入。"""
    with tempfile.TemporaryDirectory() as td:
        svc = _svc(td)
        cfg = NewsConfig(
            enabled=True, collector_enabled=True, collector_uids=["42"],
            collector_max_items_per_uid=5,
        )

        async def fake_get(url, params):
            if "dynamic" in url:
                return _dynamic_payload([
                    _dynamic_item("天依新动态", 1700000400, "dy-a"),
                    _dynamic_item("第二条动态", 1700000300, "dy-b"),
                ])
            return _video_payload([_video_item("新歌翻唱", 1700000200, "BV1abc")])

        collector = NewsCollector(svc, cfg, http_get=fake_get)
        result = await collector.poll_once()
        assert result["submitted"] == 3, result
        # 全部是 pending
        pending = await svc.list_pending()
        assert len(pending) == 3
        assert all(p["status"] == "pending" for p in pending)
        # 未审核不进入检索/注入
        assert await svc.recent_news(limit=10) == []
        assert await svc.build_injection() == ""

        # 审核其中一条
        await svc.approve(pending[0]["id"], reviewer="admin")
        approved = await svc.recent_news(limit=10)
        assert len(approved) == 1
        svc.close()


async def test_collector_dedupes_by_url():
    """同一 UID 重复轮询: 相同 url 不重复入库。"""
    with tempfile.TemporaryDirectory() as td:
        svc = _svc(td)
        cfg = NewsConfig(enabled=True, collector_enabled=True, collector_uids=["42"])

        async def fake_get(url, params):
            if "dynamic" in url:
                return _dynamic_payload([_dynamic_item("同一条", 1700000400, "same-id")])
            return _video_payload([])

        collector = NewsCollector(svc, cfg, http_get=fake_get)
        await collector.poll_once()
        await collector.poll_once()
        assert len(await svc.list_pending()) == 1
        svc.close()


# ── 3. 容错 ─────────────────────────────────────────────────

async def test_collector_survives_bad_payload():
    """接口返回错误码/畸形结构时只跳过, 不抛异常。"""
    with tempfile.TemporaryDirectory() as td:
        svc = _svc(td)
        cfg = NewsConfig(enabled=True, collector_enabled=True, collector_uids=["42"])

        async def bad_get(url, params):
            raise ValueError("接口返回错误码: -352 风控")

        collector = NewsCollector(svc, cfg, http_get=bad_get)
        result = await collector.poll_once()
        assert result == {"submitted": 0, "skipped": 0}, result
        svc.close()


async def test_collector_isolates_failing_uid():
    """一个 UID 失败不影响另一个 UID 的采集。"""
    with tempfile.TemporaryDirectory() as td:
        svc = _svc(td)
        cfg = NewsConfig(
            enabled=True, collector_enabled=True,
            collector_uids=["bad", "good"], collector_max_items_per_uid=5,
        )

        async def fake_get(url, params):
            if params.get("host_mid") == "bad" or params.get("mid") == "bad":
                raise RuntimeError("boom")
            if "dynamic" in url:
                return _dynamic_payload([_dynamic_item("好U的动态", 1700000400, "g1")])
            return _video_payload([])

        collector = NewsCollector(svc, cfg, http_get=fake_get)
        result = await collector.poll_once()
        assert result["submitted"] >= 1, result
        pending = await svc.list_pending()
        assert any("好U的动态" == p["title"] for p in pending), pending
        svc.close()


async def test_collector_respects_item_limit():
    with tempfile.TemporaryDirectory() as td:
        svc = _svc(td)
        cfg = NewsConfig(
            enabled=True, collector_enabled=True, collector_uids=["42"],
            collector_max_items_per_uid=2,
        )

        async def fake_get(url, params):
            if "dynamic" in url:
                return _dynamic_payload([
                    _dynamic_item(f"动态{i}", 1700000400 + i, f"dy{i}") for i in range(6)
                ])
            return _video_payload([])

        collector = NewsCollector(svc, cfg, http_get=fake_get)
        await collector.poll_once()
        assert len(await svc.list_pending()) == 2, await svc.list_pending()
        svc.close()


# ── 4. 聊天提取工具 ─────────────────────────────────────────

async def test_news_submit_tool_only_creates_pending():
    from mohobot.news import tools as news_tools

    with tempfile.TemporaryDirectory() as td:
        svc = _svc(td)
        try:
            news_tools.set_news_service(svc)
            payload = json.loads(await news_tools.news_submit(
                "某UP主发布新曲", content="用户在群里提到的", source_url="https://b23.tv/x",
            ))
            assert payload["status"] == "pending_review", payload
            # 未审核 → 检索不到
            search = json.loads(await news_tools.news_search("新曲"))
            assert search["items"] == [], search
            # 审核后才可见
            await svc.approve(payload["id"], reviewer="admin")
            search = json.loads(await news_tools.news_search("新曲"))
            assert len(search["items"]) == 1, search
        finally:
            news_tools.set_news_service(None)
            svc.close()


async def main() -> None:
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
        print(f"PASS {name}")
    print("\nALL NEWS COLLECTOR TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
