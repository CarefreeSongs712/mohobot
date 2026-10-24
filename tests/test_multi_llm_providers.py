"""多提供商(providers)回归: 每套 base_url + api_key 并存 + 按 bot 路由。

场景: 同时配置 snowluma 与 deepseek 两套 key, 全局默认走 snowluma,
某个 bot 用 chat_provider_override 走 deepseek; 未配置 providers 时回退旧字段。
"""

from __future__ import annotations

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


def _llm_with_providers() -> LLMConfig:
    llm = LLMConfig()
    llm.providers = _normalize_providers([
        {
            "id": "snowluma", "name": "SnowLuma",
            "base_url": "https://api.snowluma.example/v1",
            "api_key": "sk-snow", "default_model": "gpt-4o",
            "models": ["gpt-4o", "claude-sonnet-4"],
        },
        {
            "id": "deepseek", "name": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-deep", "default_model": "deepseek-chat",
        },
    ])
    llm.chat_provider = "snowluma"
    llm.chat_model = "global-model"
    return llm


# ── 1. 配置层 ────────────────────────────────────────────────

def test_normalize_providers_filters_invalid():
    out = _normalize_providers([
        {"id": "a", "name": "A", "models": "m1, m2"},
        {"id": "a", "name": "dup"},          # 重复 id 丢弃
        {"name": "no-id"},                   # 无 id 丢弃
        "not-a-dict",                        # 非 dict 丢弃
        {"id": "b", "models": ["x", " ", "y"]},
    ])
    assert [p["id"] for p in out] == ["a", "b"]
    assert out[0]["models"] == ["m1", "m2"]
    assert out[1]["models"] == ["x", "y"]
    # 非法输入不炸
    assert _normalize_providers(None) == []
    assert _normalize_providers("bad") == []


def test_provider_config_roundtrip():
    cfg = GlobalConfig()
    cfg.llm = _llm_with_providers()
    cfg.llm.vision_provider = "deepseek"
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.yaml"
        cfg.save(path)
        loaded = GlobalConfig.load(path)
        ids = [p["id"] for p in loaded.llm.providers]
        assert ids == ["snowluma", "deepseek"], ids
        assert loaded.llm.chat_provider == "snowluma"
        assert loaded.llm.vision_provider == "deepseek"
        assert loaded.llm.providers[0]["api_key"] == "sk-snow"
        # to_dict 也要带上(WebUI 依赖)
        d = cfg.to_dict()["llm"]
        assert len(d["providers"]) == 2 and d["chat_provider"] == "snowluma"


def test_bot_provider_override_roundtrip():
    bot = BotConfig.load(Path("/nonexistent")) if False else BotConfig()
    bot.bot_id = "bot_001"
    bot.chat_provider_override = "deepseek"
    bot.vision_provider_override = "snowluma"
    data = bot.to_dict()
    assert data["chat_provider_override"] == "deepseek"
    assert data["vision_provider_override"] == "snowluma"


# ── 2. LLMService 解析层 ────────────────────────────────────

def _service(llm: LLMConfig):
    from mohobot.llm_service import LLMService

    svc = object.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=llm)
    svc._provider_clients = {
        "snowluma": SimpleNamespace(name="snow-client"),
        "deepseek": SimpleNamespace(name="deep-client"),
    }
    svc._chat_client = SimpleNamespace(name="legacy-chat")
    svc._vision_client = SimpleNamespace(name="legacy-vision")
    svc._emotion_client = None
    svc._available = True
    svc._vision_available = True
    return svc


def test_specs_normalized_with_fallbacks():
    from mohobot.llm_service import LLMService

    llm = _llm_with_providers()
    specs = LLMService._provider_specs(llm)
    assert set(specs) == {"snowluma", "deepseek"}
    assert specs["snowluma"]["base_url"] == "https://api.snowluma.example/v1"
    assert specs["snowluma"]["default_model"] == "gpt-4o"
    # 缺 api_key 的项不进入(无法建 client)
    llm2 = LLMConfig()
    llm2.providers = [{"id": "x", "base_url": "https://x", "api_key": ""}]
    assert LLMService._provider_specs(llm2) == {}


def test_provider_without_base_url_inherits_global():
    """只填 api_key 的 provider 复用 chat_base_url(同端点换 key 的常见用法)。

    对抗验证时发现文档写成"两者都必须有", 与实现(回退)不符; 这里用测试锁住
    真实语义, 避免后续被误改。
    """
    llm = LLMConfig(chat_base_url="https://global.example/v1", chat_api_key="sk-global")
    llm.providers = _normalize_providers([
        {"id": "keyonly", "api_key": "sk-alt", "default_model": "m-alt"},
    ])
    specs = None
    from mohobot.llm_service import LLMService

    specs = LLMService._provider_specs(llm)
    assert "keyonly" in specs, specs
    assert specs["keyonly"]["base_url"] == "https://global.example/v1"
    assert specs["keyonly"]["api_key"] == "sk-alt"

    # 既没有 key 也没有地址 → 排除
    llm2 = LLMConfig(chat_api_key="")
    llm2.providers = _normalize_providers([{"id": "empty"}])
    assert LLMService._provider_specs(llm2) == {}


def test_chat_target_uses_global_provider():
    svc = _service(_llm_with_providers())
    client, model, pid = svc._resolve_chat_target(None)
    assert pid == "snowluma"
    assert client.name == "snow-client"
    assert model == "gpt-4o", "应使用提供商 default_model"


