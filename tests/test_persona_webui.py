"""人设面板隔离回归：mock 共享服务 + 临时数据 + 内存 ASGI，不启动服务。"""

import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from mohobot.web_panel.app import WebPanel


class PersonaInUseError(ValueError):
    def __init__(self, references):
        super().__init__("in use")
        self.references = references


class FakePersonaService:
    """按公开契约模拟缓存与引用，绝不读实际配置/上下文。"""

    def __init__(self):
        self.items = {
            "persona_001": {"id": "persona_001", "name": "默认", "content": "默认正文"},
            "persona_002": {"id": "persona_002", "name": "测试", "content": "测试正文"},
        }
        self.refs = {"persona_002": [{"type": "bot", "bot_id": "bot_001"}]}
        self.bot_persona_id = "persona_001"
        self.session_persona_id = "persona_002"
        self.updates = []
        self.bindings = []

    async def list_personas(self):
        return [dict(p, created="now", updated="now", reference_count=len(self.refs.get(pid, [])))
                for pid, p in self.items.items()]

    async def create_persona(self, name, content):
        pid = f"persona_{len(self.items) + 1:03d}"
        self.items[pid] = {"id": pid, "name": name, "content": content}
        return dict(self.items[pid])

    async def update_persona(self, pid, name, content):
        if name == "reject":
            raise ValueError("名称不合法")
        self.items[pid].update(name=name, content=content)
        return dict(self.items[pid])

    async def delete_persona(self, pid):
        if self.refs.get(pid):
            raise PersonaInUseError(self.refs[pid])
        del self.items[pid]

    async def list_references(self, pid):
        return self.refs.get(pid, [])

    async def update_bot_config(self, bot_id, data):
        if "persona" in data:
            raise ValueError("旧版 persona 不可编辑，请选择 persona_id")
        if data.get("persona_id", self.bot_persona_id) not in self.items:
            raise ValueError("人设不存在")
        self.bot_persona_id = data.get("persona_id", self.bot_persona_id)
        self.updates.append((bot_id, dict(data)))
        return SimpleNamespace(persona_id=self.bot_persona_id)

    def resolve_bot(self, config):
        return dict(self.items[config.persona_id], source="bot")

    async def get_session_binding(self, bot_id, user_id, sid):
        self.bindings.append((bot_id, user_id, sid))
        pid = self.session_persona_id or self.bot_persona_id
        return {
            "persona_id": self.session_persona_id,
            "bot_persona_id": self.bot_persona_id,
            "effective": dict(self.items[pid], source="session" if self.session_persona_id else "bot"),
        }


class PersonaWebUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="persona_webui_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = FakePersonaService()
        self.audit = SimpleNamespace(write=AsyncMock(), close=AsyncMock())
        self.audit_patch = patch("mohobot.services.audit.AuditLogger", return_value=self.audit)
        self.audit_patch.start()
        self.addCleanup(self.audit_patch.stop)
        self.sink_patch = patch.object(WebPanel, "_install_log_sink")
        self.sink_patch.start()
        self.addCleanup(self.sink_patch.stop)
        self.context = SimpleNamespace(get_session=AsyncMock(return_value={"id": "main", "name": "会话", "messages": []}))
        cfg = SimpleNamespace(bot_id="bot_001", persona_id="persona_001")
        self.bots = SimpleNamespace(get=lambda _: SimpleNamespace(config=cfg), list_bot_configs=lambda: [cfg])
        self.panel = WebPanel(
            password_hash=WebPanel._hash_password("test-pass"),
            data_dir=str(self.root), config_path=str(self.root / "missing.yaml"),
            persona_service=self.service, context_manager=self.context, bot_manager=self.bots,
        )
        self.panel._tokens["test-token"] = time.time() + 60
        self.client = TestClient(self.panel._app)
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = "Bearer test-token"

    def test_auth_all_persona_routes(self):
        paths = [
            ("GET", "/api/personas", None), ("POST", "/api/personas", {"name": "x", "content": "y"}),
            ("GET", "/api/personas/persona_002", None),
            ("GET", "/api/personas/persona_002/references", None),
            ("PUT", "/api/personas/persona_002", {"name": "x", "content": "y"}),
            ("DELETE", "/api/personas/persona_002", None),
        ]
        self.client.headers.clear()
        for method, path, body in paths:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path, json=body).status_code, 401)
        self.assertEqual(len(self.service.items), 2)

    def test_crud_auto_id_and_body_validation(self):
        for body in ({"name": " ", "content": "x"}, {"name": "x", "content": "\n"}):
            self.assertEqual(self.client.post("/api/personas", json=body).status_code, 400)
        r = self.client.post("/api/personas", json={"name": "x", "content": "y", "id": "custom"})
        self.assertEqual(r.status_code, 422)
        name, content = '<img src=x onerror="alert(1)">', '</textarea><script>alert(1)</script>'
        r = self.client.post("/api/personas", json={"name": name, "content": content})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["id"], "persona_003")
        self.assertEqual(r.json()["content"], content)
        items = self.client.get("/api/personas").json()
        self.assertEqual(items[-1]["name"], name)
        r = self.client.put("/api/personas/persona_003", json={"name": "edited", "content": "new"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/personas/persona_003").json()["content"], "new")
        self.assertEqual(self.client.delete("/api/personas/persona_003").status_code, 200)
        self.assertEqual(self.client.get("/api/personas/persona_003").status_code, 404)

    def test_not_found_and_service_validation(self):
        for method in ("GET", "PUT", "DELETE"):
            r = self.client.request(method, "/api/personas/persona_999", json={"name": "x", "content": "y"})
            self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.client.get("/api/personas/persona_999/references").status_code, 404)
        self.assertEqual(self.client.get("/api/personas/invalid.id").status_code, 400)
        r = self.client.put("/api/personas/persona_002", json={"name": "reject", "content": "x"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "名称不合法")

    def test_reference_conflict_and_default_protection(self):
        r = self.client.get("/api/personas/persona_002/references")
        self.assertEqual(r.json()["references"], self.service.refs["persona_002"])
        r = self.client.delete("/api/personas/persona_002")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["references"], self.service.refs["persona_002"])
        r = self.client.delete("/api/personas/persona_001")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("不可删除", r.json()["detail"])
        self.assertIn("persona_001", self.service.items)

    def test_bot_updates_delegate_and_legacy_rejected(self):
        data = {"nickname": "未保存的名称", "persona_id": "persona_002", "enabled": False}
        with patch("mohobot.models.config.BotConfig.load", side_effect=AssertionError("must delegate")):
            r = self.client.put("/api/bots/bot_001/config", json={"data": data})
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.service.updates, [("bot_001", data)])
        for data in ({"persona_id": "persona_999"}, {"persona": "legacy"}):
            self.assertEqual(self.client.put("/api/bots/bot_001/config", json={"data": data}).status_code, 400)

    def test_private_effective_hot_update_and_group_default(self):
        path = "/api/contexts/bot_001/private/123/session/main"
        body = self.client.get(path).json()
        self.assertEqual(body["persona_id"], "persona_002")
        self.assertEqual(body["effective"]["source"], "session")
        self.assertEqual(self.service.bindings[-1], ("bot_001", "123", "main"))
        self.client.put("/api/personas/persona_002", json={"name": "new name", "content": "hot content"})
        self.assertEqual(self.client.get(path).json()["effective"]["content"], "hot content")
        self.service.session_persona_id = ""
        body = self.client.get(path).json()
        self.assertEqual(body["effective"]["id"], "persona_001")
        before = len(self.service.bindings)
        group_path = path.replace("private/123", "group/456")
        body = self.client.get(group_path).json()
        self.assertIsNone(body["persona_id"])
        self.assertEqual(body["effective"]["id"], "persona_001")
        self.assertEqual(len(self.service.bindings), before)
        self.bots.get = lambda _: None  # 离线 Bot 仍可显示默认人设。
        self.assertEqual(self.client.get(group_path).json()["effective"]["id"], "persona_001")
        self.assertNotIn("effective", self.context.get_session.return_value)
        self.assertEqual(self.client.put(path + "/persona", json={"persona_id": "persona_002"}).status_code, 404)

    def test_no_service_legacy_bot_update_compatibility(self):
        self.panel._persona_service = None
        cfg = SimpleNamespace(nickname="before", save=lambda _: None)
        with patch("mohobot.models.config.BotConfig.load", return_value=cfg):
            r = self.client.put("/api/bots/bot_001/config", json={"data": {"nickname": "after"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(cfg.nickname, "after")
        self.assertEqual(self.client.get("/api/personas").status_code, 503)

    def test_audit_omits_persona_content_and_query(self):
        self.client.post("/api/personas?content=do-not-log&name=do-not-log", json={"name": "secret name", "content": "secret body"})
        self.assertTrue(self.audit.write.await_count)
        for call in self.audit.write.await_args_list:
            serialized = json.dumps(call.kwargs, ensure_ascii=False)
            self.assertNotIn("secret name", serialized)
            self.assertNotIn("secret body", serialized)
            self.assertNotIn("do-not-log", serialized)

    def test_global_backup_and_destructive_guards(self):
        personas = self.root / "personas"
        personas.mkdir()
        (personas / "index.json").write_text('{"test":"only temp"}', encoding="utf-8")
        scope = {"bots": ["bot_999"], "dirs": ["personas"]}
        r = self.client.post("/api/data/backup", json={"data": scope})
        self.assertEqual(r.status_code, 200, r.text)
        backup_bytes = r.content
        with zipfile.ZipFile(io.BytesIO(backup_bytes)) as zf:
            self.assertEqual(zf.namelist(), ["personas/index.json"])
        r = self.client.post("/api/data/cleanup", json={"data": {"password": "test-pass", "scope": scope}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("逐个删除", r.json()["detail"])
        self.assertTrue((personas / "index.json").exists())
        # 当前人设恢复暂禁，主代理整合引用校验后再启用。
        r = self.client.post("/api/data/restore", files={"file": ("backup.zip", io.BytesIO(backup_bytes), "application/zip")},
                             data={"password": "test-pass", "scope": json.dumps(scope)})
        self.assertNotEqual(r.status_code, 200)
        self.assertTrue((personas / "index.json").exists())
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "keep.txt").write_text("keep")
        r = self.client.post("/api/data/cleanup", json={"data": {"password": "test-pass", "scope": {"bots": "all", "dirs": ["cache", "personas"]}}})
        self.assertEqual(r.status_code, 400)
        self.assertTrue((cache / "keep.txt").exists(), "guard must run before deleting other ranges")


def test_persona_webui_suite():
    """兼容项目只发现模块 test_* 函数的 _run_all runner。"""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PersonaWebUITests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError(f"Persona WebUI suite failed: {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    test_persona_webui_suite()
