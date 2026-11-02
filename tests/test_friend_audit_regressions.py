"""friend-audit 回归补充(仅存在于审查副本, 不进仓库)。

覆盖 5 个变异幸存点:
  R1 _safe_id 拒绝路径穿越字符      (变异 M21)
  R2 Bearer token 过期后必须失效    (变异 M23)
  R3 chat_max_tokens 必须 131072 夹紧 (变异 M18)
  R4 summary 块记 1 整轮            (变异 M32)
  R5 proactive 过去/过近排期必须拒绝 (变异 M25)

运行: .venv/bin/python -m pytest -q tests/test_friend_audit_regressions.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── R1: _safe_id 拒绝路径穿越字符 ────────────────────────────
def test_safe_id_rejects_traversal_chars() -> None:
    from fastapi import HTTPException

    from mohobot.web_panel.app import WebPanel

    for bad in ("../etc", "a/b", "a\\b", "..", "./x", "bot_001/../../history"):
        try:
            WebPanel._safe_id(bad, "bot_id")
        except HTTPException as e:
            assert e.status_code == 400, bad
        else:
            raise AssertionError(f"_safe_id 接受了危险输入: {bad!r}")
    # 合法输入不受影响
    assert WebPanel._safe_id("bot_001", "bot_id") == "bot_001"
    assert WebPanel._safe_id("A-b_9", "bot_id") == "A-b_9"
    print("[R1] _safe_id 拒绝 ../ 与 / 与 \\ OK")


# ── R2: token 过期必须失效 ───────────────────────────────────
async def test_bearer_token_expiry_enforced() -> None:
    import httpx

    from mohobot.web_panel.app import WebPanel

    tmp = tempfile.mkdtemp(prefix="fa_tok_")
    panel = WebPanel(
        host="127.0.0.1", port=0, username="admin",
        password_hash=WebPanel._hash_password("right-pass"),
        data_dir=tmp, config_path=str(Path(tmp) / "none.yaml"),
    )
    app = panel._app

    import httpx as _httpx
    token = "a" * 64
    panel._tokens[token] = time.time() + 3600  # 未过期
    expired = "b" * 64
    panel._tokens[expired] = time.time() - 1   # 已过期

    async def run():
        transport = _httpx.ASGITransport(app=app)
        async with _httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            ok = await c.get("/api/health",
                             headers={"Authorization": f"Bearer {token}"})
            bad = await c.get("/api/health",
                              headers={"Authorization": f"Bearer {expired}"})
            none = await c.get("/api/health")
        return ok.status_code, bad.status_code, none.status_code

    ok_code, bad_code, none_code = await run()
    assert ok_code == 200, ok_code
    assert bad_code == 401, f"过期 token 必须被拒绝, got {bad_code}"
    assert none_code == 401
    print("[R2] token 过期失效 OK")


# ── R3: chat_max_tokens 上限夹紧 ─────────────────────────────
async def test_chat_max_tokens_clamped() -> None:
    import mohobot.llm_service as ls
    from mohobot.models.config import GlobalConfig

    captured: dict = {}

    class FakeCompletions:
        async def create(self, **kw):
            captured.update(kw)

            async def gen():
                if False:
                    yield None

            return gen()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    svc = ls.LLMService.__new__(ls.LLMService)
    svc._cfg = GlobalConfig()
    svc._cfg.llm.chat_max_tokens = 999999
    svc._available = True
    svc._resolve_chat_target = lambda bot_config: (FakeClient(), "fake-model", "fake-provider")
    svc._current_tools_schemas = lambda: []
    svc._song_annotator = None
    svc._emotion = None

    async def fake_build(*a, **k):
        return [{"role": "user", "content": "hi"}]

    svc._build_messages = fake_build

    async def run():
        async for _ in svc.chat_stream(
            bot_id="bot_001",
            event=None,
            context=[{"role": "user", "content": "hi"}],
            raw_event={},
        ):
            pass

    await run()
    assert captured.get("max_tokens") == 131072, captured.get("max_tokens")
    print("[R3] chat_max_tokens 夹紧 131072 OK")


# ── R4: summary 块记 1 整轮 ──────────────────────────────────
def test_summary_counts_as_full_round() -> None:
    from mohobot.context_manager import ContextManager

    assert ContextManager._count_rounds([{"role": "summary", "content": "s"}]) == 1
    assert ContextManager._count_rounds(
        [{"role": "summary", "content": "s"},
         {"role": "u", "content": "a"}, {"role": "assistant", "content": "b"}]) == 2
    assert ContextManager._count_rounds(
        [{"role": "u", "content": "a"}, {"role": "assistant", "content": "b"}]) == 1
    print("[R4] summary 记 1 整轮 OK")


# ── R5: proactive 过去/过近排期必须拒绝 ──────────────────────
def test_proactive_schedule_rejects_past() -> None:
    from plugins.proactive_chat.proactive_chat_core.engine import ProactiveEngine

    eng = ProactiveEngine.__new__(ProactiveEngine)
    now = time.time()
    # 30 秒前 → 拒绝
    past = eng._resolve_schedule("2020-01-01 08:00", {"max_schedule_days": 7}, now)
    assert past is None, "过去时间必须被拒绝"
    # 10 秒后(< now+60) → 拒绝
    near = time.strftime("%Y-%m-%d %H:%M", time.localtime(now + 10))
    near_utc8 = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + 10 + 8 * 3600))
    assert eng._resolve_schedule(near_utc8, {"max_schedule_days": 7}, now) is None, \
        "过近时间必须被拒绝"
    # 无法解析 → 拒绝
    assert eng._resolve_schedule("明天下午", {"max_schedule_days": 7}, now) is None
    print("[R5] 过去/过近/非法排期拒绝 OK")
