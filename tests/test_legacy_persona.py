"""回归测试: 旧版直接流式回复路径必须使用 bot 私有配置的人设。

bug 背景: message_handler 调 LLMService.chat / chat_stream 时没有传 bot_config,
导致 _build_messages 里 persona 回退成默认 "你是 Mohobot..." —— 人设丢失。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.onebot import PrivateMessageEvent, Sender


def private_event(text: str, user_id: int = 3001) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=0, self_id=1, post_type="message",
        message_type="private", message_id=1,
        user_id=user_id,
        sender=Sender(user_id=user_id, nickname="测试用户"),
        message=[{"type": "text", "data": {"text": text}}],
    )


async def test_build_messages_uses_bot_persona() -> None:
    """LLMService._build_messages 应把 BotConfig.persona 放入 system prompt。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.config import BotConfig, GlobalConfig

    svc = LLMService(global_config=GlobalConfig())
    bot_cfg = BotConfig(qq=3001, nickname="墨清弦", persona="你是墨清弦。")
    msgs = await svc._build_messages("bot_003", private_event("你好"), [], bot_cfg)

    assert msgs[0]["role"] == "system"
    assert "你是墨清弦。" in msgs[0]["content"], msgs[0]["content"]
    assert "你是 Mohobot" not in msgs[0]["content"]

    # 不传 bot_config → 仍是默认人设(旧行为保持)
    msgs2 = await svc._build_messages("bot_003", private_event("你好"), [], None)
    assert "你是 Mohobot" in msgs2[0]["content"]
    print("[1] _build_messages 使用 bot 私有 persona OK")


async def test_stream_reply_passes_bot_config() -> None:
    """MessageHandler 旧版路径必须把 bot_config 传给 LLMService。"""
    import mohobot.message_handler as mh_mod
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.context_manager import ContextManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import BotConfig, GlobalConfig, ReplyConfig

    class FakeWS:
        def __init__(self, bm):
            self._bot_manager = bm
            self.sent = []

        async def send_private_msg(self, bot_id, user_id, message):
            self.sent.append(message)

    class SpyLLMService:
        """记录 chat_stream 收到的 bot_config。"""
        def __init__(self):
            self.captured = []

        async def chat_stream(self, **kwargs):
            self.captured.append(kwargs.get("bot_config"))
            yield ("你好，我是墨清弦。", True)

        async def chat(self, **kwargs):
            self.captured.append(kwargs.get("bot_config"))
            return ("你好，我是墨清弦。", None)

    tmp = tempfile.mkdtemp(prefix="persona_")
    bot_manager = BotManager(data_dir=tmp)
    bot_manager._bots["bot_003"] = BotInstance(
        "bot_003", None,
        BotConfig(qq=3616174427, nickname="墨清弦", persona="你是墨清弦。"),
    )
    spy = SpyLLMService()
    handler = MessageHandler(
        ws_server=FakeWS(bot_manager),
        context_manager=ContextManager(data_dir=tmp),
        llm_service=spy,
        plugin_system=None,
        data_dir=tmp,
        reply_config=ReplyConfig(stream=True, segment_reply=True),
        database_manager=None,
    )

    # 分段流式路径
    ev = private_event("你好")
    await handler._stream_llm_reply("bot_003", ev, [], {})
    assert spy.captured and spy.captured[-1] is not None, "chat_stream 未收到 bot_config"
    assert spy.captured[-1].persona == "你是墨清弦。"

    # 单条(非分段)路径
    handler2 = MessageHandler(
        ws_server=FakeWS(bot_manager),
        context_manager=ContextManager(data_dir=tmp),
        llm_service=spy,
        plugin_system=None,
        data_dir=tmp,
        reply_config=ReplyConfig(stream=True, segment_reply=False),
        database_manager=None,
    )
    await handler2._stream_llm_reply("bot_003", ev, [], {})
    assert spy.captured and spy.captured[-1] is not None, "chat_stream(单条) 未收到 bot_config"
    assert spy.captured[-1].persona == "你是墨清弦。"

    # 非流式路径
    handler3 = MessageHandler(
        ws_server=FakeWS(bot_manager),
        context_manager=ContextManager(data_dir=tmp),
        llm_service=spy,
        plugin_system=None,
        data_dir=tmp,
        reply_config=ReplyConfig(stream=False, segment_reply=True),
        database_manager=None,
    )
    await handler3._stream_llm_reply("bot_003", ev, [], {})
    assert spy.captured and spy.captured[-1] is not None, "chat(非流式) 未收到 bot_config"
    assert spy.captured[-1].persona == "你是墨清弦。"
    print("[2] 旧版路径三种模式均传递 bot_config OK")


async def main() -> None:
    await test_build_messages_uses_bot_persona()
    await test_stream_reply_passes_bot_config()
    print("\nALL LEGACY PERSONA TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
