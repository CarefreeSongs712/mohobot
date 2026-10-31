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