def test_chat_target_bot_override():
    svc = _service(_llm_with_providers())
    bot = BotConfig(bot_id="bot_002", chat_provider_override="deepseek")
    client, model, pid = svc._resolve_chat_target(bot)
    assert pid == "deepseek"
    assert client.name == "deep-client"
    assert model == "deepseek-chat"


def test_chat_target_bot_model_override_wins():
    svc = _service(_llm_with_providers())
    bot = BotConfig(
        bot_id="bot_003", chat_provider_override="deepseek",
        chat_model_override="deepseek-reasoner",
    )
    _, model, pid = svc._resolve_chat_target(bot)
    assert pid == "deepseek"
    assert model == "deepseek-reasoner"


def test_chat_target_falls_back_to_legacy_when_no_provider():
    llm = LLMConfig(chat_model="legacy-model")
    svc = _service(llm)
    client, model, pid = svc._resolve_chat_target(None)
    assert pid == "" and client.name == "legacy-chat"
    assert model == "legacy-model"


def test_chat_target_unknown_provider_falls_back():
    llm = _llm_with_providers()
    llm.chat_provider = "not-exist"
    svc = _service(llm)
    client, model, pid = svc._resolve_chat_target(None)
    assert pid == "" and client.name == "legacy-chat"
    assert model == "global-model"


def test_vision_target_uses_provider_default_model():
    llm = _llm_with_providers()
    llm.vision_provider = "deepseek"
    llm.vision_model = "global-vision"
    svc = _service(llm)
    client, model, pid = svc._resolve_vision_target(None)
    assert pid == "deepseek" and client.name == "deep-client"
    assert model == "deepseek-chat"  # provider default_model

    # bot 覆盖 vision 模型 > provider 默认
    bot = BotConfig(bot_id="b", vision_model_override="vision-x")
    _, model2, _ = svc._resolve_vision_target(bot)
    assert model2 == "vision-x"


# ── 3. 可用性判定 ───────────────────────────────────────────

def test_install_clients_marks_available_with_only_providers():
    """只有 providers(无旧 chat_api_key)时也应视为 LLM 可用。"""
    from mohobot.llm_service import LLMService

    llm = _llm_with_providers()
    svc = object.__new__(LLMService)
    svc._provider_clients = {}
    svc._client_identities = {}
    LLMService._install_clients(
        svc, llm,
        {"chat": None, "vision": None, "emotion": None},
        {"chat": None, "vision": None, "emotion": None},
        {"snowluma": SimpleNamespace(name="c1")},
    )
    assert svc._available is True, "多提供商存在时不应判定为不可用"


def test_install_clients_vision_available_via_provider():
    from mohobot.llm_service import LLMService

    llm = _llm_with_providers()
    llm.vision_provider = "deepseek"
    svc = object.__new__(LLMService)
    LLMService._install_clients(
        svc, llm,
        {"chat": None, "vision": None, "emotion": None},
        {"chat": None, "vision": None, "emotion": None},
        {"deepseek": SimpleNamespace(name="c2")},
    )
    assert svc._vision_available is True


# ── 4. WebUI API 层(掩码回填) ───────────────────────────────

def test_providers_api_masks_keys_and_keeps_original():
    from fastapi.testclient import TestClient

    from mohobot.web_panel.app import WebPanel

    def _pwd_hash(password: str) -> str:
        import hashlib
        import secrets

        salt = secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
        return f"pbkdf2_sha256${salt}${h}"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = GlobalConfig()
        cfg.data_dir = str(tmp / "data")
        cfg.llm = _llm_with_providers()
        cfg_path = tmp / "global.yaml"
        cfg.save(cfg_path)

        panel = WebPanel(
            host="127.0.0.1", port=0, username="admin",
            password_hash=_pwd_hash("pw"),
            data_dir=str(tmp / "data"), config_path=str(cfg_path),
        )
        client = TestClient(panel._app)
        r = client.post("/api/login", json={"username": "admin", "password": "pw"})
        auth = {"Authorization": "Bearer " + r.json()["token"]}

        # GET: Key 应被掩码
        r = client.get("/api/providers", headers=auth)
        assert r.status_code == 200, r.text
        items = r.json()["providers"]
        assert [p["id"] for p in items] == ["snowluma", "deepseek"]
        assert "sk-snow" not in r.text, "API Key 不得明文返回"

        # PUT: 只改 name/base_url, api_key 留空 → 保留原 key
        data = {
            "providers": [
                {"id": "snowluma", "name": "SnowLuma-改", "base_url": "https://new.example/v1",
                 "api_key": ""},
                {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com",
                 "api_key": ""},
            ],
            "chat_provider": "deepseek",
            "vision_provider": "",
            "emotion_provider": "",
        }
        r = client.put("/api/providers", headers=auth, json={"data": data})
        assert r.status_code == 200, r.text

        reloaded = GlobalConfig.load(cfg_path)
        by_id = {p["id"]: p for p in reloaded.llm.providers}
        assert by_id["snowluma"]["name"] == "SnowLuma-改"
        assert by_id["snowluma"]["api_key"] == "sk-snow", "留空应保留原 Key"
        assert reloaded.llm.chat_provider == "deepseek"
