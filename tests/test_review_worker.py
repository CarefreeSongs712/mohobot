"""Review worker safety regressions: request timeouts and failed-submit accounting."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
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
