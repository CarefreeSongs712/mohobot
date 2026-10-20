"""PR #73 独立审查问题的热修回归。

对应独立审查发现的：
- P1 N1: GET /api/config 明文返回 provider api_key
- P2 N2: bot/全局 provider 无效时跳过全局 provider → provider-only 部署不可用
- P2 N3: provider client 从不退休/关闭
- P2 N4: bot vision_provider_override 未接线
- P2 N5: emotion_provider 未接线
- P3: provider 空 key 继承 chat key 并发往不同端点（Key 外泄风险）
- P3: PUT /api/providers 清空 models；前端缓存不刷新/失败清空
"""

from __future__ import annotations

import hashlib
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import (  # noqa: E402
    BotConfig,
    GlobalConfig,
    LLMConfig,
    _normalize_providers,
)


def _llm() -> LLMConfig:
    llm = LLMConfig()
    llm.providers = _normalize_providers([
        {"id": "snowluma", "name": "S", "base_url": "https://snow.example/v1",
         "api_key": "sk-SUPER-SECRET-snow", "default_model": "gpt-4o"},
        {"id": "deepseek", "name": "D", "base_url": "https://api.deepseek.com",
         "api_key": "sk-SECRET-deep", "default_model": "deepseek-chat"},
    ])
    llm.chat_provider = "snowluma"
    return llm


def _panel(tmp: Path, cfg: GlobalConfig):
    from fastapi.testclient import TestClient

    from mohobot.web_panel.app import WebPanel

    salt = secrets.token_hex(16)
    pw_hash = "pbkdf2_sha256$" + salt + "$" + hashlib.pbkdf2_hmac(
        "sha256", b"pw", salt.encode(), 100000
    ).hex()
    cfg.data_dir = str(tmp / "data")
    cfg_path = tmp / "global.yaml"
    cfg.save(cfg_path)
    panel = WebPanel(host="127.0.0.1", port=0, username="admin",
                     password_hash=pw_hash, data_dir=str(tmp / "data"),
                     config_path=str(cfg_path))
    client = TestClient(panel._app)
    tok = client.post("/api/login", json={"username": "admin", "password": "pw"}).json()["token"]
    return client, {"Authorization": "Bearer " + tok}, cfg_path


# ── P1 N1: /api/config 不得泄漏 provider key ───────────────

def test_config_endpoint_masks_provider_keys():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = GlobalConfig()
        cfg.llm = _llm()
        client, auth, _ = _panel(tmp, cfg)
        resp = client.get("/api/config", headers=auth)
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "sk-SUPER-SECRET-snow" not in body, "P1 回归: /api/config 泄漏 snowluma key"
        assert "sk-SECRET-deep" not in body, "P1 回归: /api/config 泄漏 deepseek key"
        # 结构仍在(前端要能展示提供商列表)
        data = resp.json()
        assert [p["id"] for p in data["llm"]["providers"]] == ["snowluma", "deepseek"]
        # 顶层 llm key 仍按原逻辑掩码
        assert data["llm"]["chat_api_key"] in ("", "*" * 8) or "secret" not in data["llm"]["chat_api_key"]


def test_providers_endpoint_still_masked():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = GlobalConfig()
        cfg.llm = _llm()
        client, auth, _ = _panel(tmp, cfg)
        body = client.get("/api/providers", headers=auth).text
        assert "sk-SUPER-SECRET" not in body
        assert "sk-SECRET-deep" not in body


# ── P2 N2: 无效 override 应回退到全局 provider, 而非直接 legacy ──

def _svc(llm: LLMConfig):
    from mohobot.llm_service import LLMService

    svc = object.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=llm)
    svc._provider_clients = {
        "snowluma": SimpleNamespace(name="snow"),
        "deepseek": SimpleNamespace(name="deep"),
    }
    svc._chat_client = None
    svc._vision_client = None
    svc._emotion_client = None
    svc._available = True
    svc._vision_available = True
    return svc


def test_invalid_bot_override_falls_back_to_global_provider():
    svc = _svc(_llm())
    bot = BotConfig(bot_id="b", chat_provider_override="does-not-exist")
    client, model, pid = svc._resolve_chat_target(bot)
    assert pid == "snowluma", pid
    assert client.name == "snow"
    assert model == "gpt-4o", model


def test_invalid_global_provider_falls_back_to_legacy():
    llm = LLMConfig(chat_model="legacy")
    llm.chat_provider = "nope"
    svc = _svc(llm)
    svc._chat_client = SimpleNamespace(name="legacy-client")
    client, model, pid = svc._resolve_chat_target(None)
    assert (pid, model) == ("", "legacy")
    assert client.name == "legacy-client"


# ── P2 N3: provider client 必须退休并关闭 ─────────────────

