"""用量按会话统计测试 — 记录端会话字段 + /用量 会话 聚合。"""

from __future__ import annotations

import sys
import time
import tempfile
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def test_usage_recorder_writes_chat_fields() -> None:
    from mohobot.services.usage import UsageRecorder

    class _Usage:
        prompt_tokens = 11
        completion_tokens = 7
        total_tokens = 18
        prompt_tokens_details = type("D", (), {"cached_tokens": 8})()

    class _UsageDeepSeek(_Usage):
        prompt_tokens_details = None
        prompt_cache_hit_tokens = 6

    with tempfile.TemporaryDirectory() as td:
        rec = UsageRecorder(td)
        await rec.record(
            _Usage(), model="test-model", bot_id="bot_001",
            module="chat", kind="chat",
            chat_type="group", chat_id="123456",
        )
        await rec.record(
            _UsageDeepSeek(), model="test-model", bot_id="bot_001",
            module="chat", kind="chat",
            chat_type="group", chat_id="123456",
        )
        await rec.close()
        lines = (Path(td) / "stats" / "llm_usage.jsonl").read_text(encoding="utf-8").strip().splitlines()
        d1, d2 = json.loads(lines[0]), json.loads(lines[1])
        assert d1["chat_type"] == "group" and d1["chat_id"] == "123456"
        assert d1["total_tokens"] == 18
        assert d1["cached_tokens"] == 8, d1
        assert d2["cached_tokens"] == 6, d2  # DeepSeek 风格回退


def _make_plugin(tmp_data: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "usage_stats_plugin", "plugins/usage_stats/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = mod.Plugin
    cls.inject_data_dir(tmp_data)
    cls.inject_admin_ids([10000])
    return cls


def test_session_reply_aggregates_and_ranks() -> None:
    cls = _make_plugin(tempfile.mkdtemp())
    inst = cls()
    path = Path(cls._data_dir) / "stats" / "llm_usage.jsonl"
    now = time.time()
    rows = [
        # 群 A: 两次调用共 300 token → Top1
        {"time": now, "bot_id": "bot_001", "module": "chat",
         "chat_type": "group", "chat_id": "111",
         "prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200,
         "cached_tokens": 80},
        {"time": now, "bot_id": "bot_001", "module": "summarize",
         "chat_type": "group", "chat_id": "111",
         "prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100},
        # 私聊 B: 50 token → Top2
        {"time": now, "bot_id": "bot_001", "module": "chat",
         "chat_type": "private", "chat_id": "222",
         "prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
        # 旧记录无会话字段 → 未知会话
        {"time": now, "bot_id": "bot_001", "module": "chat",
         "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        # 太老的记录应被 7d 窗口外排除不了(就在现在), 用超旧时间戳测窗口
        {"time": now - 90 * 86400, "bot_id": "bot_001", "module": "chat",
         "chat_type": "group", "chat_id": "111",
         "prompt_tokens": 999, "completion_tokens": 999, "total_tokens": 1998},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    text = inst._session_reply(["7d"])
    assert "近 7 天" in text
    assert "群 111" in text and "私聊 222" in text and "未知会话" in text
    lines = text.splitlines()
    # 排序: 群 111(300) 在 私聊 222(50) 之前
    idx_group = next(i for i, l in enumerate(lines) if "群 111" in l)
    idx_priv = next(i for i, l in enumerate(lines) if "私聊 222" in l)
    assert idx_group < idx_priv
    assert "300" in lines[idx_group]  # 200 + 100
    # 缓存占比: 群 111 缓存 80 / 输入 150 ≈ 53%
    assert "缓存" in lines[idx_group]
    assert "53%" in lines[idx_group] or "缓存 53%" in lines[idx_group]
    # module 细分: 群 111 明细行包含 上下文总结
    assert "上下文总结" in lines[idx_group + 1]
    # 90 天前的记录不计入 7d
    assert "1998" not in text

    # 今日窗口同样包含(记录都是现在)
    text_today = inst._session_reply([])
    assert "今日" in text_today and "群 111" in text_today


def test_session_reply_empty() -> None:
    cls = _make_plugin(tempfile.mkdtemp())
    inst = cls()
    text = inst._session_reply(["30d"])
    assert "没有用量记录" in text
