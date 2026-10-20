"""新闻 WebUI 审核 API 回归 (REQ-6) — 通过 FastAPI TestClient，不启真实端口。

覆盖:
1. 未启用新闻库时: 列表接口返回 enabled=false, 审核接口 503;
2. 手动新增 → 待审; 待审不出现在已生效列表;
3. 审核通过 → 进入已生效(会被注入/工具检索);
4. 审核拒绝 → 不生效, 且不能再次通过;
5. 批量审核返回 changed/failed;
6. 删除已生效条目;
7. 未认证请求 401。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import NewsConfig  # noqa: E402
from mohobot.news.service import NewsService  # noqa: E402


def _pwd_hash(password: str) -> str:
    import hashlib
    import secrets

    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return f"pbkdf2_sha256${salt}${h}"


def _make_panel(tmp: Path, *, enabled: bool = True):
    from mohobot.web_panel.app import WebPanel

    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp / "global.yaml"
    cfg_path.write_text("data_dir: ./data\n", encoding="utf-8")

    news = None
    if enabled:
        svc = NewsService(NewsConfig(enabled=True), db_path=data_dir / "news" / "news.db")
        svc.open()
        news = svc

    panel = WebPanel(
        host="127.0.0.1", port=0, username="admin",
        password_hash=_pwd_hash("secret123"),
        data_dir=str(data_dir), config_path=str(cfg_path),
        news_service=news,
    )
    return panel, news


def _login(client):
    r = client.post("/api/login", json={"username": "admin", "password": "secret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_news_api_requires_auth():
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        panel, news = _make_panel(Path(td))
        client = TestClient(panel._app)
        try:
            assert client.get("/api/news/pending").status_code == 401
            assert client.post("/api/news/review", json={"ids": ["x"]}).status_code == 401
        finally:
            if news:
                news.close()


def test_news_api_when_disabled():
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        panel, news = _make_panel(Path(td), enabled=False)
        client = TestClient(panel._app)
        auth = _login(client)
        try:
            r = client.get("/api/news/pending", headers=auth)
            assert r.status_code == 200, r.text
            assert r.json() == {"enabled": False, "items": []}, r.json()
            # 未装配服务 → 审核接口 503
            r = client.post("/api/news/review", json={"ids": ["x"], "approve": True}, headers=auth)
            assert r.status_code == 503, r.text
        finally:
            if news:
                news.close()


def test_news_review_workflow():
    """完整闭环: 手动新增 → 待审 → 通过 → 已生效 → 删除。"""
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        panel, news = _make_panel(Path(td))
        client = TestClient(panel._app)
        auth = _login(client)
        try:
            # 1) 手动新增
            r = client.post("/api/news/submit", headers=auth, json={
                "title": "天依新歌发布", "content": "官方公告", "url": "https://b23.tv/new",
                "author_name": "官方号", "tags": ["新歌"],
            })
            assert r.status_code == 200, r.text
            item_id = r.json()["id"]
            assert r.json()["review"] == "pending"

            # 2) 待审可见, 已生效为空
            r = client.get("/api/news/pending", headers=auth)
            assert [i["id"] for i in r.json()["items"]] == [item_id]
            r = client.get("/api/news/approved", headers=auth)
            assert r.json()["items"] == []

            # 3) 通过 → 进入已生效
            r = client.post("/api/news/review", headers=auth,
                            json={"ids": [item_id], "approve": True})
            assert r.status_code == 200 and r.json()["changed"] == 1, r.text
            r = client.get("/api/news/approved", headers=auth)
            titles = [i["title"] for i in r.json()["items"]]
            assert titles == ["天依新歌发布"], titles

            # 统计
            r = client.get("/api/news/stats", headers=auth)
            counts = r.json()["counts"]
            assert counts.get("approved") == 1 and not counts.get("pending"), counts

            # 4) 删除
            r = client.delete(f"/api/news/{item_id}", headers=auth)
            assert r.status_code == 200, r.text
            r = client.get("/api/news/approved", headers=auth)
            assert r.json()["items"] == []
        finally:
            news.close()


def test_news_reject_cannot_be_reapproved():
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        panel, news = _make_panel(Path(td))
        client = TestClient(panel._app)
        auth = _login(client)
        try:
            r = client.post("/api/news/submit", headers=auth, json={"title": "不实消息"})
            item_id = r.json()["id"]

            r = client.post("/api/news/review", headers=auth,
                            json={"ids": [item_id], "approve": False})
            assert r.json()["changed"] == 1, r.text

            # 已拒绝不能再通过
            r = client.post("/api/news/review", headers=auth,
                            json={"ids": [item_id], "approve": True})
            assert r.json()["changed"] == 0, r.text
            assert r.json()["failed"] == [item_id], r.json()

            r = client.get("/api/news/approved", headers=auth)
            assert r.json()["items"] == []
        finally:
            news.close()


def test_news_batch_review_reports_failed():
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        panel, news = _make_panel(Path(td))
        client = TestClient(panel._app)
        auth = _login(client)
        try:
            ids = []
            for i in range(3):
                r = client.post("/api/news/submit", headers=auth, json={"title": f"条目{i}"})
                ids.append(r.json()["id"])
            r = client.post("/api/news/review", headers=auth,
                            json={"ids": ids + ["does-not-exist"], "approve": True})
            body = r.json()
            assert body["changed"] == 3, body
            assert body["failed"] == ["does-not-exist"], body
        finally:
            news.close()


def test_news_submit_validates_title():
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        panel, news = _make_panel(Path(td))
        client = TestClient(panel._app)
        auth = _login(client)
        try:
            r = client.post("/api/news/submit", headers=auth, json={"title": "   "})
            assert r.status_code == 400, r.text
        finally:
            news.close()


async def test_approved_items_drive_injection_and_tools():
    """端到端语义: 只有审核通过的条目会出现在注入片段与工具结果里。

    注意: 根 runner 在事件循环内执行测试, 这里必须用 async 定义并直接 await,
    不能在测试内部再 asyncio.run()(会触发 "cannot be called from a running loop")。
    """
    from fastapi.testclient import TestClient

    from mohobot.news import tools as news_tools

    with tempfile.TemporaryDirectory() as td:
        panel, news = _make_panel(Path(td))
        client = TestClient(panel._app)
        auth = _login(client)
        try:
            pending = client.post("/api/news/submit", headers=auth,
                                  json={"title": "待审的不该出现"}).json()["id"]
            approved = client.post("/api/news/submit", headers=auth,
                                   json={"title": "已审的应该出现"}).json()["id"]
            client.post("/api/news/review", headers=auth,
                        json={"ids": [approved], "approve": True})

            import json

            news_tools.set_news_service(news)
            try:
                block = await news.build_injection()
                assert "已审的应该出现" in block and "待审的不该出现" not in block, block
                payload = json.loads(await news_tools.news_recent(limit=10))
                titles = [i["title"] for i in payload["items"]]
                assert titles == ["已审的应该出现"], titles
                assert pending  # 仅确认待审条目存在, 不应出现在结果里
            finally:
                news_tools.set_news_service(None)
        finally:
            news.close()
