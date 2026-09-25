"""情感管理 WebUI 回归测试:
1. EmotionExpert.status() 熔断快照(正常/熔断态)
2. MemorySystem.user_records() 只读读取
3. EmotionManager.runtime_status() / user_memory()
4. WebUI 端点: 登录鉴权 + /api/emotion/{status,states,operate,memory}
   (set_favor / set_intimacy / set_attitude / reset_user / clear_bot)
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from mohobot.emotion.expert import EmotionExpert
from mohobot.emotion.manager import EmotionManager
from mohobot.emotion.memory import MemorySystem
from mohobot.models.config import EmotionConfig


FAKE_ANALYSIS = json.dumps({
    "emotion_updates": {"favor": 3, "intimacy": 2, "joy": 2},
    "relationship": "初识朋友",
    "attitude": "热情友好",
}, ensure_ascii=False)


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def analyze_emotion(self, prompt, model=None):
        self.calls += 1
        return FAKE_ANALYSIS


def make_manager(tmp: str) -> EmotionManager:
    cfg = EmotionConfig(enabled=True, smart_update=False)
    return EmotionManager(data_dir=tmp, config=cfg, llm_service=FakeLLM())


def make_client(tmp: str, manager: EmotionManager) -> TestClient:
    from mohobot.web_panel.app import WebPanel
    panel = WebPanel(
        host="127.0.0.1", port=0, username="admin",
        password_hash=WebPanel._hash_password("test-pass"),
        data_dir=tmp, config_path=str(Path(tmp) / "none.yaml"),
        emotion_manager=manager,
    )
    client = TestClient(panel._app)
    r = client.post("/api/login", json={"username": "admin", "password": "test-pass"})
    assert r.status_code == 200, r.text
    client.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return client


async def test_expert_status() -> None:
    async def ok_call(prompt):
        return FAKE_ANALYSIS

    expert = EmotionExpert(llm_call=ok_call)
    st = expert.status()
    assert st["llm_available"] is True
    assert st["tripped"] is False
    assert st["consecutive_failures"] == 0

    # 连续失败 3 次 → 熔断, 冷却剩余 > 0
    expert._record_failure()
    expert._record_failure()
    expert._record_failure()
    st = expert.status()
    assert st["tripped"] is True
    assert st["llm_available"] is False
    assert st["consecutive_failures"] == 3
    assert 0 < st["cooldown_remaining_sec"] <= 300
    print("[1] EmotionExpert.status OK")


def test_memory_user_records() -> None:
    mem = MemorySystem()
    assert mem.user_records("bot_001", "123") == []
    mem.add_interaction("bot_001", "123", "你好", "你好呀", 5, {"favor": 3}, threshold=5)
    mem.add_interaction("bot_001", "123", "再见", "拜拜", 6, {"favor": -1}, threshold=5)
    recs = mem.user_records("bot_001", "123")
    assert len(recs) == 2
    # 新→旧
    assert recs[0]["user_msg"] == "再见"
    assert recs[1]["user_msg"] == "你好"
    # 未写入的(低于阈值)不出现
    mem.add_interaction("bot_001", "456", "小声", "嗯", 1, {}, threshold=5)
    assert mem.user_records("bot_001", "456") == []
    print("[2] MemorySystem.user_records OK")


async def test_manager_status_and_memory() -> None:
    tmp = tempfile.mkdtemp(prefix="emo_mgr_")
    mgr = make_manager(tmp)
    st = mgr.runtime_status()
    assert st["enabled"] is True
    assert st["queue_pending"] == 0 and st["burst_left"] == 0
    assert st["active_model"] is None
    assert "expert" in st and "store" in st and "memory" in st

    await mgr.process_turn("bot_001", "123", "今天真开心", "是呀, 很高兴见到你")
    state = await mgr.get_state("bot_001", "123")
    assert state.favor == 3 and state.intimacy == 2
    assert state.stats.total_count == 1
    # favor3+intimacy2+joy2=7 ≥ 5 → 记忆写入
    mem = mgr.user_memory("bot_001", "123")
    assert len(mem) == 1 and mem[0]["user_msg"] == "今天真开心"
    await mgr.flush()
    print("[3] EmotionManager.runtime_status / user_memory OK")


async def test_webui_endpoints() -> None:
    tmp = tempfile.mkdtemp(prefix="emo_webui_")
    mgr = make_manager(tmp)

    # 先造一轮数据
    await mgr.process_turn("bot_001", "123", "今天真开心", "是呀, 很高兴见到你")

    client = make_client(tmp, mgr)

    # 未登录 → 401
    r = TestClient(_panel(tmp, mgr)).get("/api/emotion/status")
    assert r.status_code == 401, r.text

    # status
    r = client.get("/api/emotion/status")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["enabled"] is True
    assert s["queue_pending"] == 0
    assert s["expert"]["llm_available"] is True
    assert s["store"]["users_total"] == 1

    # states
    r = client.get("/api/emotion/states?bot_id=bot_001")
    assert r.status_code == 200, r.text
    states = r.json()["states"]
    assert len(states) == 1
    assert states[0]["user_key"] == "123"
    assert states[0]["favor"] == 3

    # set_favor
    r = client.post("/api/emotion/operate", json={"data": {
        "action": "set_favor", "bot_id": "bot_001", "user_id": "123", "value": 50,
    }})
    assert r.status_code == 200, r.text
    assert r.json()["state"]["favor"] == 50

    # set_intimacy
    r = client.post("/api/emotion/operate", json={"data": {
        "action": "set_intimacy", "bot_id": "bot_001", "user_id": "123", "value": 88,
    }})
    assert r.status_code == 200, r.text
    assert r.json()["state"]["intimacy"] == 88

    # set_attitude
    r = client.post("/api/emotion/operate", json={"data": {
        "action": "set_attitude", "bot_id": "bot_001", "user_id": "123", "text": "热情友好",
    }})
    assert r.status_code == 200, r.text
    assert r.json()["state"]["descriptions"]["attitude"] == "热情友好"

    # 非法态度(emoji 不在放行字符集, 截断也救不了) → 400
    r = client.post("/api/emotion/operate", json={"data": {
        "action": "set_attitude", "bot_id": "bot_001", "user_id": "123", "text": "😀😀😀😀😀",
    }})
    assert r.status_code == 400, r.text

    # 非法 value → 400
    r = client.post("/api/emotion/operate", json={"data": {
        "action": "set_favor", "bot_id": "bot_001", "user_id": "123", "value": "abc",
    }})
    assert r.status_code == 400, r.text

    # 未知操作 → 400
    r = client.post("/api/emotion/operate", json={"data": {
        "action": "nope", "bot_id": "bot_001", "user_id": "123",
    }})
    assert r.status_code == 400, r.text

    # memory
    r = client.get("/api/emotion/memory?bot_id=bot_001&user_id=123")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["records"]) == 1
    assert body["records"][0]["user_msg"] == "今天真开心"
    assert body["stats"]["long_term_count"] == 1

    # reset_user
    r = client.post("/api/emotion/operate", json={"data": {
        "action": "reset_user", "bot_id": "bot_001", "user_id": "123",
    }})
    assert r.status_code == 200, r.text
    st = r.json()["state"]
    assert st["favor"] == 0 and st["intimacy"] == 0

    # clear_bot → 状态与记忆全清
    r = client.post("/api/emotion/operate", json={"data": {
        "action": "clear_bot", "bot_id": "bot_001",
    }})
    assert r.status_code == 200, r.text
    assert client.get("/api/emotion/states?bot_id=bot_001").json()["states"] == []
    body = client.get("/api/emotion/memory?bot_id=bot_001&user_id=123").json()
    assert body["records"] == []
    print("[4] WebUI /api/emotion/* endpoints OK")


def _panel(tmp: str, mgr: EmotionManager):
    from mohobot.web_panel.app import WebPanel
    return WebPanel(
        host="127.0.0.1", port=0, username="admin",
        password_hash=WebPanel._hash_password("test-pass"),
        data_dir=tmp, config_path=str(Path(tmp) / "none.yaml"),
        emotion_manager=mgr,
    )._app


async def main() -> None:
    await test_expert_status()
    test_memory_user_records()
    await test_manager_status_and_memory()
    await test_webui_endpoints()


if __name__ == "__main__":
    asyncio.run(main())
