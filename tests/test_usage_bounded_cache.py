"""#137: 用量明细常驻内存有界 + 长范围聚合流式回读。

回归点:
1. llm_usage.jsonl 追加后, 内存只保留最近窗口(不再全历史常驻);
2. 窗口内聚合(1h/今日)直接复用常驻缓存;
3. 超出窗口范围(30d)时流式回读文件, 聚合结果仍完整;
4. 追加新记录只增量解析, 常驻条目数保持上限。
"""

import json
import time
from pathlib import Path

from mohobot import llm_service as llm_mod
from mohobot.llm_service import LLMService
from mohobot.models.config import GlobalConfig


def _write_records(tmp: str, records: list[dict]) -> None:
    path = Path(tmp) / "stats" / "llm_usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _append_records(tmp: str, records: list[dict]) -> None:
    path = Path(tmp) / "stats" / "llm_usage.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _rec(ts: float, total: int, **overrides) -> dict:
    rec = {
        "time": ts,
        "bot_id": overrides.pop("bot_id", "bot-1"),
        "module": overrides.pop("module", "chat"),
        "kind": overrides.pop("kind", "chat"),
        "model": overrides.pop("model", "m-1"),
        "chat_type": "private",
        "chat_id": "u-1",
        "user_id": "u-1",
        "prompt_tokens": total,
        "completion_tokens": 0,
        "total_tokens": total,
        "cached_tokens": 0,
    }
    rec.update(overrides)
    return rec


async def test_usage_cache_is_bounded_and_long_range_streams(tmp_path):
    """常驻明细不超过"最近窗口 + 乱序缓冲"; 更早范围流式回读后聚合仍完整。"""
    # tests/_run_all.py 只支持 tmp_path, 不能用 monkeypatch fixture; 手工还原。
    original_window = llm_mod._USAGE_RECORD_WINDOW_SECONDS
    llm_mod._USAGE_RECORD_WINDOW_SECONDS = 86400
    tmp = str(tmp_path)
    now = time.time()
    old = [_rec(now - 10 * 86400 + i, 1) for i in range(2500)]
    recent = [_rec(now - i, 10) for i in range(5)]
    _write_records(tmp, old + recent)

    svc = LLMService(global_config=GlobalConfig(data_dir=tmp))
    try:
        records = await svc._load_usage_records()
        # 只保留最近窗口 + 2000 条兜住乱序写入, 不再全历史常驻
        assert len(records) == 2005, len(records)
        assert len(records) < len(old) + len(recent)

        # 窗口内聚合直接命中常驻缓存
        hour = await svc.get_session_usage_stats("1h")
        assert hour["totals"]["calls"] == 5
        assert hour["totals"]["total_tokens"] == 50

        # 超出常驻窗口: 流式回读文件, 早期记录仍计入
        month = await svc.get_session_usage_stats("30d")
        assert month["totals"]["calls"] == 2505
        assert month["totals"]["total_tokens"] == 2500 * 1 + 5 * 10

        # 追加新记录: 增量解析 + 再次裁剪, 条目数仍有界
        _append_records(tmp, [_rec(now + 1, 7) for _ in range(100)])
        records2 = await svc._load_usage_records()
        assert len(records2) <= 2105, len(records2)
        hour2 = await svc.get_session_usage_stats("1h")
        assert hour2["totals"]["calls"] == 105
    finally:
        llm_mod._USAGE_RECORD_WINDOW_SECONDS = original_window
        await svc.close()


async def test_long_range_aggregation_respects_excluded_models(tmp_path):
    """流式回读路径同样应用 usage_excluded_models 排除。"""
    original_window = llm_mod._USAGE_RECORD_WINDOW_SECONDS
    llm_mod._USAGE_RECORD_WINDOW_SECONDS = 3600
    tmp = str(tmp_path)
    now = time.time()
    _write_records(tmp, [
        _rec(now - 5 * 86400, 1000, model="excluded-model"),
        _rec(now - 5 * 86400 + 1, 100, model="kept-model"),
        _rec(now, 10, model="kept-model"),
    ])
    cfg = GlobalConfig(data_dir=tmp, usage_excluded_models=["excluded-model"])
    svc = LLMService(global_config=cfg)
    try:
        # 常驻缓存仅最近 1h, 5 天前记录必须走流式回读
        records = await svc._load_usage_records()
        assert all(r.get("model") != "excluded-model" for r in records)
        month = await svc.get_module_usage_stats("30d")
        assert month["totals"]["calls"] == 2, month["totals"]
        assert month["totals"]["total_tokens"] == 110, month["totals"]
    finally:
        llm_mod._USAGE_RECORD_WINDOW_SECONDS = original_window
        await svc.close()
