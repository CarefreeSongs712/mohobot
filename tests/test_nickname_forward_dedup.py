"""三个修复的测试:
1. get_nickname: TTL 缓存 + strip + 失败短缓存重试(昵称修复)
2. 合并转发: _send_reply 超长文本自动改合并转发, 失败回退
3. 全局指令去重: 群内多 bot 时 /占卜 /help 只由 bot_id 最小者回复
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotInstance, BotManager
from mohobot.models.config import BotConfig
from mohobot.models.onebot import GroupMessageEvent, Sender


def make_group_event(user_id, text, self_id=1000):
    return GroupMessageEvent(
        time=0, self_id=self_id, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=888888,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


# ── 1. get_nickname ─────────────────────────────────────────

class NickWS:
    """模拟 ws_server.get_nickname 依赖: send_to_bot 返回可控响应。"""

    def __init__(self):
        self.calls = []
        self.member_resp = None      # get_group_member_info 响应
        self.stranger_resp = None    # get_stranger_info 响应

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        self.calls.append(action)
        if action == "get_group_member_info":
            return self.member_resp
        if action == "get_stranger_info":
            return self.stranger_resp
        return {"status": "ok", "retcode": 0, "data": {}}

    def send_group_msg(self, *a, **kw):
        pass


async def test_nickname_ttl_and_strip() -> None:
    """get_nickname: 空白名片回退昵称 + TTL 缓存 + 失败短缓存。"""
    from mohobot.ws_server import WSServer

    ws = WSServer(bot_manager=BotManager(data_dir=tempfile.mkdtemp(prefix="nk_")), port=0)
    fw = NickWS()
    ws._bot_manager._bots["bot_001"] = BotInstance(
        "bot_001", None, BotConfig(qq=1000, nickname="B1", agent_enabled=False),
    )
    ws.send_to_bot = fw.send_to_bot  # 替换为 mock

    # 1. 群名片为空格 → 回退昵称
    fw.member_resp = {"status": "ok", "retcode": 0, "data": {"card": "  ", "nickname": "天依"}}
    name = await ws.get_nickname("bot_001", 2001, 888888)
    assert name == "天依", f"空白名片应回退昵称: {name!r}"

    # 2. 缓存命中(不再调 API)
    fw.member_resp = {"status": "ok", "retcode": 0, "data": {"card": "阿绫", "nickname": "luo"}}
    name2 = await ws.get_nickname("bot_001", 2001, 888888)
    assert name2 == "天依", "TTL 内应命中缓存"
    assert fw.calls.count("get_group_member_info") == 1

    # 3. 失败(返回数字) → 短缓存后可重试
    ws._nickname_cache.clear()
    fw.calls.clear()
    fw.member_resp = {"status": "failed", "retcode": 1, "data": None}
    fw.stranger_resp = {"status": "ok", "retcode": 0, "data": {"nickname": "墨清弦"}}
    name3 = await ws.get_nickname("bot_001", 2002, 888888)
    assert name3 == "墨清弦", f"群资料失败应走陌生人昵称: {name3}"
    # 群资料+陌生人全失败 → 数字 + 短缓存(30s)可重试
    fw.stranger_resp = {"status": "failed", "retcode": 1, "data": None}
    ws._nickname_cache.clear()
    name4 = await ws.get_nickname("bot_001", 2003, 888888)
    assert name4 == "2003"
    # 立即再查: 命中短缓存(不重试)
    fw.calls.clear()
    await ws.get_nickname("bot_001", 2003, 888888)
    assert fw.calls == [], "失败结果 30 秒内应缓存"
    # 手工过期 → 重试成功
    ws._nickname_cache[next(iter(ws._nickname_cache))] = (name4, 0)
    fw.member_resp = {"status": "ok", "retcode": 0, "data": {"card": "乐正绫", "nickname": "yy"}}
    name5 = await ws.get_nickname("bot_001", 2003, 888888)
    assert name5 == "乐正绫", "缓存过期后应重试获取"
    print("[1] get_nickname TTL/strip/失败重试 OK")


# ── 2. 合并转发 ─────────────────────────────────────────────

class ForwardWS:
    def __init__(self, bot_manager, fail_forward=False):
        self._bot_manager = bot_manager
        self.forward_calls = []
        self.plain_calls = []
        self.fail_forward = fail_forward

    async def send_group_forward_msg(self, bot_id, group_id, nodes):
        if self.fail_forward:
            raise RuntimeError("client not support")
        self.forward_calls.append((bot_id, group_id, nodes))

    async def send_group_msg(self, bot_id, group_id, message):
        self.plain_calls.append((bot_id, group_id, message))

    async def send_private_msg(self, bot_id, user_id, message):
        self.plain_calls.append((bot_id, user_id, message))


async def test_forward_long_reply() -> None:
    """长文本回复自动合并转发; 短文本正常发送; 失败回退普通。"""
    from mohobot.message_handler import MessageHandler
    from mohobot.context_manager import ContextManager
    from mohobot.models.config import ReplyConfig

    bm = BotManager(data_dir=tempfile.mkdtemp(prefix="fw_"))
    bm._bots["bot_001"] = BotInstance(
        "bot_001", None, BotConfig(qq=1000, nickname="天依beta", agent_enabled=False),
    )
    ws = ForwardWS(bm)
    handler = MessageHandler(
        ws_server=ws,
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None,
        plugin_system=None,
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(stream=False),
    )
    ev = make_group_event(2001, "/help")

    # 长文本(>600 字符) → 合并转发
    long_text = "\n".join(f"第 {i} 行: " + "帮助说明内容" * 8 for i in range(20))
    assert len(long_text) > 600
    await handler._send_reply("bot_001", ev, long_text)
    assert ws.forward_calls, "长文本应走合并转发"
    assert not ws.plain_calls
    bot_id, gid, nodes = ws.forward_calls[-1]
    assert str(gid) == "888888"
    assert len(nodes) == 20
    node = nodes[0]
    assert node["type"] == "node"
    assert node["data"]["user_id"] == "1000", "节点署名应为 bot QQ"
    assert node["data"]["nickname"] == "天依beta"
    assert node["data"]["content"][0]["type"] == "text"

    # 短文本(<600 字符) → 普通发送, 不拆合并转发
    ws.forward_calls.clear()
    short_text = "\n".join(f"第 {i} 行: 帮助说明" for i in range(30))
    assert len(short_text) < 600
    await handler._send_reply("bot_001", ev, short_text)
    assert not ws.forward_calls and ws.plain_calls, "600 字以内应普通发送"

    # 合并转发失败 → 回退普通发送
    ws.fail_forward = True
    ws.forward_calls.clear()
    ws.plain_calls.clear()
    await handler._send_reply("bot_001", ev, long_text)
    assert not ws.forward_calls and ws.plain_calls, "失败应回退普通发送"
    print("[2] 长文本合并转发 + 失败回退 OK")


# ── 3. 全局指令去重 ─────────────────────────────────────────

class DedupPlugins:
    """mock 插件系统: 一个声明 global_triggers 的插件。"""

    def __init__(self):
        class FakeDivination:
            global_triggers = {"/占卜", "占卜", "今日占卜"}

        class FakeOther:
            pass  # 无 global_triggers 的插件(如 relationship)

        self._plugins = [
            {"enabled": True, "loaded": True, "instance": FakeDivination()},
            {"enabled": True, "loaded": True, "instance": FakeOther()},
        ]

    async def dispatch_observed(self, *a, **kw):
        return (False, None)


async def test_global_command_dedup() -> None:
    """群内多 bot: /占卜 /help 只由 bot_id 最小者回复。"""
    from mohobot.message_handler import MessageHandler
    from mohobot.context_manager import ContextManager
    from mohobot.models.config import ReplyConfig

    bm = BotManager(data_dir=tempfile.mkdtemp(prefix="dd_"))
    bm._bots["bot_002"] = BotInstance(
        "bot_002", None, BotConfig(qq=1000, nickname="B2", agent_enabled=False),
    )
    bm._bots["bot_003"] = BotInstance(
        "bot_003", None, BotConfig(qq=1001, nickname="B3", agent_enabled=False),
    )
    ws = ForwardWS(bm)
    handler = MessageHandler(
        ws_server=ws,
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None,
        plugin_system=DedupPlugins(),
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(stream=False),
    )
    handler._interceptors = []

    # 两个 bot 都在群 888888
    bm.note_group_message("bot_002", 888888)
    bm.note_group_message("bot_003", 888888)
    assert bm.min_bot_for_group(888888) == "bot_002"

    # bot_003 收到 /占卜 → 应被去重跳过(不进入拦截链/LLM)
    skipped = handler._should_defer_global_command("bot_003", make_group_event(2001, "/占卜"))
    assert skipped is True, "非最小 bot 应跳过全局指令"
    # bot_002 收到 /占卜 → 不应跳过
    skipped2 = handler._should_defer_global_command("bot_002", make_group_event(2001, "/占卜"))
    assert skipped2 is False

    # /help 同样
    assert handler._should_defer_global_command("bot_003", make_group_event(2001, "/help")) is True
    assert handler._should_defer_global_command("bot_002", make_group_event(2001, "/help")) is False

    # 普通消息不去重
    assert handler._should_defer_global_command("bot_003", make_group_event(2001, "/今日老婆")) is False
    # 无前缀占卜(不进 gate 的消息)也匹配去重
    assert handler._should_defer_global_command("bot_003", make_group_event(2001, "占卜")) is True

    # 封禁系统命令(带参数)前缀匹配去重
    assert handler._should_defer_global_command("bot_003", make_group_event(2001, "/ban @2001 1h")) is True
    assert handler._should_defer_global_command("bot_003", make_group_event(2001, "/banlist")) is True
    assert handler._should_defer_global_command("bot_003", make_group_event(2001, "/pass-all 2001")) is True
    assert handler._should_defer_global_command("bot_003", make_group_event(2001, "/dec-ban 2001")) is True
    # bot_002 是群内最小 bot → 不跳过
    assert handler._should_defer_global_command("bot_002", make_group_event(2001, "/ban @2001 1h")) is False

    # 群内只有自己一个 bot → 不去重
    bm2 = BotManager(data_dir=tempfile.mkdtemp(prefix="dd2_"))
    bm2._bots["bot_002"] = BotInstance(
        "bot_002", None, BotConfig(qq=1000, nickname="B2", agent_enabled=False),
    )
    bm2.note_group_message("bot_002", 888888)
    ws2 = ForwardWS(bm2)
    handler2 = MessageHandler(
        ws_server=ws2,
        context_manager=ContextManager(data_dir=tempfile.mkdtemp()),
        llm_service=None,
        plugin_system=DedupPlugins(),
        data_dir=tempfile.mkdtemp(),
        reply_config=ReplyConfig(stream=False),
    )
    assert handler2._should_defer_global_command("bot_002", make_group_event(2001, "/占卜")) is False

    # 断开清理: bot_003 断开后只剩 bot_002
    bm.forget_bot_groups("bot_003")
    assert bm.min_bot_for_group(888888) == "bot_002"
    print("[3] 全局指令多 bot 去重 OK")


async def test_all() -> None:
    await test_nickname_ttl_and_strip()
    await test_forward_long_reply()
    await test_global_command_dedup()
    print("\nALL NEW FIX TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(test_all())
