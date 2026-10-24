"""Review worker safety regressions: request timeouts and failed-submit accounting."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


_MODULE = None
_CONFIG_PATH = None


def worker_module():
    global _MODULE, _CONFIG_PATH
    if _MODULE is not None:
        return _MODULE
    cfg = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({
        "server": "http://127.0.0.1:1",
        "user": "test",
        "llm_base": "http://127.0.0.1:1/v1",
        "llm_key": "test",
        "llm_model": "test",
    }, cfg)
    cfg.close()
    _CONFIG_PATH = cfg.name
    os.environ["REVIEW_CFG"] = _CONFIG_PATH
    path = Path(__file__).resolve().parents[1] / "review" / "worker" / "review_bot.py"
    spec = importlib.util.spec_from_file_location("review_worker_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODULE = module
    return module


def test_curl_bounds_subprocess_timeout():
    worker = worker_module()
    original = worker.subprocess.run
    seen = {}

    def timeout(*args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    worker.subprocess.run = timeout
    try:
        status, payload = worker.curl("GET", "http://example.invalid")
    finally:
        worker.subprocess.run = original
    assert seen["timeout"] == 125
    assert status == 0
    assert payload == {"detail": "curl request timed out"}


def test_failed_submit_does_not_count_as_progress():
    worker = worker_module()
    worker.STATS.clear()
    original_submit = worker.submit
    original_log = worker.log_rec
    worker.submit = lambda *args, **kwargs: False
    worker.log_rec = lambda *args, **kwargs: None
    try:
        result = worker.handle({
            "s": {"display_name": "test", "page": 1, "pages": 1},
            "sk": "bot/private/1",
            "user_fps": ["fp-1"],
            "bot_fps": [],
            "err": "",
            "verdict": {"per": None, "all": {
                "action": "normal", "tags": [], "note": "ok", "conf": 1.0,
            }},
            "text": "结论: 正常 | ok | 1.0",
        })
    finally:
        worker.submit = original_submit
        worker.log_rec = original_log
    assert result == 0
    assert worker.STATS["normal"] == 0


def test_failed_bot_only_submit_does_not_count_as_progress():
    worker = worker_module()
    worker.STATS.clear()
    original_submit = worker.submit
    original_log = worker.log_rec
    worker.submit = lambda *args, **kwargs: False
    worker.log_rec = lambda *args, **kwargs: None
    try:
        result = worker.handle({
            "s": {"display_name": "test", "page": 1, "pages": 1},
            "sk": "bot/group/1",
            "user_fps": [],
            "bot_fps": ["bot-fp-1"],
            "err": "",
            "verdict": None,
            "text": "",
        })
    finally:
        worker.submit = original_submit
        worker.log_rec = original_log
    assert result == 0
    assert worker.STATS["bot_only"] == 0


_HOUR = 3600


def _session(ages_h, now):
    """构造一页会话: 每个 age 一条待审用户消息(小时前), 中间夹一条 bot 回复。"""
    entries = []
    for i, age in enumerate(ages_h):
        entries.append({"kind": "user", "status": "unreviewed", "fingerprint": f"fp-{i + 1}",
                        "speaker": "u", "content": f"消息{i + 1}", "time_str": "",
                        "timestamp": now - age * _HOUR})
        entries.append({"kind": "assistant", "status": "unreviewed", "fingerprint": f"bot-{i + 1}",
                        "content": "回复", "time_str": "", "timestamp": now - age * _HOUR})
    return {"display_name": "t", "chat_type": "private", "page": 1, "pages": 1, "entries": entries}


def _with_llm(worker, replies):
    """把 _post_llm 换成依次返回 replies 的假实现, 记录每次请求。"""
    calls = []
    original = worker._post_llm

    def fake(req):
        calls.append(req)
        text = replies[len(calls) - 1] if len(calls) <= len(replies) else ""
        return (text, "") if text else ("", "回复里没有「结论:」行 (已尝试 1 次)")

    worker._post_llm = fake
    return calls, lambda: setattr(worker, "_post_llm", original)


def test_forced_indices_only_old_user_messages():
    worker = worker_module()
    now = 1_800_000_000
    s = _session([1, 80, 71.9, 200], now)
    s["entries"].insert(0, {"kind": "summary", "content": "总结"})
    s["entries"].append({"kind": "user", "status": "normal", "fingerprint": "old-done",
                         "timestamp": now - 500 * _HOUR})
    assert worker.forced_indices(s, now) == {2, 4}
    req = worker.build_llm_req(s, ["色情"], worker.forced_indices(s, now))
    body = req["messages"][1]["content"]
    assert "(待审#2, 必须判决)" in body and "(待审#4, 必须判决)" in body
    assert "(待审#1)" in body and "(待审#3)" in body


def test_old_message_doubt_is_followed_up_and_resolved():
    """72h 前的消息回「存疑」→ 追问一次, 追问给出结论即采用; 新消息存疑不受影响。"""
    worker = worker_module()
    now = time.time()
    s = _session([1, 100], now)
    calls, restore = _with_llm(worker, [
        "#1 结论: 存疑 | 半句话 | 置信度: 0.5\n#2 结论: 存疑 | 看不懂 | 置信度: 0.4\n总结: x",
        "#2 结论: 异常 | 辱骂 | 骂人 | 置信度: 0.6",
    ])
    try:
        text, v, err = worker.ask_llm(s, ["辱骂", "色情"])
    finally:
        restore()
    assert err == "" and len(calls) == 2
    follow = calls[1]["messages"]
    assert follow[-2]["role"] == "assistant" and "#2" in follow[-1]["content"] and "#1" not in follow[-1]["content"]
    per = v["per"]
    assert per[1]["action"] == "skip", "72h 内的存疑仍转人工"
    assert per[2]["action"] == "abnormal" and per[2]["tags"] == ["辱骂"], "低置信度不再拦截积压条目"
    assert per[2]["forced"] and per[2]["note"].startswith("[追问]")
    assert "积压条目追问" in text


def test_old_message_still_undecided_falls_back_to_normal():
    """追问后仍存疑/无结论/异常无合法标签 → 兜底判正常, 不会留在未审。"""
    worker = worker_module()
    now = time.time()
    s = _session([100, 90, 80], now)
    calls, restore = _with_llm(worker, [
        "#1 结论: 存疑 | 不确定 | 置信度: 0.3\n#2 结论: 异常 | 乱写标签 | 理由 | 置信度: 0.9\n总结: x",
        "#1 结论: 存疑 | 还是不确定 | 置信度: 0.3\n#2 结论: 异常 | 还是乱写 | 理由 | 置信度: 0.9",
    ])
    try:
        _, v, err = worker.ask_llm(s, ["辱骂"])
    finally:
        restore()
    assert err == "" and len(calls) == 2
    per = v["per"]
    for i in (1, 2, 3):  # #3 首轮压根没给结论
        assert per[i]["action"] == "normal", (i, per[i])
        assert per[i]["fallback"] and per[i]["forced"] and per[i]["note"].startswith("[兜底]")


def test_old_messages_low_confidence_submitted_without_followup():
    """积压条目给了正常/异常, 即使置信度低于 min_conf 也直接采用, 不追问。"""
    worker = worker_module()
    now = time.time()
    s = _session([100, 1], now)
    calls, restore = _with_llm(worker, [
        "#1 结论: 正常 | 闲聊 | 置信度: 0.4\n#2 结论: 正常 | 闲聊 | 置信度: 0.4\n总结: x",
    ])
    try:
        _, v, _ = worker.ask_llm(s, ["辱骂"])
    finally:
        restore()
    assert len(calls) == 1
    assert v["per"][1]["action"] == "normal" and v["per"][1]["forced"]
    assert v["per"][2]["action"] == "skip", "72h 内仍受 min_conf 约束"


def test_handle_submits_forced_fallback_as_normal():
    """handle: 兜底判正常的条目计入正常并提交, 统计积压/兜底数量, 会话继续推进。"""
    worker = worker_module()
    worker.STATS.clear()
    sent = []
    original_submit, original_log = worker.submit, worker.log_rec
    worker.submit = lambda sk, action, fps, **kw: sent.append((action, list(fps))) or True
    worker.log_rec = lambda *a, **k: None
    try:
        nxt = worker.handle({
            "s": {"display_name": "t", "page": 1, "pages": 2},
            "sk": "bot/private/1", "user_fps": ["fp-1", "fp-2"], "bot_fps": ["bot-1"], "err": "",
            "verdict": {"per": {
                1: {"action": "normal", "tags": [], "conf": None, "label": "", "note": "[兜底] x",
                    "fallback": True, "forced": True},
                2: {"action": "skip", "tags": [], "conf": 0.5, "label": "", "note": "新消息存疑"},
            }, "all": None},
            "text": "",
        })
    finally:
        worker.submit, worker.log_rec = original_submit, original_log
    assert ("normal", ["fp-1"]) in sent and ("normal", ["bot-1"]) in sent
    assert nxt == 2
    assert worker.STATS["forced"] == 1 and worker.STATS["fallback"] == 1 and worker.STATS["skip"] == 1
