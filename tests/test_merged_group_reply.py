"""群聊多 bot 合并回复测试:

1. 触发匹配: 精确/带参数命中, 无关文本不命中
2. 多 bot 群: 非选中 bot 静默跳过; 被选中的 bot 收集全部 bot 回复并发合并转发
3. 节点署名: user_id/nickname 为各 bot 自己, 顺序按 bot_id
4. 单 bot 群 / 未命中: 走原流程(返回 False)
5. 合并转发发送失败: 退化为发送者自己的普通回复
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.message_handler import MessageHandler
from mohobot.models.onebot import GroupMessageEvent


class _FakeConfig:
    def __init__(self, nickname: str = ""):
        self.nickname = nickname


class _FakeInstance:
    def __init__(self, bot_id: str, qq: int, nickname: str):
        self.bot_id = bot_id
        self.qq = qq
        self.config = _FakeConfig(nickname)


class _FakeBotManager:
    def __init__(self, bots: dict[str, _FakeInstance], group_bots: dict[str, set[str]]):
        self._bots = bots
        self._group_bots = group_bots

    def get(self, bot_id):
        return self._bots.get(bot_id)

    def bots_in_group(self, group_id):
        return sorted(
            b for b in self._group_bots.get(str(group_id), set()) if b in self._bots
        )

    def pick_bot_for_group(self, group_id, message_id=None):
        # 真实实现为 random.choice(随机); 测试固定选最小以保证确定性
        candidates = [
            b for b in self._group_bots.get(str(group_id), set()) if b in self._bots
        ]
        return min(candidates) if candidates else None


class _FakeWS:
    def __init__(self, bot_manager):
        self._bot_manager = bot_manager
        self.forwards = []   # (bot_id, group_id, nodes)
        self.replies = []    # (bot_id, group_id, message)
        self.fail_forward = False

    async def send_group_forward_msg(self, bot_id, group_id, nodes):
        if self.fail_forward:
            raise RuntimeError("forward failed")
        self.forwards.append((bot_id, group_id, nodes))

    async def send_group_msg(self, bot_id, group_id, message):
        self.replies.append((bot_id, group_id, message))


class _FakePlugins:
    """模拟 praise 类插件: 每个 bot 以自己身份回复。"""

    def __init__(self, fail_bot_ids=()):
        self.fail_bot_ids = set(fail_bot_ids)
        self.called = []

    async def intercept(self, bot_id, event, raw):
        self.called.append(bot_id)
        if bot_id in self.fail_bot_ids:
            raise RuntimeError("plugin error")
        return (True, f"来自 {bot_id} 的回复")

    async def dispatch_observed(self, bot_id, event, raw):
        return (False, None)


def _make_handler(ws, plugins, command_handler=None):
    h = MessageHandler(ws_server=None, context_manager=None, llm_service=None,
                       plugin_system=plugins)
    h._ws = ws
    h._plugins = plugins
    h._command_handler = command_handler
    return h


def _make_event(group_id=888, text="赞我", user_id=10086):
    return GroupMessageEvent(
        time=1000, self_id=0, post_type="message",
        message_type="group", sub_type="normal", message_id=1,
        user_id=user_id, group_id=group_id,
        message=[{"type": "text", "data": {"text": text}}],
        raw_message=text, font=0, sender={"nickname": "u", "card": "u"},
    )


def test_trigger_matching():
    assert MessageHandler._match_merged_trigger("赞我") == "赞我"
    assert MessageHandler._match_merged_trigger("/好感度") == "/好感度"
    assert MessageHandler._match_merged_trigger("/好感排行 15") == "/好感排行"
    assert MessageHandler._match_merged_trigger("zanwo") == "zanwo"
    assert MessageHandler._match_merged_trigger("你好") is None
    assert MessageHandler._match_merged_trigger("/帮助") is None
    assert MessageHandler._match_merged_trigger("赞我一下") is None  # 精确匹配


async def test_merged_reply_multi_bot():
    bots = {
        "bot_001": _FakeInstance("bot_001", 111, "大乔"),
        "bot_002": _FakeInstance("bot_002", 222, "小乔"),
        "bot_003": _FakeInstance("bot_003", 333, "阿三"),
    }
    bm = _FakeBotManager(bots, {"888": {"bot_001", "bot_002", "bot_003"}})
    ws = _FakeWS(bm)
    plugins = _FakePlugins()
    h = _make_handler(ws, plugins)

    async def run():
        # 非选中 bot: 静默跳过(返回 True, 不发送任何东西)
        event = _make_event()
        assert await h._try_merged_group_reply("bot_002", event, {}) is True
        assert ws.forwards == [] and plugins.called == []

        # 选中 bot: 收集全部 bot 回复, 发合并转发
        assert await h._try_merged_group_reply("bot_001", event, {}) is True
        assert len(ws.forwards) == 1
        fwd_bot, gid, nodes = ws.forwards[0]
        assert fwd_bot == "bot_001" and str(gid) == "888"
        assert [n["data"]["user_id"] for n in nodes] == ["111", "222", "333"]
        assert [n["data"]["nickname"] for n in nodes] == ["大乔", "小乔", "阿三"]
        assert nodes[0]["data"]["content"][0]["data"]["text"] == "来自 bot_001 的回复"
        # 每个 bot 都被以自己的身份调用过一次
        assert sorted(plugins.called) == ["bot_001", "bot_002", "bot_003"]

    await run()


async def test_merged_reply_failed_bot_placeholder():
    bots = {
        "bot_001": _FakeInstance("bot_001", 111, "大乔"),
        "bot_002": _FakeInstance("bot_002", 222, "小乔"),
    }
    bm = _FakeBotManager(bots, {"888": {"bot_001", "bot_002"}})
    ws = _FakeWS(bm)
    plugins = _FakePlugins(fail_bot_ids={"bot_002"})
    h = _make_handler(ws, plugins)

    async def run():
        event = _make_event(text="/好感度")
        assert await h._try_merged_group_reply("bot_001", event, {}) is True
        _, _, nodes = ws.forwards[0]
        assert nodes[0]["data"]["content"][0]["data"]["text"] == "来自 bot_001 的回复"
        assert nodes[1]["data"]["content"][0]["data"]["text"] == "(无响应)"

    await run()


async def test_merged_reply_single_bot_and_no_match():
    bots = {"bot_001": _FakeInstance("bot_001", 111, "大乔")}
    bm = _FakeBotManager(bots, {"888": {"bot_001"}})
    ws = _FakeWS(bm)
    plugins = _FakePlugins()
    h = _make_handler(ws, plugins)

    async def run():
        # 单 bot 群: 不走合并
        assert await h._try_merged_group_reply("bot_001", _make_event(), {}) is False
        # 未命中触发词
        bm2 = _FakeBotManager(bots, {"888": {"bot_001", "bot_002"}})
        bm2._bots["bot_002"] = _FakeInstance("bot_002", 222, "小乔")
        ws2 = _FakeWS(bm2)
        h2 = _make_handler(ws2, plugins)
        assert await h2._try_merged_group_reply("bot_001", _make_event(text="你好"), {}) is False

    await run()


async def test_merged_reply_forward_failure_fallback():
    bots = {
        "bot_001": _FakeInstance("bot_001", 111, "大乔"),
        "bot_002": _FakeInstance("bot_002", 222, "小乔"),
    }
    bm = _FakeBotManager(bots, {"888": {"bot_001", "bot_002"}})
    ws = _FakeWS(bm)
    ws.fail_forward = True
    plugins = _FakePlugins()
    h = _make_handler(ws, plugins)

    async def run():
        event = _make_event()
        assert await h._try_merged_group_reply("bot_001", event, {}) is True
        assert ws.forwards == []
        # 退化为发送者(bot_001)自己的普通回复
        assert len(ws.replies) == 1
        fb_bot, gid, text = ws.replies[0]
        assert fb_bot == "bot_001" and str(gid) == "888"
        assert "bot_001" in text

    await run()
