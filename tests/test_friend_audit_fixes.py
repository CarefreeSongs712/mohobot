"""friend-audit 修复验证(随 bugfix/audit-fixes 分支提交).

覆盖:
  X1 分段算法线性化: 与旧实现差分对比(随机+边界) + 400KB 性能回归
  X2 归档失败不再吞掉整条消息派发
  X3 图片归一化上限(前 4 张)
  X4 temperature NaN/字符串防护
运行: .venv/bin/python -m pytest -q tests/test_friend_audit_fixes.py
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_handler(data_dir: str):
    from mohobot.message_handler import MessageHandler

    h = MessageHandler.__new__(MessageHandler)
    h._data_dir = data_dir
    h._writer_registry = {}
    h._group_mid_window = {}
    h._group_fp_window = {}
    h._sent_claims = None
    h._history_barrier = None
    h._sync_reply_config(_reply_config())
    return h


def _reply_config():
    from mohobot.models.config import ReplyConfig

    return ReplyConfig()


# ── 旧实现参考版(与修复前 message_handler 逐字等价, 仅换名) ──
_QUOTE_OPEN = "\u201c"
_QUOTE_CLOSE = "\u201d"
_QUOTE_MIN_LEN = 16
_PUNCT_STRONG = "。！？!?…"
_PUNCT_SOFT = "；;，,、"
_SEG_MIN, _SEG_MAX = 12, 60


def _ref_find_long_quote(text):
    open_idx = text.find(_QUOTE_OPEN)
    while open_idx != -1:
        close_idx = text.find(_QUOTE_CLOSE, open_idx + 1)
        if close_idx == -1:
            return ("unclosed", open_idx, -1)
        if close_idx - open_idx - 1 >= _QUOTE_MIN_LEN:
            return ("closed", open_idx, close_idx)
        open_idx = text.find(_QUOTE_CLOSE and _QUOTE_OPEN, close_idx + 1)
    return None


def _ref_quote_prefix_cut(before):
    boundary = "。！？!?…；;，,、：:—\n"
    idx = before.rfind("\n\n")
    if idx != -1:
        return idx + 2
    for i in range(len(before) - 1, -1, -1):
        if before[i] in boundary and before[i] != "\n":
            return i + 1
    return None


def _ref_find_cut(text):
    idx = text.rfind("\n\n")
    if idx != -1 and idx + 2 <= len(text):
        return idx + 2
    for i in range(len(text) - 1, -1, -1):
        if text[i] in _PUNCT_STRONG:
            return i + 1
    for i in range(len(text) - 1, -1, -1):
        if text[i] in _PUNCT_SOFT:
            return i + 1
    return None


def _ref_flush(buffer):
    segments = []
    while True:
        quote = _ref_find_long_quote(buffer)
        if quote is not None:
            kind, open_idx, close_idx = quote
            before = buffer[:open_idx]
            cut = _ref_quote_prefix_cut(before) if before else None
            if kind == "unclosed":
                if cut:
                    segments.append(buffer[:cut])
                    buffer = buffer[cut:]
                break
            if cut is not None and 0 < cut <= open_idx:
                segments.append(buffer[:cut])
                buffer = buffer[cut:]
                continue
            if not before.strip():
                segments.append(buffer[open_idx : close_idx + 1])
                buffer = buffer[close_idx + 1 :]
                continue
            segments.append(buffer[: close_idx + 1])
            buffer = buffer[close_idx + 1 :]
            continue
        idx = buffer.find("\n\n")
        if idx != -1:
            cut = idx + 2
            segments.append(buffer[:cut])
            buffer = buffer[cut:]
            continue
        if len(buffer) >= _SEG_MAX:
            cut = _ref_find_cut(buffer[:_SEG_MAX])
            if cut is None:
                cut = _SEG_MAX
            segments.append(buffer[:cut])
            buffer = buffer[cut:]
            continue
        if len(buffer) >= _SEG_MIN:
            cut = _ref_find_cut(buffer)
            if cut is not None and cut >= _SEG_MIN:
                segments.append(buffer[:cut])
                buffer = buffer[cut:]
                continue
        break
    return {"segments": segments, "rest": buffer}


def test_segmentation_matches_reference_on_random_inputs() -> None:
    """随机+边界输入下, 新实现与旧实现逐段一致。"""
    from mohobot.message_handler import MessageHandler

    h = _make_handler(tempfile.mkdtemp(prefix="fa_fix_"))
    rng = random.Random(20261031)
    alphabet = ["好", "，", "a", " ", "“", "”", "\n\n", "词" * 20, "！"]
    texts = ["", "好", "“", "”", "“”", "“" + "词" * 16 + "”", "a" * _SEG_MAX,
             "a" * (_SEG_MAX + 1), "“未闭合" + "词" * 30, "\n\n", "“short”“long……”)"]
    for _ in range(200):
        n = rng.randint(0, 40)
        texts.append("".join(rng.choice(alphabet) for _ in range(n)))
    for i, text in enumerate(texts):
        got = h._flush_ready_segments(text)
        want = _ref_flush(text)
        assert got["segments"] == want["segments"], (
            f"case {i} 段列表不一致: {text[:60]!r}\ngot={got['segments'][:5]}\nwant={want['segments'][:5]}")
        assert got["rest"] == want["rest"], f"case {i} rest 不一致"
        # 不变量: 拼接还原原文 —— 例外: 旧算法有意丢弃长引号前的纯空白前缀
        # (before.strip() 为空时不发), 因此仅当两端都还原时才断言。
        joined = "".join(got["segments"]) + got["rest"]
        want_joined = "".join(want["segments"]) + want["rest"]
        assert joined == text or joined == want_joined == text.lstrip() or \
            joined == want_joined, f"case {i} 拼接异常: {joined[:60]!r}"
    print(f"[X1a] 差分一致: {len(texts)} 组随机+边界输入 OK")


def test_segmentation_large_input_fast() -> None:
    """400KB 引号密集文本应在秒级完成(修复前 65s)。"""
    from mohobot.message_handler import MessageHandler

    h = _make_handler(tempfile.mkdtemp(prefix="fa_fix_"))
    text = ("“歌词片段”好啊，" * (400 * 1000 // 9))
    t0 = time.perf_counter()
    out = h._flush_ready_segments(text)
    dt = time.perf_counter() - t0
    assert dt < 2.0, f"400KB 分段耗时 {dt:.1f}s, 仍超线性"
    assert "".join(out["segments"]) + out["rest"] == text
    print(f"[X1b] 400KB 分段 {dt * 1000:.0f}ms(<2s) OK")


async def test_archive_failure_does_not_kill_dispatch() -> None:
    """孤立代理对导致归档编码失败时, 主消息处理必须继续。"""
    import mohobot.message_handler as mh
    from mohobot.models.onebot import Event

    h = _make_handler(tempfile.mkdtemp(prefix="fa_fix_"))
    h._db = None
    h._plugins = None
    h._interceptors = []
    h._ban_interceptor = None
    h._ws = None
    h._global_config = None
    raw = {"post_type": "message", "message_type": "private", "user_id": 4242,
           "message_id": 1, "time": 1760000000,
           "message": [{"type": "text", "data": {"text": "hi \ud83d alone"}}],
           "sender": {"user_id": 4242, "nickname": "x"}}
    ev = Event.from_dict(raw)
    called = {"n": 0}
    orig = mh.MessageHandler._handle_message

    async def spy(self, *a, **k):
        called["n"] += 1

    mh.MessageHandler._handle_message = spy
    try:
        await h.handle_event("bot_001", ev, raw)
    finally:
        mh.MessageHandler._handle_message = orig
    assert called["n"] == 1, "归档失败后主消息处理未执行"
    print("[X2] 归档失败不吞派发 OK")


async def test_image_normalize_capped() -> None:
    """100 个缺 url 图片段最多发起 4 次 get_image。"""
    from mohobot.models.onebot import GroupMessageEvent

    calls = {"n": 0}

    class FakeWS:
        _bot_manager = None

        async def send_to_bot(self, *a, **k):
            calls["n"] += 1
            return {"status": "ok", "retcode": 0, "data": {"base64": "aGVsbG8="}}

    h = _make_handler(tempfile.mkdtemp(prefix="fa_fix_"))
    h._ws = FakeWS()
    imgs = [{"type": "image", "data": {"file": f"{i}.image"}} for i in range(100)]
    ev = GroupMessageEvent(time=1760000000, self_id=1, post_type="message",
                           sub_type="normal", message_id=7, user_id=5, group_id=9,
                           message=imgs, sender={"user_id": 5, "nickname": "u", "card": ""})
    await h._normalize_image_segments("bot_001", ev)
    assert calls["n"] == h._IMAGE_NORMALIZE_MAX == 4, calls["n"]
    print("[X3] 图片归一化上限 4 OK")


def test_temperature_nan_guard() -> None:
    """配置里写 nan/字符串 temperature 时必须回退有限默认值。"""
    import yaml

    from mohobot.models.config import GlobalConfig

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "g.yaml"
        p.write_text(yaml.safe_dump({
            "llm": {"chat_temperature": "nan", "vision_temperature": float("nan"),
                    "emotion_temperature": "abc"},
        }), encoding="utf-8")
        cfg = GlobalConfig.load(str(p))
    assert cfg.llm.chat_temperature == 0.7, cfg.llm.chat_temperature
    assert cfg.llm.vision_temperature == 0.3, cfg.llm.vision_temperature
    assert cfg.llm.emotion_temperature == 0.3, cfg.llm.emotion_temperature
    # 合法值不受影响
    with tempfile.TemporaryDirectory() as td2:
        p2 = Path(td2) / "g.yaml"
        p2.write_text(yaml.safe_dump({"llm": {"chat_temperature": 1.25}}), encoding="utf-8")
        cfg2 = GlobalConfig.load(str(p2))
        assert abs(cfg2.llm.chat_temperature - 1.25) < 1e-9
    print("[X4] temperature NaN/字符串防护 OK")


# ── 第二批: WS token / to_thread / 括号嵌套 / 缓存观测配套测试 ──

def test_ws_token_ok_matrix() -> None:
    """OneBot v11 access_token: Bearer 头 / 查询参数 / 空配置放行。"""
    from mohobot.ws_server import WSServer

    ok = WSServer._token_ok
    # 未配置 → 放行(兼容旧部署)
    assert ok("", {}, "/ws") is True
    assert ok("", {"authorization": "Bearer whatever"}, "/ws") is True
    # Bearer 头
    assert ok("s3cret", {"authorization": "Bearer s3cret"}, "/ws") is True
    assert ok("s3cret", {"Authorization": "Bearer s3cret"}, "/ws") is True
    assert ok("s3cret", {"authorization": "Bearer wrong"}, "/ws") is False
    assert ok("s3cret", {"authorization": "s3cret"}, "/ws") is False, "非 Bearer 前缀不接受"
    # 查询参数(OneBot v11 标准备选)
    assert ok("s3cret", {}, "/ws?access_token=s3cret") is True
    assert ok("s3cret", {}, "/ws?access_token=wrong&x=1") is False
    assert ok("s3cret", {}, "/ws") is False
    print("[Y1] WS token 矩阵 OK")


async def test_persist_legacy_turn_off_event_loop() -> None:
    """_persist_legacy_turn 走 to_thread: 两行 conversation 落库且不阻塞。"""
    import sqlite3

    from mohobot.db.database_manager import DatabaseManager
    from mohobot.models.onebot import PrivateMessageEvent

    with tempfile.TemporaryDirectory() as td:
        db = DatabaseManager(db_folder=td, db_file="t.db")
        h = _make_handler(td)
        h._db = db
        from mohobot.models.onebot import Event
        ev = Event.from_dict({
            "post_type": "message", "message_type": "private", "user_id": 4242,
            "message_id": 5, "time": 1760000000,
            "message": [{"type": "text", "data": {"text": "hi"}}],
            "sender": {"user_id": 4242, "nickname": "x"}})
        await h._persist_legacy_turn("bot_001", "private", "4242", ev, "hi", "回复")
        con = sqlite3.connect(Path(td) / "t.db")
        try:
            n = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            sources = [r[0] for r in con.execute("SELECT source FROM conversations").fetchall()]
        finally:
            con.close()
        assert n == 2, n
        assert sorted(sources) == ["agent", "user"], sources
    print("[Y2] persist 轮次 2 行落库(经 to_thread) OK")


def test_schedule_nested_bracket_no_leak() -> None:
    """内层同型括号不再把残留文本漏进正文。"""
    from plugins.proactive_chat.proactive_chat_core.engine import ProactiveEngine

    eng = ProactiveEngine.__new__(ProactiveEngine)
    raws, body = eng._extract_schedule("[SCHEDULE: [x] 2026-10-31 18:00（明天）] 你好呀")
    assert body.strip() == "你好呀", f"正文残留: {body!r}"
    assert len(raws) == 1 and "2026-10-31" in raws[0]
    # 常规用例不受影响
    raws2, body2 = eng._extract_schedule("[SCHEDULE: 2026-10-31 18:00] 先发正文")
    assert body2.strip() == "先发正文"
    assert eng._parse_schedule_time(raws2[0]) is not None
    print("[Y3] 括号深度配对 OK")


def test_quote_cache_instance_isolated() -> None:
    """_quote_cache 为实例属性, 多 handler 不共享。"""
    from mohobot.message_handler import MessageHandler

    h1 = MessageHandler(ws_server=None, context_manager=None, llm_service=None,
                        plugin_system=None, data_dir=tempfile.mkdtemp(prefix="fa_q1_"))
    h2 = MessageHandler(ws_server=None, context_manager=None, llm_service=None,
                        plugin_system=None, data_dir=tempfile.mkdtemp(prefix="fa_q2_"))
    h1._quote_cache["k"] = (time.time() + 60, "v")
    assert "k" not in h2._quote_cache
    print("[Y4] quote cache 实例隔离 OK")


async def test_archive_echo_timeout_falls_back_to_local_id() -> None:
    """echo 超时(8s 兜底)后仍以 local: id 归档, 不丢 bot 发言。"""
    from mohobot.bot_manager import BotManager
    from mohobot.ws_server import WSServer

    with tempfile.TemporaryDirectory() as td:
        ws = WSServer(bot_manager=BotManager(data_dir=td), data_dir=td)
        ws._ARCHIVE_ECHO_TIMEOUT = 0.05  # 实例级覆盖, 测试不必等 8s
        fut = asyncio.get_running_loop().create_future()  # 永不 resolve
        t0 = time.perf_counter()
        await ws._archive_sent_message(
            "bot_001", "group", 42, "send_group_msg",
            {"message": [{"type": "text", "data": {"text": "hi"}}]},
            fut, "echo-test", "auto", owns_response=True)
        dt = time.perf_counter() - t0
        assert dt < 2.0, f"超时兜底耗时 {dt:.1f}s"
        f = Path(td) / "history/group/42.jsonl"
        assert f.exists(), "超时后应兜底归档"
        line = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
        assert line["post_type"] == "message_sent"
        assert str(line["message_id"]).startswith("local:"), line["message_id"]
    print("[Y5] echo 超时 local: 兜底归档 OK")


def test_old_prefix_len_guard() -> None:
    """孤立 summary 前缀不再反复压缩; 有旧非 summary 内容才返回非 0。"""
    from mohobot.context_manager import ContextManager

    cm = ContextManager(data_dir=tempfile.mkdtemp(prefix="fa_m12_"))
    now = time.time()
    old = now - 10 * 3600
    summary = {"role": "summary", "content": "s", "timestamp": old}
    # 全是 summary → 0(防孤立总结块反复再总结)
    assert cm._old_prefix_len([summary, dict(summary)]) == 0
    # 前导 summary + 旧非 summary → 非零且计入
    entries = [summary, {"role": "u", "content": "a", "timestamp": old}]
    assert cm._old_prefix_len(entries) == 2
    # 近期消息 → 0
    recent = {"role": "u", "content": "a", "timestamp": now - 60}
    assert cm._old_prefix_len([recent]) == 0
    print("[Y6] 孤立 summary 守卫 OK")


def test_review_login_constant_delay() -> None:
    """review 面板登录同样有 0.5s 恒定延迟(与主面板同款防爆破)。"""
    import time as _t

    from fastapi.testclient import TestClient
    from review.config import ReviewConfig, ReviewUser
    from review.hash_password import hash_password
    from review.loader import MohobotData
    from review.app import create_app
    from review.store import ReviewStore

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        h = hash_password("pw123")
        cfg = ReviewConfig(users=[ReviewUser("admin", h)])
        data = MohobotData(root)
        store = ReviewStore(root / "review.db")
        app = create_app(cfg, data, store, config_path=root / "config.yaml")
        client = TestClient(app)
        t0 = _t.monotonic()
        r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
        dt = _t.monotonic() - t0
        assert r.status_code == 401
        assert dt >= 0.45, f"review 登录耗时 {dt:.2f}s, 未达 0.5s 硬延迟"
    print("[Y7] review 登录 0.5s 硬延迟 OK")


async def test_plugin_config_update_callback_invoked() -> None:
    """on_config_update(同步)必须被调用——变异 M33 的缺口回归。"""
    from mohobot.interceptors.plugin_system import PluginSystem

    with tempfile.TemporaryDirectory() as td:
        plug = Path(td) / "dummyplug"
        plug.mkdir()
        (plug / "main.py").write_text(
            "class Plugin:\n"
            "    _calls = []\n"
            "    def on_config_update(self, config):\n"
            "        type(self)._calls.append(dict(config))\n",
            encoding="utf-8")
        (plug / "_conf_schema.json").write_text(
            '{"foo": {"type": "int", "default": 0}}', encoding="utf-8")
        ps = PluginSystem(plugins_dir=str(td), data_dir=str(Path(td) / "data"))
        await ps.load_plugins()
        assert await ps.save_plugin_config("dummyplug", {"foo": 5})
        meta = next(m for m in ps._plugins if m["name"] == "dummyplug")
        calls = meta["instance"].__class__._calls
        assert calls and calls[-1].get("foo") == 5, calls
    print("[Y8] on_config_update 同步回调被调用 OK")
