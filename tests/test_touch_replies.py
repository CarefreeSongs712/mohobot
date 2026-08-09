"""戳一戳固定回复配置测试:
1. 内置默认列表存在
2. CharacterReflex: bot 私有 > 全局配置 > 内置默认
3. set_touch_replies 运行时更新
4. message_handler._resolve_touch_replies 优先级
5. 非 agent bot poke 也能回复(所有 bot 生效)
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import GlobalConfig
from mohobot.models.onebot import NoticeEvent, Sender


def group_poke_event(user_id: int = 1001, group_id: int = 2001, target_id: int = 1) -> NoticeEvent:
    return NoticeEvent(
        time=0, self_id=1, post_type="notice",
        notice_type="notify", sub_type="poke",
        user_id=user_id, group_id=group_id, target_id=target_id,
    )


async def test_reflex_priority() -> None:
    """CharacterReflex 优先级: bot 私有 > 全局 > 默认。"""
    from mohobot.agent.character_reflex import (
        DEFAULT_TOUCH_REPLIES, CharacterReflex,
    )

    # 默认
    r = CharacterReflex(config={})
    assert r.touch_replies == DEFAULT_TOUCH_REPLIES

    # 全局配置(agent.reflex.touch_replies)
    r = CharacterReflex(config={"touch_replies": ["全局回复1", "全局回复2"]})
    assert r.touch_replies == ["全局回复1", "全局回复2"]

    # bot 私有覆盖全局
    r = CharacterReflex(
        config={"touch_replies": ["全局回复1"]},
        touch_replies=["bot回复1", "bot回复2"],
    )
    assert r.touch_replies == ["bot回复1", "bot回复2"]

    # set_touch_replies: 清空 bot 私有后回退全局
    r.set_touch_replies([])
    assert r.touch_replies == ["全局回复1"]
    print("[1] CharacterReflex 优先级 OK")


async def test_resolve_touch_replies() -> None:
    """message_handler._resolve_touch_replies: bot > 全局 > 默认。"""
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.context_manager import ContextManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import BotConfig, ReplyConfig

    class FakeWS:
        def __init__(self, bm):
            self._bot_manager = bm
            self.sent = []

        async def send_group_msg(self, bot_id, group_id, message):
            self.sent.append((bot_id, group_id, message))

        async def send_private_msg(self, bot_id, user_id, message):
            self.sent.append((bot_id, user_id, message))

    tmp = tempfile.mkdtemp(prefix="touch_")
    cfg = GlobalConfig()
    cfg.agent.reflex["touch_replies"] = ["全局戳1", "全局戳2"]
    bm = BotManager(data_dir=tmp)

    handler = MessageHandler(
        ws_server=FakeWS(bm),
        context_manager=ContextManager(data_dir=tmp),
        llm_service=None,
        plugin_system=None,
        data_dir=tmp,
        reply_config=ReplyConfig(),
        global_config=cfg,
    )

    # 无 bot 配置 → 用全局
    bm._bots["bot_a"] = BotInstance("bot_a", None, BotConfig(nickname="A"))
    replies = handler._resolve_touch_replies("bot_a")
    assert replies == ["全局戳1", "全局戳2"], replies

    # bot 私有覆盖全局
    bm._bots["bot_b"] = BotInstance(
        "bot_b", None,
        BotConfig(nickname="B", touch_replies=["bot戳1", "bot戳2"]),
    )
    replies = handler._resolve_touch_replies("bot_b")
    assert replies == ["bot戳1", "bot戳2"], replies

    # 都没有 → 默认
    cfg2 = GlobalConfig()  # 无全局配置
    handler2 = MessageHandler(
        ws_server=FakeWS(bm),
        context_manager=ContextManager(data_dir=tmp),
        llm_service=None,
        plugin_system=None,
        data_dir=tmp,
        reply_config=ReplyConfig(),
        global_config=cfg2,
    )
    from mohobot.agent.character_reflex import DEFAULT_TOUCH_REPLIES
    assert handler2._resolve_touch_replies("bot_a") == DEFAULT_TOUCH_REPLIES
    print("[2] _resolve_touch_replies 优先级 OK")


async def test_poke_all_bots() -> None:
    """agent_enabled=false 的 bot 戳一戳也会固定回复。"""
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.context_manager import ContextManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import BotConfig, ReplyConfig

    class FakeWS:
        def __init__(self, bm):
            self._bot_manager = bm
            self.sent = []

        async def send_group_msg(self, bot_id, group_id, message):
            self.sent.append((bot_id, group_id, message))

        async def send_private_msg(self, bot_id, user_id, message):
            self.sent.append((bot_id, user_id, message))

    tmp = tempfile.mkdtemp(prefix="poke_")
    cfg = GlobalConfig()
    cfg.agent.reflex["touch_replies"] = ["全局戳回复"]
    bm = BotManager(data_dir=tmp)
    # agent_enabled=false 的 bot(旧版路径)
    bm._bots["bot_003"] = BotInstance(
        "bot_003", None,
        BotConfig(qq=1, nickname="墨清弦", persona="你是墨清弦。", agent_enabled=False),
    )
    ws = FakeWS(bm)
    handler = MessageHandler(
        ws_server=ws,
        context_manager=ContextManager(data_dir=tmp),
        llm_service=None,
        plugin_system=None,
        data_dir=tmp,
        reply_config=ReplyConfig(),
        global_config=cfg,
        agent_manager=None,
    )

    ev = group_poke_event(user_id=555, group_id=777, target_id=1)
    await handler._handle_poke("bot_003", ev)
    assert len(ws.sent) == 1, f"应发送 1 条, 实际 {len(ws.sent)}"
    bot_id, group_id, msg = ws.sent[0]
    assert bot_id == "bot_003" and group_id == 777
    assert msg == "全局戳回复", msg
    print("[3] 非 agent bot 戳一戳固定回复 OK")


async def test_poke_ignore_other_target() -> None:
    """戳的不是本 bot → 忽略。"""
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.context_manager import ContextManager
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import BotConfig, ReplyConfig

    class FakeWS:
        def __init__(self, bm):
            self._bot_manager = bm
            self.sent = []

        async def send_group_msg(self, bot_id, group_id, message):
            self.sent.append((bot_id, group_id, message))

        async def send_private_msg(self, bot_id, user_id, message):
            self.sent.append((bot_id, user_id, message))

    tmp = tempfile.mkdtemp(prefix="poke_ig_")
    bm = BotManager(data_dir=tmp)
    bm._bots["bot_003"] = BotInstance(
        "bot_003", None,
        BotConfig(qq=999, nickname="墨清弦", agent_enabled=False),
    )
    ws = FakeWS(bm)
    handler = MessageHandler(
        ws_server=ws,
        context_manager=ContextManager(data_dir=tmp),
        llm_service=None,
        plugin_system=None,
        data_dir=tmp,
        reply_config=ReplyConfig(),
    )
    # target_id=888 ≠ bot QQ 999 → 忽略
    await handler._handle_poke("bot_003", group_poke_event(user_id=555, group_id=777, target_id=888))
    assert len(ws.sent) == 0
    print("[4] 戳别人忽略 OK")


async def main() -> None:
    await test_reflex_priority()
    await test_resolve_touch_replies()
    await test_poke_all_bots()
    await test_poke_ignore_other_target()
    print("\nALL TOUCH REPLY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
