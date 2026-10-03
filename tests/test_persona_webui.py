"""人设面板隔离回归：mock 共享服务 + 临时数据 + 内存 ASGI，不启动服务。"""

import io
import json
import asyncio
import shutil
import subprocess
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
        self.list_calls = 0
        self.options_calls = 0
        self.get_calls = []
        self.invalidations = 0
        self.lock = asyncio.Lock()

    def invalidate_references(self):
        self.invalidations += 1

    def get_persona(self, pid):
        self.get_calls.append(pid)
        item = self.items.get(pid)
        return dict(item, created="now", updated="now") if item else None

    async def list_persona_options(self):
        self.options_calls += 1
        return [dict(p, created="now", updated="now") for p in self.items.values()]

    async def list_personas(self):
        self.list_calls += 1
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
            ("GET", "/api/personas", None), ("GET", "/api/personas/options", None),
            ("POST", "/api/personas", {"name": "x", "content": "y"}),
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
        self.assertEqual(self.service.list_calls, 0)
        self.assertEqual(self.service.options_calls, 0)
        self.assertEqual(self.service.get_calls, [])

    def test_options_cached_full_presets_without_reference_scan(self):
        with patch.object(self.service, "list_personas", AsyncMock(side_effect=AssertionError("no list scan"))), \
             patch.object(self.service, "list_references", AsyncMock(side_effect=AssertionError("no reference scan"))):
            r = self.client.get("/api/personas/options")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()), 2)
        self.assertEqual(r.json()[1]["content"], "测试正文")
        self.assertIn("created", r.json()[0])
        self.assertNotIn("reference_count", r.json()[0])
        self.assertEqual(self.service.get_calls, [], "fixed route must precede dynamic ID route")
        self.assertEqual(self.service.options_calls, 1)
        # Existing management route continues to include default reference counts.
        self.assertEqual(self.client.get("/api/personas").json()[1]["reference_count"], 1)

    def test_crud_existence_uses_get_not_list(self):
        with patch.object(self.service, "list_personas", AsyncMock(side_effect=AssertionError("no list scan"))):
            self.assertEqual(self.client.get("/api/personas/persona_002").status_code, 200)
            self.assertEqual(self.client.get("/api/personas/persona_002/references").status_code, 200)
            self.assertEqual(self.client.put("/api/personas/persona_002", json={"name": "new", "content": "body"}).status_code, 200)
            self.assertEqual(self.client.delete("/api/personas/persona_002").status_code, 409)
            self.assertEqual(self.client.get("/api/personas/persona_999").status_code, 404)
            r = self.client.post("/api/personas", json={"name": "new", "content": "body"})
            self.assertEqual(self.client.delete("/api/personas/" + r.json()["id"]).status_code, 200)
        self.assertIn("persona_002", self.service.get_calls)
        self.assertEqual(self.service.list_calls, 0)

    def test_list_validation_failure_is_actionable_503_options_still_available(self):
        with patch.object(self.service, "list_personas", AsyncMock(side_effect=ValueError("引用索引损坏"))):
            r = self.client.get("/api/personas")
            self.assertEqual(r.status_code, 503, r.text)
            self.assertIn("引用索引损坏", r.json()["detail"])
            self.assertEqual(self.client.get("/api/personas/options").status_code, 200)
        with patch.object(self.service, "list_persona_options", AsyncMock(side_effect=ValueError("缓存不可用"))):
            r = self.client.get("/api/personas/options")
            self.assertEqual(r.status_code, 503, r.text)
            self.assertIn("缓存不可用", r.json()["detail"])
        with patch.object(self.service, "list_references", AsyncMock(side_effect=ValueError("引用索引损坏"))):
            self.assertEqual(self.client.get("/api/personas/persona_002/references").status_code, 503)

    def test_reference_cache_invalidated_after_external_mutations(self):
        self.bots.create_bot = lambda **_: SimpleNamespace(bot_id="bot_003")
        self.assertEqual(self.client.post("/api/bots", json={"data": {}}).status_code, 200)
        self.assertEqual(self.service.invalidations, 1)
        self.context.delete_session = AsyncMock(return_value=True)
        path = "/api/contexts/bot_001/private/123/session/main"
        self.assertEqual(self.client.delete(path).status_code, 200)
        self.assertEqual(self.service.invalidations, 2)
        self.context.delete_session.return_value = False
        self.assertEqual(self.client.delete(path).status_code, 400)
        self.assertEqual(self.service.invalidations, 2)
        self.context.maintenance_lock = asyncio.Lock()
        with patch.object(self.panel, "_cleanup_data", return_value=0):
            r = self.client.post("/api/data/cleanup", json={"data": {"password": "test-pass", "scope": {"bots": "all", "dirs": ["contexts"]}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.service.invalidations, 3)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("contexts/bot_001/temp.json", "{}")
        with patch.object(self.panel, "_restore_persona_data", AsyncMock(return_value=1)):
            r = self.client.post("/api/data/restore", files={"file": ("temp.zip", archive.getvalue(), "application/zip")},
                                 data={"password": "test-pass", "scope": json.dumps({"bots": "all", "dirs": ["contexts"]})})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.service.invalidations, 4)

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

    def test_persona_frontend_native_mock_loading_retry_and_request_generations(self):
        """Execute actual inline functions with native Node + minimal DOM; no browser/server."""
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is required for native frontend regression")
        html = (Path(__file__).resolve().parent.parent / "mohobot/web_panel/static/index.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', async () => {
  try {
    const source = JSON.parse(input);
    new vm.Script(source); // Syntax check entire production inline script.
    const section = source.slice(source.indexOf('// ═══════════════ Shared personas'), source.indexOf('// ═══════════════ 3. Models'));
    const botLoader = source.slice(source.indexOf('let botConfigGeneration = 0;'), source.indexOf('async function saveGlobalConfig'));
    const botSaver = source.slice(source.indexOf('async function saveBotConfig'), source.indexOf('// ═══════════════ Shared personas'));
    class Element {
      constructor(value = '') { this.value = value; this.dataset = {}; this.disabled = false; this.children = []; this.options = []; this._html = ''; this._text = ''; }
      set textContent(v) { this._text = v; this.children = []; }
      get textContent() { return this._text; }
      set innerHTML(v) { this._html = v; }
      get innerHTML() { return this._html; }
      replaceChildren() { this.options = []; this.value = ''; }
      add(option) { this.options.push(option); if (this.options.length === 1) this.value = option.value; }
      appendChild(child) { this.children.push(child); }
      addEventListener(name, fn) { this[name] = fn; }
      querySelectorAll() { return []; }
    }
    const elements = {};
    for (const id of ['config-bot-select', 'config-bot-form', 'config-bot-save', 'cfg-persona_id', 'cfg-persona-preview', 'cfg-persona-status', 'cfg-nickname', 'persona-list', 'persona-list-status', 'persona-references']) elements[id] = new Element();
    elements['config-bot-select'].value = 'bot_001';
    elements['config-bot-form'].dataset.botId = 'bot_001';
    elements['cfg-persona_id'].dataset.initialId = 'persona_002';
    elements['cfg-persona_id'].disabled = true;
    elements['config-bot-save'].disabled = true;
    elements['cfg-nickname'].value = 'unsaved nickname';
    const calls = [];
    const queue = [];
    const toasts = [];
    const context = vm.createContext({
      console, document: { getElementById: id => elements[id], createElement: () => new Element(), querySelector: () => ({ click() {} }) },
      Option: function(text, value) { this.text = text; this.value = value; },
      AbortSignal: { timeout: ms => ({ timeoutMs: ms }) },
      api: async (path, options = {}) => { calls.push({ path, options }); if (!queue.length) throw new Error('unexpected request ' + path); const data = await queue.shift()(path, options); return data?.mockResponse ? data.response : { ok: true, status: 200, json: async () => data }; },
      apiJSON: async (path, options = {}) => { calls.push({ path, options }); if (!queue.length) throw new Error('unexpected request ' + path); return await queue.shift()(path, options); },
      showToast: (...args) => toasts.push(args), esc: v => String(v).replace(/[<>&"']/g, '_'),
      formField: () => '', textareaField: () => '', confirm: () => true,
    });
    vm.runInContext('let pageRequestGeneration = 0; let botConfigCache = null;', context);
    vm.runInContext(botLoader + botSaver + section, context);
    const presets = [
      { id: 'persona_001', name: 'default', content: 'default body', reference_count: 0 },
      { id: 'persona_002', name: 'test', content: 'test body', reference_count: 1 },
    ];
    const fail = () => Promise.reject(new Error('offline'));
    const ok = data => () => Promise.resolve(data);
    const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve }; };
    const run = js => vm.runInContext(js, context);

    // Empty initial load cannot save; failure persists, retry uses saved initial ID.
    await run('saveBotConfig()');
    assert.equal(calls.length, 0);
    queue.push(fail);
    await run('refreshPersonaChoices()');
    assert.equal(calls[0].path, '/api/personas/options');
    assert.equal(calls[0].options.signal.timeoutMs, 10000);
    assert.equal(elements['cfg-persona_id'].disabled, true);
    assert.equal(elements['config-bot-save'].disabled, true);
    assert.match(elements['cfg-persona-status'].textContent, /offline/);
    assert.equal(elements['cfg-persona-status'].children.length, 1);
    queue.push(ok(presets));
    await elements['cfg-persona-status'].children[0].click();
    assert.equal(elements['cfg-persona_id'].value, 'persona_002');
    assert.equal(elements['config-bot-save'].disabled, false);
    assert.equal(elements['cfg-persona-preview'].value, 'test body');
    assert.equal(elements['cfg-nickname'].value, 'unsaved nickname');
    queue.push(fail);
    await run('refreshPersonaChoices()');
    assert.equal(elements['cfg-persona_id'].value, 'persona_002');
    assert.equal(elements['cfg-persona_id'].disabled, false);
    assert.match(elements['cfg-persona-status'].textContent, /已保留/);

    // Later refresh wins; late error also must not overwrite current success.
    const oldOptions = deferred();
    queue.push(() => oldOptions.promise, ok([{ ...presets[1], content: 'new body' }]));
    const oldRequest = run('refreshPersonaChoices()');
    await run('refreshPersonaChoices()');
    oldOptions.resolve(presets);
    await oldRequest;
    assert.equal(elements['cfg-persona-preview'].value, 'new body');
    const pageOptions = deferred();
    queue.push(() => pageOptions.promise);
    const abandonedOptions = run('refreshPersonaChoices()');
    run('++pageRequestGeneration');
    pageOptions.resolve(presets);
    await abandonedOptions;
    assert.equal(elements['cfg-persona-preview'].value, 'new body');

    // Normal management load: one list read, reuse returned items for Bot dropdown.
    calls.length = 0;
    queue.push(ok(presets));
    assert.equal(await run('loadPersonasPage()'), true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, '/api/personas');
    assert.equal(calls[0].options.signal.timeoutMs, 15000);
    assert.match(elements['persona-list'].innerHTML, /缓存/);
    assert.match(elements['persona-list'].innerHTML, /被引用，禁止删除（缓存统计）/);
    assert.equal(elements['cfg-persona-preview'].value, 'test body');
    assert.equal(elements['cfg-nickname'].value, 'unsaved nickname');
    const oldList = deferred();
    queue.push(() => oldList.promise, ok([{ ...presets[1], name: 'newer list' }]));
    const oldListRequest = run('loadPersonasPage()');
    await run('loadPersonasPage()');
    oldList.resolve(presets);
    await oldListRequest;
    assert.match(elements['persona-list'].innerHTML, /newer list/);
    const pageList = deferred();
    queue.push(() => pageList.promise);
    const abandonedList = run('loadPersonasPage()');
    run('++pageRequestGeneration');
    pageList.resolve(presets);
    await abandonedList;
    assert.match(elements['persona-list'].innerHTML, /newer list/);

    // Failed reference statistics still display cached editable presets, not fake zero.
    calls.length = 0;
    queue.push(fail, ok(presets));
    assert.equal(await run('loadPersonasPage()'), false);
    assert.deepEqual(calls.map(c => c.path), ['/api/personas', '/api/personas/options']);
    assert.match(elements['persona-list-status'].textContent, /引用统计暂不可用/);
    assert.match(elements['persona-list'].innerHTML, /未知/);
    assert.match(elements['persona-list'].innerHTML, /data-action="delete" disabled/);
    queue.push(fail, fail);
    await run('loadPersonasPage()');
    assert.match(elements['persona-list-status'].textContent, /缓存选项也不可用/);
    assert.equal(elements['persona-list-status'].children.length, 1);
    const timeout = Object.assign(new Error('timeout'), { name: 'TimeoutError' });
    queue.push(() => Promise.reject(timeout));
    await run('refreshPersonaChoices()');
    assert.match(elements['cfg-persona-status'].textContent, /请求超时（10 秒）/);
    queue.push(ok({ mockResponse: true, response: { ok: true, status: 200, json: async () => { throw timeout; } } }));
    await run('refreshPersonaChoices()');
    assert.match(elements['cfg-persona-status'].textContent, /请求超时（10 秒）/);
    queue.push(ok({ mockResponse: true, response: { ok: false, status: 503, json: async () => ({ detail: 'service unavailable' }) } }));
    await run('refreshPersonaChoices()');
    assert.match(elements['cfg-persona-status'].textContent, /service unavailable/);

    // References have loading + permanent error/retry and ignore old IDs/pages.
    const refs = deferred();
    queue.push(() => refs.promise);
    const refRequest = run("viewPersonaReferences('persona_001')");
    assert.match(elements['persona-references'].textContent, /正在加载/);
    refs.resolve({ references: [] });
    await refRequest;
    assert.match(elements['persona-references'].innerHTML, /无引用/);
    queue.push(fail);
    await run("viewPersonaReferences('persona_001')");
    assert.match(elements['persona-references'].textContent, /引用加载失败/);
    queue.push(ok({ references: [] }));
    await elements['persona-references'].children[0].click();
    const oldRefs = deferred();
    queue.push(() => oldRefs.promise, ok({ references: [] }));
    const oldRefRequest = run("viewPersonaReferences('persona_001')");
    await run("viewPersonaReferences('persona_002')");
    oldRefs.resolve({ references: [] });
    await oldRefRequest;
    assert.match(elements['persona-references'].innerHTML, /persona_002/);

    // Fast Bot switch: old config cannot recreate old form/options.
    const oldBot = deferred();
    queue.push(() => oldBot.promise, ok({ bot_id: 'bot_002', persona_id: 'persona_001' }), ok(presets));
    const oldBotRequest = run('loadBotConfig()');
    elements['config-bot-select'].value = 'bot_002';
    await run('loadBotConfig()');
    oldBot.resolve({ bot_id: 'bot_001', persona_id: 'persona_002' });
    await oldBotRequest;
    assert.equal(elements['config-bot-form'].dataset.botId, 'bot_002');
    assert.equal(run('botConfigCache.bot_id'), 'bot_002');
    assert.equal(queue.length, 0);
    console.log('Native persona frontend regressions passed');
  } catch (error) { console.error(error); process.exitCode = 1; }
});
'''
        result = subprocess.run([node, "-e", harness], input=json.dumps(script), text=True, encoding="utf-8",
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