async def test_sync_config_retires_old_provider_clients():
    """真实调用 sync_config: 旧 provider client 必须进入退休列表, close() 后关闭。

    (第一版测试手工复刻了退休循环, 变异"删除退休循环"仍能通过 → 不承重;
     这里改为调用真实 sync_config, 只桩掉会建网络连接的 prepare/install。)
    """
    from mohobot.llm_service import LLMService

    class FakeClient:
        def __init__(self, name):
            self.name = name
            self.closed = False

        async def close(self):
            self.closed = True

    cfg = GlobalConfig()
    cfg.llm = _llm()
    old_snow, old_deep = FakeClient("old-snow"), FakeClient("old-deep")

    svc = object.__new__(LLMService)
    svc._cfg = cfg
    svc._provider_clients = {"snowluma": old_snow, "deepseek": old_deep}
    svc._chat_client = None
    svc._vision_client = None
    svc._emotion_client = None
    svc._retired_clients = []
    svc._client_identities = {}

    # 不建真实网络 client: 桩掉 prepare(返回空 provider 集)与 install
    svc._prepare_clients = lambda llm, created: (
        {"chat": None, "vision": None, "emotion": None},
        {"chat": None, "vision": None, "emotion": None},
        {},
    )

    def _install(llm, identities, clients, provider_clients=None):
        svc._provider_clients = provider_clients or {}
        svc._chat_client = svc._vision_client = svc._emotion_client = None

    svc._install_clients = _install

    await svc.sync_config(cfg)

    assert old_snow in svc._retired_clients, "旧 provider client 未退休(snowluma)"
    assert old_deep in svc._retired_clients, "旧 provider client 未退休(deepseek)"

    await svc.close()
    assert old_snow.closed and old_deep.closed, "close() 未关闭 provider client"
    assert svc._provider_clients == {}


# ── P2 N5: emotion provider 接线 ───────────────────────────

async def test_emotion_uses_provider_when_legacy_client_absent():
    from mohobot.llm_service import LLMService

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="开心"))],
                usage=None,
            )

    class ProvClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    llm = LLMConfig(chat_model="m", emotion_model="", emotion_provider="deepseek")
    llm.providers = _normalize_providers([
        {"id": "deepseek", "base_url": "https://api.deepseek.com",
         "api_key": "sk-deep", "default_model": "deepseek-chat"},
    ])
    svc = object.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=llm)
    svc._provider_clients = {"deepseek": ProvClient()}
    svc._chat_client = None
    svc._vision_client = None
    svc._emotion_client = None  # legacy 缺失

    async def _noop(*a, **k):
        return None

    svc._record_usage = _noop
    out = await LLMService.analyze_emotion(svc, "分析这句话")
    assert out == "开心", out
    assert captured["model"] == "deepseek-chat", captured["model"]


# ── P2 N4: bot vision provider 接线 ───────────────────────

async def test_vision_bot_override_used_by_describe_image():
    from mohobot.llm_service import LLMService

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="一只猫"))],
                usage=None,
            )

    class ProvClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    llm = LLMConfig(chat_model="m", vision_model="global-vision")
    llm.providers = _normalize_providers([
        {"id": "visionprov", "base_url": "https://v", "api_key": "sk-v",
         "default_model": "v-model"},
    ])
    svc = object.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=llm)
    svc._provider_clients = {"visionprov": ProvClient()}
    svc._chat_client = None
    svc._vision_client = None
    svc._emotion_client = None
    svc._vision_available = True

    async def _noop(*a, **k):
        return None

    svc._record_usage = _noop
    bot = BotConfig(bot_id="b", vision_provider_override="visionprov")
    out = await LLMService.describe_image(svc, "http://img/x.png", bot_config=bot)
    assert out == "一只猫", out
    assert captured["model"] == "v-model", captured["model"]


# ── P3: 空 key 不得发给不同端点 ────────────────────────────

def test_provider_without_key_does_not_inherit_to_other_endpoint():
    from mohobot.llm_service import LLMService

    llm = LLMConfig(chat_api_key="sk-main", chat_base_url="https://api.deepseek.com")
    llm.providers = _normalize_providers([
        # 不同端点 + 空 key → 跳过(否则主 key 被发往第三方)
        {"id": "third", "base_url": "https://evil.example/v1", "api_key": ""},
        # 同一端点 + 空 key → 允许继承
        {"id": "same", "base_url": "https://api.deepseek.com/", "api_key": ""},
        # 空 base_url + 空 key → 继承 chat 端点与 key
        {"id": "inherit", "api_key": ""},
    ])
    specs = LLMService._provider_specs(llm)
    assert "third" not in specs, "不同端点不得继承主 key"
    assert specs["same"]["api_key"] == "sk-main"
    assert specs["inherit"]["base_url"] == "https://api.deepseek.com"
    assert specs["inherit"]["api_key"] == "sk-main"


# ── P3: PUT /api/providers 保留 models ─────────────────────

def test_put_providers_preserves_models():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = GlobalConfig()
        cfg.llm = _llm()
        cfg.llm.providers[0]["models"] = ["gpt-4o", "claude-sonnet-4"]
        client, auth, cfg_path = _panel(tmp, cfg)

        data = {"providers": [
            {"id": "snowluma", "name": "S2", "base_url": "https://snow.example/v1",
             "api_key": "", "models": []},   # 前端行编辑器不传 models
            {"id": "deepseek", "name": "D", "base_url": "https://api.deepseek.com",
             "api_key": ""},
        ], "chat_provider": "snowluma"}
        assert client.put("/api/providers", headers=auth, json={"data": data}).status_code == 200
        reloaded = GlobalConfig.load(cfg_path)
        by = {p["id"]: p for p in reloaded.llm.providers}
        assert by["snowluma"]["models"] == ["gpt-4o", "claude-sonnet-4"], by["snowluma"]["models"]
        assert by["snowluma"]["api_key"] == "sk-SUPER-SECRET-snow"


# ── P3: 前端静态断言(缓存刷新/失败不清空) ─────────────────

def test_frontend_refreshes_provider_cache_safely():
    html = (Path(__file__).resolve().parent.parent
            / "mohobot" / "web_panel" / "static" / "index.html").read_text(encoding="utf-8")
    # 保存后刷新 Bot 配置页缓存
    assert "_botProviderCache = providerCache.providers" in html
    # 加载失败时不把缓存清空
    assert "失败时保留旧缓存" in html or "保留旧缓存" in html
