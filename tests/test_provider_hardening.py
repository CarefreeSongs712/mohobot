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


# ── PR #74 复审跟进：N13 / N14 / N15 / N16 / N17 ─────────────

def test_empty_chat_base_url_does_not_leak_key_to_other_endpoint():
    """N14: chat_base_url 为空时, 空 key provider 也不得继承主 key 到别的端点。"""
    from mohobot.llm_service import LLMService

    llm = LLMConfig(chat_api_key="sk-MAIN-OPENAI", chat_base_url="")
    llm.providers = _normalize_providers([
        {"id": "third", "base_url": "https://evil.example/v1", "api_key": ""},
        {"id": "inherit", "api_key": ""},   # 无 base_url → 跟随(空)chat 端点
    ])
    specs = LLMService._provider_specs(llm)
    assert "third" not in specs, "N14: 主 key 被继承到不同端点"
    # 没有可用端点时 inherit 也不该带出 key
    assert "inherit" not in specs


async def test_vision_available_with_per_bot_provider_only():
    """N13: 全局 vision_provider 留空、仅 per-bot override 时识图也应可用。"""
    from mohobot.llm_service import LLMService

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="一只狗"))],
                usage=None,
            )

    class ProvClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    llm = LLMConfig(chat_model="m", vision_model="global-vision", vision_provider="")
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
    svc._provider_identity_cache = {}

    # 用真实 _install_clients 计算可用性(全局 vision_provider 为空)
    LLMService._install_clients(
        svc, llm,
        {"chat": None, "vision": None, "emotion": None},
        {"chat": None, "vision": None, "emotion": None},
        {"visionprov": svc._provider_clients["visionprov"]},
    )

    async def _noop(*a, **k):
        return None

    svc._record_usage = _noop
    bot = BotConfig(bot_id="b", vision_provider_override="visionprov")
    out = await LLMService.describe_image(svc, "http://img/x.png", bot_config=bot)
    assert out == "一只狗", out
    assert captured["model"] == "v-model"


async def test_close_closes_current_provider_clients():
    """N16: close() 必须关闭**当前**(未退休)的 provider client(变异承重)。"""
    from mohobot.llm_service import LLMService

    class FakeClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    current = FakeClient()
    svc = object.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=LLMConfig())
    svc._provider_clients = {"p": current}
    svc._provider_identities = {"p": ("sk", "https://x")}
    svc._chat_client = svc._vision_client = svc._emotion_client = None
    svc._retired_clients = []       # 没有退休 client → 只能靠 close 的 provider 循环
    svc._available = True
    svc._vision_available = True
    svc._owns_usage_recorder = False
    await svc.close()
    assert current.closed, "close() 未关闭当前 provider client"
    assert svc._provider_clients == {}
    assert svc._provider_identity_cache == {}


async def test_vision_callback_passes_bot_config():
    """N17: LLMService._vision_callback(bot) 必须把 bot 配置传进 describe_image_file。"""
    from mohobot.llm_service import LLMService

    seen = {}

    svc = object.__new__(LLMService)

    async def fake_describe_file(local_path, max_tokens=None, bot_config=None):
        seen["bot_config"] = bot_config
        return "描述"

    svc.describe_image_file = fake_describe_file
    bot = BotConfig(bot_id="b", vision_provider_override="p2")
    cb = LLMService._vision_callback(svc, bot)
    out = await cb("http://img/x.png", "/tmp/x.png")
    assert out == "描述"
    assert seen["bot_config"] is bot, "N17: _vision_callback 未传 bot_config"


async def test_message_handler_vision_callback_passes_bot_config():
    """N17: MessageHandler._vision_callback(bot_id) 应取该 bot 配置并传下去。"""
    import mohobot.message_handler as mh

    seen = {}

    class FakeLLM:
        async def describe_image_file(self, local_path, max_tokens=None, bot_config=None):
            seen["bot_config"] = bot_config
            return "描述"

    handler = object.__new__(mh.MessageHandler)
    handler._llm = FakeLLM()
    handler._bot_config = lambda bot_id: BotConfig(
        bot_id=bot_id, vision_provider_override="p3",
    )
    cb = mh.MessageHandler._vision_callback(handler, "bot_001")
    out = await cb("http://img/x.png", "/tmp/x.png")
    assert out == "描述"
    assert seen["bot_config"] is not None
    assert seen["bot_config"].vision_provider_override == "p3"


