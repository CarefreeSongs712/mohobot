"""Smoke test for the agent subsystem (beta branch) — fake LLM, no real API calls.

Verifies: config load → DatabaseManager init → runtime assembly → 
event → topic extraction → plan → reply realization → send callback → 
reflection → DB persistence.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import GlobalConfig

# ── Fake LLM module (monkeypatched into runtime) ──────────────

FAKE_RESPONSES = {
    "topic_extractor": json.dumps({
        "source_message_ids": [0],
        "topic_content": "用户和机器人打招呼",
        "topic_type": "chat",
        "fact_constraints": [],
        "memory_attempts": [],
        "sing_attempts": [],
    }, ensure_ascii=False),
    "main_chat": "[中性]你好呀！很高兴见到你～\n[欣喜]今天过得怎么样？",
    "memory_writer": json.dumps({"user_memory": [], "event_memory": []}, ensure_ascii=False),
    "user_profile_updater": "no_update",
}


class FakeLLMModule:
    def __init__(self, module_name: str, config=None, **kwargs):
        self.module_name = module_name
        self._cfg = config or {}

    def is_available(self) -> bool:
        return True

    async def generate_response(self, **kwargs) -> str:
        return FAKE_RESPONSES.get(self.module_name, "")


# ── Test ──────────────────────────────────────────────────────

async def main() -> None:
    # 1. Config load
    config_path = Path(__file__).resolve().parent.parent / "config" / "global.yaml"
    cfg = GlobalConfig.load(config_path)
    assert cfg.database.enabled and cfg.agent.enabled
    cfg_dict = cfg.to_dict()
    assert "agent" in cfg_dict and "llm" in cfg_dict
    print("[1] config OK")

    # 2. DB (temp folder to avoid touching real data)
    tmp = tempfile.mkdtemp(prefix="mohobot_test_")
    import mohobot.agent.runtime as runtime_mod
    from mohobot.db.database_manager import DatabaseManager
    runtime_mod.LLMModule = FakeLLMModule  # monkeypatch

    dbm = DatabaseManager(db_folder=tmp, db_file="test.db")
    print("[2] database OK")

    # 3. Runtime assembly
    manager = runtime_mod.BotAgentManager(cfg_dict, dbm)
    sent: list[tuple] = []

    async def reply_handler(bot_id, chat_type, chat_id, reply_items, trigger_message_id=""):
        sent.append((bot_id, chat_type, chat_id, [i.get_content() for i in reply_items], trigger_message_id))

    runtime = manager.get_or_create("123456", bot_nickname="测试Bot", persona="你是测试机器人")
    runtime.set_reply_handler(reply_handler)
    print("[3] runtime OK, llm modules:", list(runtime.llm_modules.keys()))

    # 4. Feed a private message
    from mohobot.agent.domain import ChatInputEvent, ChatInputEventType
    event = ChatInputEvent(
        event_type=ChatInputEventType.USER_MESSAGE,
        user_id="10001",
        character_id="123456",
        content="你好呀！",
        message_id="m1",
        message_type="text",
        timestamp=asyncio.get_event_loop().time(),
        payload={"speaker": "10001-小明", "chat_type": "private", "chat_id": "10001"},
    )
    await runtime.handle_event("private", "10001", event)
    print("[4] event fed, waiting for pipeline...")

    # 5. Wait for pipeline to produce a reply
    for _ in range(60):
        if sent:
            break
        await asyncio.sleep(0.1)
    assert sent, "No reply was produced by the pipeline!"
    bot_id, chat_type, chat_id, texts, trigger = sent[0]
    print(f"[5] reply OK: {chat_type}/{chat_id} trigger={trigger} texts={texts}")
    assert bot_id == "123456" and chat_type == "private" and chat_id == "10001"
    assert trigger == "m1"

    # 6. Wait for reflection to settle, then check DB
    await asyncio.sleep(1.5)
    convs = dbm.get_recent_conversations("10001", "123456", limit=10)
    print("[6] DB conversations:")
    for c in convs:
        print(f"    {c['source']}: {c['content'][:40]}")
    assert any(c["source"] == "user" for c in convs), "user message not in DB"
    assert any(c["source"] == "agent" for c in convs), "agent reply not in DB"

    # 7. Shutdown
    await manager.stop_all()
    print("[7] shutdown OK")
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
