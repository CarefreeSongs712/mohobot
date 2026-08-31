"""用量统计三种聚合模式 + 情感分析间隔测试:

1. 情感分析强制更新间隔: 默认 force_update_interval=10, 距上次强制更新超 60 分钟才触发
2. 按会话聚合(模式一)保留; 未知会话/空 bot 归位
3. 按用户聚合(模式二): 群/私聊跨 bot 合并, 无 user_id 归未知用户
4. 按用途聚合(模式三): bot × module, 空 bot 归"系统"行
5. user_id 写入 llm_usage.jsonl
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.emotion.models import EmotionalState
from mohobot.models.config import EmotionConfig, GlobalConfig


def _mk_records() -> list[dict]:
    now = time.time()
    return [
        # 用户 111 在 bot_001 群聊的主对话(2 次) + 私聊识图(1 次)
        {"time": now, "bot_id": "bot_001", "module": "chat", "kind": "chat",
         "chat_type": "group", "chat_id": "888", "user_id": "111",
         "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cached_tokens": 10},
        {"time": now, "bot_id": "bot_001", "module": "chat", "kind": "chat",
         "chat_type": "group", "chat_id": "888", "user_id": "111",
         "prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280, "cached_tokens": 20},
        {"time": now, "bot_id": "bot_001", "module": "vision", "kind": "vision",
         "chat_type": "private", "chat_id": "111", "user_id": "111",
         "prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80, "cached_tokens": 0},
        # 用户 222 在 bot_002 私聊主对话
        {"time": now, "bot_id": "bot_002", "module": "chat", "kind": "chat",
         "chat_type": "private", "chat_id": "222", "user_id": "222",
         "prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80, "cached_tokens": 0},
        # 系统内部: 无 bot 无 user 的 summary(上下文总结) + emotion(情感分析)
        {"time": now, "bot_id": "", "module": "summarize", "kind": "summary",
         "chat_type": "", "chat_id": "", "user_id": "",
         "prompt_tokens": 400, "completion_tokens": 100, "total_tokens": 500, "cached_tokens": 0},
        {"time": now, "bot_id": "", "module": "emotion", "kind": "emotion",
         "chat_type": "", "chat_id": "", "user_id": "",
         "prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40, "cached_tokens": 0},
        # 旧记录: 无 user_id 的 chat
        {"time": now, "bot_id": "bot_003", "module": "chat", "kind": "chat",
         "chat_type": "", "chat_id": "", "user_id": "",
         "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cached_tokens": 0},
    ]


def _write_records(tmp: str, records: list[dict]) -> None:
    import json
    from pathlib import Path as _P
    p = _P(tmp) / "stats" / "llm_usage.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


async def test_usage_session_mode():
    tmp = tempfile.mkdtemp()
    _write_records(tmp, _mk_records())
    cfg = GlobalConfig(data_dir=tmp)
    from mohobot.llm_service import LLMService
    svc = LLMService(global_config=cfg)
    result = await svc.get_session_usage_stats("30d")
    sessions = result["sessions"]
    by_key = {(s["bot_id"], s["chat_type"], s["chat_id"]): s for s in sessions}
    # 群聊 bot_001: 两轮对话合并 430
    s = by_key[("bot_001", "group", "888")]
    assert s["total_tokens"] == 430 and s["calls"] == 2
    # 系统内部(空 bot/空会话): summary 500 + emotion 40 合并
    s = by_key[("?", "", "")]
    assert s["total_tokens"] == 540 and s["calls"] == 2
    mods = s["modules"]
    assert mods["summarize"]["total_tokens"] == 500
    assert mods["emotion"]["total_tokens"] == 40
    await svc.close()


async def test_usage_user_mode():
    tmp = tempfile.mkdtemp()
    _write_records(tmp, _mk_records())
    from mohobot.llm_service import LLMService
    svc = LLMService(global_config=GlobalConfig(data_dir=tmp))
    result = await svc.get_user_usage_stats("30d")
    users = {u["user_id"]: u for u in result["users"]}
    # 用户 111: 群聊 430 + 私聊识图 80 = 510, 跨 bot 只有 bot_001
    u = users["111"]
    assert u["total_tokens"] == 510 and u["calls"] == 3
    assert u["bots"]["bot_001"]["total_tokens"] == 510
    assert u["bots"]["bot_001"]["modules"]["vision"]["calls"] == 1
    # 用户 222
    assert users["222"]["total_tokens"] == 80
    # 未知用户(系统内部 + 旧记录): summary 500 + emotion 40 + 旧 chat 15 = 555
    u_unknown = users[""]
    assert u_unknown["total_tokens"] == 555 and u_unknown["calls"] == 3
    # 未知用户排最后
    assert result["users"][-1]["user_id"] == ""
    await svc.close()


async def test_usage_module_mode():
    tmp = tempfile.mkdtemp()
    _write_records(tmp, _mk_records())
    from mohobot.llm_service import LLMService
    svc = LLMService(global_config=GlobalConfig(data_dir=tmp))
    result = await svc.get_module_usage_stats("30d")
    bots = {b["bot_id"]: b for b in result["bots"]}
    b1 = bots["bot_001"]
    assert b1["modules"]["chat"]["total_tokens"] == 430 and b1["modules"]["chat"]["calls"] == 2
    assert b1["modules"]["vision"]["total_tokens"] == 80
    assert b1["total_tokens"] == 510
    sys_row = bots["系统"]
    assert sys_row["modules"]["summarize"]["total_tokens"] == 500
    assert sys_row["modules"]["emotion"]["total_tokens"] == 40
    assert sys_row["total_tokens"] == 540
    # 系统行排最后
    assert result["bots"][-1]["bot_id"] == "系统"
    await svc.close()


async def test_usage_user_id_recorded():
    """新记录写盘时带 user_id 字段。"""
    tmp = tempfile.mkdtemp()
    from mohobot.services.usage import UsageRecorder

    class _U:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15
        prompt_tokens_details = None
        prompt_cache_hit_tokens = None

    rec = UsageRecorder(tmp)
    await rec.record(_U(), model="m", bot_id="bot_001", module="chat",
                     chat_type="group", chat_id="888", user_id="111")
    await rec.close()
    import json
    with open(Path(tmp) / "stats" / "llm_usage.jsonl", encoding="utf-8") as f:
        data = json.loads(f.readline())
    assert data["user_id"] == "111"
    assert data["bot_id"] == "bot_001"


# ── 情感分析间隔 ─────────────────────────────────────────────

def test_emotion_force_update_defaults():
    assert EmotionConfig().force_update_interval == 10
    assert EmotionConfig().enabled is False


def test_emotion_force_update_interval_60min():
    state = EmotionalState(user_key="u1")
    state.reset_force_update_counter()
    # 距上次强制更新 30 分钟 → 不触发
    import time as _t
    state.last_force_update = _t.time() - 30 * 60
    assert state.should_force_update(10) is False
    # 距上次强制更新 61 分钟 → 触发
    state.last_force_update = _t.time() - 61 * 60
    assert state.should_force_update(10) is True
    # 计数器满 10 轮 → 触发(无论时间)
    state.last_force_update = _t.time()
    state.force_update_counter = 10
    assert state.should_force_update(10) is True
    state.force_update_counter = 5
    assert state.should_force_update(10) is False
