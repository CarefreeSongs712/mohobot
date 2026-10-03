"""Preset resolution and captured private-session reply isolation."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotInstance, BotManager
from mohobot.context_manager import ContextManager
from mohobot.llm_service import LLMService
from mohobot.message_handler import MessageHandler
from mohobot.models.config import GlobalConfig, ReplyConfig
from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender
from mohobot.persona_service import PersonaService


def event(user=3001, mid=1):
    return PrivateMessageEvent(
        time=1, self_id=1001, post_type="message", message_type="private",
        message_id=mid, user_id=user, sender=Sender(user_id=user, nickname="测试用户"),
        message=[{"type": "text", "data": {"text": "你好"}}],
    )


class FakeWS:
    def __init__(self, manager):
        self._bot_manager = manager
        self.sent = []

    async def send_private_msg(self, bot_id, user_id, message, source="auto"):
        self.sent.append((bot_id, user_id, message))


class SpyLLM:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        return "这是回复", None

    async def chat_stream(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        yield "这是回复", True


async def setup(td, stream=True, segment=True):
    manager = BotManager(data_dir=td)
    cfg = manager.create_bot(nickname="测试bot", qq=1001)
    context = ContextManager(data_dir=td, summary_enabled=False, trim_at_rounds=100)
    service = PersonaService(td, manager, context)
    await service.startup()
    cfg = manager.load_bot_config(cfg.bot_id)
    manager._bots[cfg.bot_id] = BotInstance(cfg.bot_id, None, cfg)
    spy = SpyLLM()
    handler = MessageHandler(
        FakeWS(manager), context, spy, None, data_dir=td,
        reply_config=ReplyConfig(stream=stream, segment_reply=segment,
                                 segment_delay_min=0, segment_delay_max=0),
        persona_service=service,
    )
    await context.capture_session(cfg.bot_id, "private", "3001")
    return manager, context, service, handler, spy, cfg.bot_id


async def test_all_reply_modes_use_session_persona():
    for stream, segment in [(True, True), (True, False), (False, True)]:
        with tempfile.TemporaryDirectory() as td:
            manager, context, service, handler, spy, bot = await setup(td, stream, segment)
            preset = await service.create_persona("专属", "会话专属正文")
            await service.bind_session(bot, "3001", "sess_main", preset["id"])
            await handler._handle_message(bot, event(), {})
            call = spy.calls[-1]
            assert call["persona_content"] == "会话专属正文"
            assert call["bot_config"] is not manager.get(bot).config
            saved = await context.load_context(bot, "private", "3001")
            assert len(saved) == 2
            assert "会话专属正文" not in json.dumps(saved, ensure_ascii=False)
            await handler.close()


async def test_persona_snapshot_and_session_switch_during_reply():
    with tempfile.TemporaryDirectory() as td:
        _, context, service, handler, spy, bot = await setup(td)
        preset = await service.create_persona("角色", "旧正文")
        await service.bind_session(bot, "3001", "sess_main", preset["id"])
        other = await context.create_session(bot, "private", "3001", "另一会话")
        await context.switch_session(bot, "private", "3001", "sess_main")
        spy.release.clear()
        task = asyncio.create_task(handler._handle_message(bot, event(), {}))
        await spy.started.wait()
        await service.update_persona(preset["id"], "改名", "新正文")
        await context.switch_session(bot, "private", "3001", other)
        spy.release.set()
        await task
        assert spy.calls[0]["persona_content"] == "旧正文"
        assert len(await context.load_context(bot, "private", "3001", session_id="sess_main")) == 2
        assert await context.load_context(bot, "private", "3001", session_id=other) == []
        await context.switch_session(bot, "private", "3001", "sess_main")
        await handler._handle_message(bot, event(mid=2), {})
        assert spy.calls[-1]["persona_content"] == "新正文"
        await handler.close()


async def test_deleted_and_recreated_session_rejects_old_reply():
    with tempfile.TemporaryDirectory() as td:
        _, context, service, handler, spy, bot = await setup(td)
        sid = await context.create_session(bot, "private", "3001", "旧会话")
        preset = await service.create_persona("旧角色", "旧会话正文")
        await service.bind_session(bot, "3001", sid, preset["id"])
        spy.release.clear()
        task = asyncio.create_task(handler._handle_message(bot, event(), {}))
        await spy.started.wait()
        assert await context.delete_session(bot, "private", "3001", sid)
        replacement = await context.create_session(bot, "private", "3001", "新会话")
        assert replacement == sid
        spy.release.set()
        await task
        assert await context.load_context(bot, "private", "3001", session_id=sid) == []
        binding = await service.get_session_binding(bot, "3001", sid)
        assert not binding["persona_id"]
        assert spy.calls[0]["persona_content"] == "旧会话正文"
        await handler.close()


async def test_presets_replace_legacy_and_no_special_user_override():
    with tempfile.TemporaryDirectory() as td:
        manager, _, service, handler, _, bot = await setup(td)
        preset = await service.create_persona("普通角色", "所有用户按配置")
        cfg = await service.update_bot_config(bot, {"persona_id": preset["id"]})
        config = GlobalConfig(data_dir=td)
        llm = LLMService(config, persona_service=service)
        try:
            for user in (3001, 3831097597, 38310975970):
                msgs = await llm._build_messages(bot, event(user), [], cfg)
                assert msgs[0]["content"].startswith("所有用户按配置")
            group = GroupMessageEvent(
                time=1, self_id=1001, post_type="message", message_type="group",
                message_id=1, group_id=999, user_id=3831097597,
                sender=Sender(user_id=3831097597, nickname="测试"),
                message=[{"type": "text", "data": {"text": "你好"}}],
            )
            msgs = await llm._build_messages(bot, group, [], cfg)
            assert msgs[0]["content"].startswith("所有用户按配置")
            snapshot = "捕获的专属正文"
            await service.update_persona(preset["id"], "修改", "新的默认正文")
            msgs = await llm._build_messages(bot, event(), [], cfg, snapshot)
            assert msgs[0]["content"].startswith(snapshot)
        finally:
            await llm.close()
            await handler.close()


async def test_registered_tool_followup_keeps_captured_persona():
    with tempfile.TemporaryDirectory() as td:
        manager, _, service, handler, _, bot = await setup(td)
        preset = await service.create_persona("角色", "旧人设")
        cfg = await service.update_bot_config(bot, {"persona_id": preset["id"]})
        requests = []

        async def create(**kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                await service.update_persona(preset["id"], "角色", "新人设")
                tool = SimpleNamespace(id="t1", function=SimpleNamespace(name="get_current_time", arguments="{}"))
                message = SimpleNamespace(content="", tool_calls=[tool])
            else:
                message = SimpleNamespace(content="完成", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        config = GlobalConfig(data_dir=td)
        llm = LLMService(config, persona_service=service)
        llm._available = True
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        llm._chat_client = client
        try:
            result, _ = await llm.chat(bot, event(), [], {}, cfg, "旧人设")
            assert result == "完成" and len(requests) == 2
            assert all(req["messages"][0]["content"].startswith("旧人设") for req in requests)
        finally:
            llm._chat_client = None
            await llm.close()
            await handler.close()


async def test_persona_group_command_dedup_handles_case_and_whitespace():
    with tempfile.TemporaryDirectory() as td:
        manager, _, _, handler, _, bot = await setup(td)
        other = manager.create_bot(nickname="other", qq=1002)
        manager._bots[other.bot_id] = BotInstance(other.bot_id, None, other)
        for name in (bot, other.bot_id):
            manager.note_group_message(name, 9001)
        for mid, text in enumerate(("/persona set bot_001 3001 sess_main persona_001",
                                    "/PERSONA list", "/persona\tlist"), 10):
            group = GroupMessageEvent(
                time=1, self_id=1001, post_type="message", message_type="group",
                message_id=mid, group_id=9001, user_id=3001,
                sender=Sender(user_id=3001, nickname="admin"),
                message=[{"type":"text", "data":{"text":text}}],
            )
            deferred = [handler._should_defer_global_command(name, group) for name in (bot, other.bot_id)]
            assert sum(deferred) == 1, (text, deferred)
        await handler.close()


async def main():
    for name, target in sorted(globals().items()):
        if name.startswith("test_"):
            await target()
            print("PASS", name)


if __name__ == "__main__":
    asyncio.run(main())