async def test_sync_config_reuses_unchanged_provider_clients():
    """N15: provider 配置未变时, sync_config 不应重建 client。"""
    from mohobot.llm_service import LLMService

    cfg = GlobalConfig()
    cfg.llm = _llm()
    svc = object.__new__(LLMService)
    svc._cfg = cfg
    svc._provider_clients = {}
    svc._provider_identity_cache = {}
    svc._chat_client = svc._vision_client = svc._emotion_client = None
    svc._retired_clients = []
    svc._client_identities = {}
    svc._install_clients = LLMService._install_clients.__get__(svc, LLMService)

    # 第一次: prepare 造出两个 provider client
    class FakeClient:
        def __init__(self, name):
            self.name = name

        async def close(self):
            pass

    created: list[FakeClient] = []

    def prepare(llm, created_holder):
        provider_clients = {}
        for pid in ("snowluma", "deepseek"):
            c = FakeClient(pid)
            created.append(c)
            provider_clients[pid] = c
        return (
            {"chat": None, "vision": None, "emotion": None},
            {"chat": None, "vision": None, "emotion": None},
            provider_clients,
        )

    svc._prepare_clients = prepare
    await svc.sync_config(cfg)
    first = dict(svc._provider_clients)
    assert len(created) == 2

    # 第二次: 用真实 _prepare_clients 的复用池行为 → 不应新建
    svc._prepare_clients = LLMService._prepare_clients.__get__(svc, LLMService)
    # 真实 _prepare_clients 会构造 AsyncOpenAI; 用桩替换构造, 只验证复用逻辑
    import mohobot.llm_service as lsvc

    orig = lsvc.AsyncOpenAI
    try:
        created_after: list = []

        def fake_openai(api_key=None, base_url=None):
            c = FakeClient(f"{api_key}@{base_url}")
            created_after.append(c)
            return c

        lsvc.AsyncOpenAI = fake_openai
        await svc.sync_config(cfg)
    finally:
        lsvc.AsyncOpenAI = orig

    assert created_after == [], f"N15: 配置未变仍重建了 client: {created_after}"
    assert svc._provider_clients == first


# ── PR #75 复审遗留 P3（N23 / N24 / N25 承重） ─────────────

async def test_prepare_messages_passes_bot_config_to_vision():
    """N17 残留: _prepare_messages 必须把 bot_config 传给视觉描述路径。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.onebot import GroupMessageEvent, Sender

    seen = {}

    svc = object.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=LLMConfig(vision_mode="describe"))
    svc._vision_available = True
    svc._image_cache = None
    svc._song_annotator = None
    svc._news_annotator = None

    async def fake_describe(url, bot_config=None):
        seen["bot_config"] = bot_config
        return "一只猫"

    svc._describe_image_for_text = fake_describe
    bot = BotConfig(bot_id="b", vision_provider_override="p-vision")

    event = GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=2000, group_id=3000,
        message=[{"type": "image", "data": {"url": "http://img/x.png"}}],
        sender=Sender(user_id=2000, nickname="u"),
    )
    svc._build_self_reference = lambda *a, **k: ""
    svc._build_time_context = lambda *a, **k: ""
    svc._build_memory_context = lambda *a, **k: ""
    try:
        await LLMService._prepare_messages(
            svc, "b", event, [], bot, force_describe=True,
        )
    except Exception:
        pass
    assert seen.get("bot_config") is bot, "N17: _prepare_messages 未把 bot_config 传给视觉路径"


async def test_vision_unavailable_does_not_touch_image_cache():
    """N23: 无视觉能力时, 描述路径必须在下载/编码图片之前提前返回。"""
    from mohobot.llm_service import LLMService

    class ExplodingCache:
        async def get_or_describe(self, *a, **k):
            raise AssertionError("无视觉能力时不应访问 ImageCache(会触发下载/编码)")

    svc = object.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=LLMConfig(vision_mode="describe"))
    svc._vision_available = False
    svc._image_cache = ExplodingCache()
    out = await LLMService._describe_image_for_text(svc, "http://img/x.png", None)
    assert out == ""


async def test_vision_unavailable_warning_is_interpolated():
    """N24: 不可用 warning 必须真正插值(loguru 不用 %s), 且可被断言。"""
    from loguru import logger as _logger

    from mohobot.llm_service import LLMService

    messages: list[str] = []
    handler_id = _logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        svc = object.__new__(LLMService)
        svc._cfg = SimpleNamespace(llm=LLMConfig(vision_provider=""))
        svc._provider_clients = {}
        svc._chat_client = svc._vision_client = svc._emotion_client = None
        svc._vision_available = False
        await LLMService.describe_image(svc, "http://img/x.png")
    finally:
        _logger.remove(handler_id)
    joined = "\n".join(messages)
    assert "识图不可用" in joined, joined
    assert "%s" not in joined and "%r" not in joined, f"loguru 未插值: {joined}"
    assert "clients=False" in joined, joined


async def test_vision_provider_default_model_works_when_global_model_empty():
    """N25: 全局 vision_model 为空时, 提供商 default_model 仍可作为视觉模型。"""
    from mohobot.llm_service import LLMService

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="描述"))],
                usage=None,
            )

    class ProvClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    llm = LLMConfig(chat_model="m", vision_model="")   # 全局视觉模型留空
    llm.providers = _normalize_providers([
        {"id": "p", "base_url": "https://v", "api_key": "sk-v", "default_model": "v-model"},
    ])
    svc = object.__new__(LLMService)
    svc._cfg = SimpleNamespace(llm=llm)
    svc._provider_clients = {"p": ProvClient()}
    svc._chat_client = svc._vision_client = svc._emotion_client = None
    LLMService._install_clients(
        svc, llm,
        {"chat": None, "vision": None, "emotion": None},
        {"chat": None, "vision": None, "emotion": None},
        {"p": svc._provider_clients["p"]},
    )

    async def _noop(*a, **k):
        return None

    svc._record_usage = _noop
    bot = BotConfig(bot_id="b", vision_provider_override="p")
    out = await LLMService.describe_image(svc, "http://img/x.png", bot_config=bot)
    assert out == "描述", out
    assert captured["model"] == "v-model"
