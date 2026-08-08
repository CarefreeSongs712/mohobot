"""MessageHandler agent-path test — simulates a OneBot private message end-to-end.

Verifies: group/private gating intact, agent pipeline reply with quote +
segmented sending, context (JSONL) unchanged management, DB persistence.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import GlobalConfig, ReplyConfig


class FakeLLMModule:
    def __init__(self, module_name: str, config=None, **kwargs):
        self.module_name = module_name
        self._cfg = config or {}

    def is_available(self) -> bool:
        return True

    async def generate_response(self, **kwargs) -> str:
        if self.module_name == "topic_extractor":
            return json.dumps({
                "source_message_ids": [0],
                "topic_content": "用户和机器人打招呼",
                "topic_type": "chat",
                "fact_constraints": [],
                "memory_attempts": [],
                "sing_attempts": [],
            }, ensure_ascii=False)
        if self.module_name == "main_chat":
            return "[中性]你好呀！很高兴见到你～\n[欣喜]今天过得怎么样？"
        if self.module_name == "memory_writer":
            return json.dumps({"user_memory": [], "event_memory": []})
        return "no_update"


class FakeWS:
    """模拟 WSServer 的发送接口 + bot_manager。"""

    def __init__(self, bot_manager):
        self._bot_manager = bot_manager
        self.sent: list[tuple] = []

    async def send_private_msg(self, bot_id, user_id, message):
        self.sent.append(("private", bot_id, user_id, message))

    async def send_group_msg(self, bot_id, group_id, message):
        self.sent.append(("group", bot_id, group_id, message))


class FakePlugins:
    async def dispatch_notice(self, *a, **kw): pass
    async def dispatch_meta(self, *a, **kw): pass


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="mohobot_handler_")

    import mohobot.agent.runtime as runtime_mod
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.context_manager import ContextManager
    from mohobot.db.database_manager import DatabaseManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import BotConfig
    runtime_mod.LLMModule = FakeLLMModule  # monkeypatch

    cfg = GlobalConfig.load(Path(__file__).resolve().parent / "test_config.yaml")
    cfg_dict = cfg.to_dict()

    dbm = DatabaseManager(db_folder=tmp, db_file="handler.db")
    ctx_mgr = ContextManager(data_dir=tmp)
    agent_manager = runtime_mod.BotAgentManager(cfg_dict, dbm)

    bot_manager = BotManager(data_dir=tmp)
    bot_manager._bots["123456"] = BotInstance(
        "123456", None, BotConfig(qq=123456, nickname="测试Bot", persona="你是测试机器人"),
    )
    fake_ws = FakeWS(bot_manager)

    handler = MessageHandler(
        ws_server=fake_ws,
        context_manager=ctx_mgr,
        llm_service=None,
        plugin_system=FakePlugins(),
        data_dir=tmp,
        context_max_rounds=30,
        reply_config=ReplyConfig(reply_quote=True, segment_reply=True),
        agent_manager=agent_manager,
        database_manager=dbm,
    )
    handler.set_interceptors([])
    print("[1] handler wired")

    # ── 私聊消息 ──
    raw = {
        "time": 1754030000, "self_id": 123456, "post_type": "message",
        "message_type": "private", "sub_type": "friend",
        "message_id": 1002, "user_id": 10001,
        "message": [{"type": "text", "data": {"text": "你好呀！"}}],
        "raw_message": "你好呀！",
        "sender": {"user_id": 10001, "nickname": "小明"},
    }
    from mohobot.models.onebot import Event
    event = Event.from_dict(raw)
    await handler.handle_event("123456", event, raw)
    print("[2] private message handled, waiting for pipeline...")

    for _ in range(80):
        if len(fake_ws.sent) >= 2:
            break
        await asyncio.sleep(0.1)
    assert len(fake_ws.sent) >= 2, f"Expected 2 reply segments, got {len(fake_ws.sent)}"
    first = fake_ws.sent[0]
    print(f"[3] sent {len(fake_ws.sent)} message(s): {first[3] if len(first) > 3 else first}")
    assert first[0] == "private" and first[1] == "123456"
    assert str(first[2]) == "10001", f"chat_id mismatch: {first[2]!r}"
    first_msg = first[3]
    assert isinstance(first_msg, list), "first segment should quote the trigger"
    assert first_msg[0] == {"type": "reply", "data": {"id": "1002"}}, f"quote mismatch: {first_msg}"
    # 后续段: 无引用 + 随机延迟(延迟不影响断言)
    for _, bot_id, uid, msg in fake_ws.sent[1:]:
        assert isinstance(msg, str) and msg.strip(), f"non-first segment should be plain text: {msg}"

    # ── 上下文(JSONL)仍由 ContextManager 管理 ──
    await asyncio.sleep(0.3)
    ctx = await ctx_mgr.load_context("123456", "private", "10001")
    roles = [c.get("role") for c in ctx]
    print(f"[4] context roles: {roles}")
    assert "10001-小明" in roles and "assistant" in roles

    # ── DB 持久化 ──
    convs = dbm.get_recent_conversations("10001", "123456", limit=10)
    sources = [c["source"] for c in convs]
    print(f"[5] DB sources: {sources}")
    assert "user" in sources and "agent" in sources

    # ── 群消息未 @ 不触发 ──
    raw_group = {
        "time": 1754030001, "self_id": 123456, "post_type": "message",
        "message_type": "group", "sub_type": "normal",
        "message_id": 2001, "user_id": 10001, "group_id": 55555,
        "message": [{"type": "text", "data": {"text": "有人吗"}}],
        "raw_message": "有人吗",
        "sender": {"user_id": 10001, "nickname": "小明", "card": ""},
    }
    sent_before = len(fake_ws.sent)
    await handler.handle_event("123456", Event.from_dict(raw_group), raw_group)
    await asyncio.sleep(1.0)
    assert len(fake_ws.sent) == sent_before, "group message without @ must NOT trigger a reply"
    print("[6] group gate OK (no reply without mention)")

    # ── 群消息 @ bot 触发 ──
    raw_mention = dict(raw_group)
    raw_mention["message"] = [
        {"type": "at", "data": {"qq": "123456"}},
        {"type": "text", "data": {"text": "在吗？"}},
    ]
    raw_mention["raw_message"] = "[CQ:at,qq=123456]在吗？"
    raw_mention["message_id"] = 2002
    await handler.handle_event("123456", Event.from_dict(raw_mention), raw_mention)
    for _ in range(80):
        if len(fake_ws.sent) > sent_before:
            break
        await asyncio.sleep(0.1)
    assert len(fake_ws.sent) > sent_before, "@mention should trigger a reply"
    print(f"[7] group @mention OK, sent={len(fake_ws.sent) - sent_before} message(s)")

    await agent_manager.stop_all()
    print("\nALL HANDLER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
