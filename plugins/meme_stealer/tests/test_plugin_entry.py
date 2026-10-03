"""main.py（mohobot Plugin 入口）：钩子、去重、门控、LLM 工具与回复后发表情。

用最小的 mohobot 替身（WSServer / BotManager / LLM 工具注册表）驱动真实的插件实例。
"""

import asyncio
import importlib.util
import inspect
import json
import sys
import types
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image as PILImage

PLUGIN_DIR = Path(__file__).resolve().parents[1]


# ── mohobot 替身 ────────────────────────────────────────


class FakeRegistry:
    """与 mohobot.services.llm_tools.LLMToolRegistry 行为一致的最小实现。"""

    def __init__(self):
        self._tools = {}

    def register(self, tool):
        if tool.name in self._tools:
            raise ValueError(f"duplicate LLM tool: {tool.name}")
        self._tools[tool.name] = tool

    def contains(self, name):
        return name in self._tools

    async def execute(self, name, arguments):
        tool = self._tools[name]
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        result = tool.handler(**args)
        if inspect.isawaitable(result):
            result = await result
        return result


class FakeLLMTool:
    def __init__(self, schema, handler):
        self.schema = schema
        self.handler = handler

    @property
    def name(self):
        return self.schema["function"]["name"]


def _tool_schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


class FakeWS:
    def __init__(self):
        self.sent = []
        self._on_event = None

    async def send_group_msg(self, bot_id, group_id, message, source="auto"):
        self.sent.append(("group", bot_id, str(group_id), message))

    async def send_private_msg(self, bot_id, user_id, message, source="auto"):
        self.sent.append(("private", bot_id, str(user_id), message))

    async def send_to_bot(self, bot_id, action, params=None, wait_response=False, timeout=10.0):
        return {"status": "ok", "data": {}}


class FakeBotInstance:
    def __init__(self, bot_id, qq):
        self.bot_id = bot_id
        self.qq = qq
        self.sent_ids = set()

    def is_my_message(self, chat_type, chat_id, message_id):
        return str(message_id) in self.sent_ids


class FakeBotManager:
    def __init__(self, *bots):
        self._bots = {bot.bot_id: bot for bot in bots}

    def get(self, bot_id):
        return self._bots.get(bot_id)

    @property
    def all_bots(self):
        return list(self._bots.values())


def _group_event(text="", *, message_id=1, user_id=42, segments=None, group_id=123):
    message = list(segments or [])
    if text:
        message.append({"type": "text", "data": {"text": text}})
    raw = {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": user_id,
        "self_id": 10001,
        "message_id": message_id,
        "sender": {"user_id": user_id, "nickname": "小明"},
        "message": message,
    }
    event = types.SimpleNamespace(
        group_id=group_id, user_id=user_id, self_id=10001, message_id=message_id,
        message=message, sender=types.SimpleNamespace(nickname="小明", card=""),
    )
    return event, raw


@pytest.fixture
def mohobot(monkeypatch):
    registry = FakeRegistry()
    llm_tools = types.ModuleType("mohobot.services.llm_tools")
    llm_tools.LLMTool = FakeLLMTool
    llm_tools.registry = registry
    llm_tools.tool_schema = _tool_schema
    monkeypatch.setitem(sys.modules, "mohobot", types.ModuleType("mohobot"))
    monkeypatch.setitem(sys.modules, "mohobot.services", types.ModuleType("mohobot.services"))
    monkeypatch.setitem(sys.modules, "mohobot.services.llm_tools", llm_tools)
    return registry


@pytest.fixture
def entry():
    spec = importlib.util.spec_from_file_location("meme_stealer_entry_under_test", PLUGIN_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def plugin(entry, mohobot, tmp_path):
    ws = FakeWS()
    bots = FakeBotManager(FakeBotInstance("bot_001", 10001), FakeBotInstance("bot_002", 10002))
    cls = entry.Plugin
    cls.inject_data_dir(str(tmp_path))
    cls.inject_ws_server(ws)
    cls.inject_bot_manager(bots)
    cls.inject_admin_ids([7777])
    cls._task_supervisor = None
    cls._llm_service = None
    instance = cls()
    instance.plugin_config = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text("utf-8"))
    instance.plugin_config = {k: v["default"] for k, v in instance.plugin_config.items()}
    instance.plugin_config.update({"meme_send_delay": 0.0, "meme_chance": 1.0})
    await instance.on_startup()
    assert instance.app is not None
    instance.ws = ws
    instance.bots = bots
    instance.registry = mohobot
    yield instance
    await instance.on_shutdown()


async def _add_meme(app, tmp_path, name="cat.png"):
    path = app.plugin_config.ensure_category_dir("uncategorized") / name
    PILImage.new("RGB", (16, 16), "red").save(path)
    await app.db_service.insert_batch(
        [{"path": str(path), "hash": name, "category": "uncategorized", "desc": "惊讶的猫"}]
    )
    return str(path)


# ── 生命周期与工具注册 ──────────────────────────────────


@pytest.mark.asyncio
async def test_startup_registers_tools_and_shutdown_removes_them(plugin, tmp_path):
    assert set(plugin.registry._tools) == {"search_meme", "send_meme", "steal_meme"}
    schema = plugin.registry._tools["steal_meme"].schema["function"]
    assert "可以留空" in schema["description"]
    assert schema["parameters"]["required"] == []
    search = plugin.registry._tools["search_meme"].schema["function"]
    assert "send_meme" in search["description"]
    assert plugin.registry._tools["send_meme"].schema["function"]["parameters"]["properties"]["emoji_id"]["type"] == "integer"
    assert (tmp_path / "plugins_data" / "meme_stealer" / "cache" / "emoji.db").is_file()

    await plugin.on_shutdown()
    assert plugin.registry._tools == {}


@pytest.mark.asyncio
async def test_reload_replaces_leftover_tools(entry, mohobot, tmp_path):
    mohobot._tools["search_meme"] = FakeLLMTool(_tool_schema("search_meme", "old", {}, []), lambda: "old")
    entry.Plugin.inject_data_dir(str(tmp_path))
    instance = entry.Plugin()
    await instance.on_startup()
    try:
        assert mohobot._tools["search_meme"].schema["function"]["description"] != "old"
    finally:
        await instance.on_shutdown()


# ── 指令 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_meme_command_routes_and_checks_admin(plugin):
    event, raw = _group_event("/meme status", user_id=42)
    handled, reply = await plugin.on_message("bot_001", event, raw)
    assert handled and "插件状态" in reply

    event, raw = _group_event("/meme on", user_id=42, message_id=2)
    handled, reply = await plugin.on_message("bot_001", event, raw)
    assert handled and "仅限管理员" in reply
    assert plugin.app.plugin_config.steal_meme is False

    event, raw = _group_event("/meme on", user_id=7777, message_id=3)
    handled, reply = await plugin.on_message("bot_001", event, raw)
    assert handled and reply == "已开启偷表情包"
    saved = json.loads((Path(plugin._data_dir) / "plugins_config" / "meme_stealer.json").read_text("utf-8"))
    assert saved["steal_meme"] is True

    event, raw = _group_event("/memes", message_id=4)
    assert await plugin.on_message("bot_001", event, raw) == (False, None)
    event, raw = _group_event("hello", message_id=5)
    assert await plugin.on_message("bot_001", event, raw) == (False, None)


# ── 观察与去重 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_message_is_processed_once_across_bots(plugin):
    seen = []

    async def on_message(event):
        seen.append((event.bot_id, event.message_id))

    plugin.app.on_message = on_message
    for bot_id in ("bot_001", "bot_002"):
        event, raw = _group_event("看这个", message_id=10)
        assert await plugin.on_message_observed(bot_id, event, raw) == (False, None)
    assert seen == [("bot_001", "10")]

    # 其他 bot 自己发的消息不偷
    event, raw = _group_event("bot 的回复", message_id=11, user_id=10002)
    await plugin.on_message_observed("bot_001", event, raw)
    assert seen == [("bot_001", "10")]


# ── 选图提示门控 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_hint_only_for_messages_the_bot_will_answer(plugin, tmp_path):
    await _add_meme(plugin.app, tmp_path)

    event, raw = _group_event("大家好", message_id=20)
    assert await plugin.on_perception("bot_001", event, raw) == ""

    event, raw = _group_event("/meme status", message_id=21)
    assert await plugin.on_perception("bot_001", event, raw) == ""

    at_other = [{"type": "at", "data": {"qq": "10002"}}]
    event, raw = _group_event("你好", segments=at_other, message_id=22)
    assert await plugin.on_perception("bot_001", event, raw) == ""

    at_me = [{"type": "at", "data": {"qq": "10001"}}]
    event, raw = _group_event("你好", segments=at_me, message_id=23)
    hint = await plugin.on_perception("bot_001", event, raw)
    assert "search_meme" in hint and "send_meme" in hint

    plugin.bots.get("bot_001").sent_ids.add("900")
    reply_to_me = [{"type": "reply", "data": {"id": "900"}}]
    event, raw = _group_event("哈哈", segments=reply_to_me, message_id=24)
    # 20 秒冷却只在真正发出表情后才开始，这里仍然通过
    assert await plugin.on_perception("bot_001", event, raw)


@pytest.mark.asyncio
async def test_stale_hint_is_cleared_from_mohobot_perception_cache(plugin, tmp_path):
    await _add_meme(plugin.app, tmp_path)

    class Handler:
        def __init__(self):
            self._perception_text = {}

        async def handle_event(self, *args):
            pass

    handler = Handler()
    plugin.ws._on_event = handler.handle_event

    at_me = [{"type": "at", "data": {"qq": "10001"}}]
    event, raw = _group_event("你好", segments=at_me, message_id=30)
    hint = await plugin.on_perception("bot_001", event, raw)
    handler._perception_text[("bot_001", "group", "123")] = hint

    plugin.app.plugin_config.meme_chance = 0.0
    event, raw = _group_event("再来", segments=at_me, message_id=31)
    assert await plugin.on_perception("bot_001", event, raw) == ""
    assert ("bot_001", "group", "123") not in handler._perception_text


# ── 工具调用与回复后发送 ────────────────────────────────


@pytest.mark.asyncio
async def test_tools_use_current_message_and_meme_is_sent_after_the_turn(plugin, tmp_path):
    path = await _add_meme(plugin.app, tmp_path)

    async def semantic_candidates(description, *, limit, event=None):
        return [(path, {"desc": "惊讶的猫", "overlay_text": "啊？"}, 0.9)]

    plugin.app.meme_selector.semantic_candidates = semantic_candidates

    async def handle_message():
        """模拟 mohobot 的单条消息 task：感知 → 观察 → LLM 工具调用 → 发文字回复。"""
        at_me = [{"type": "at", "data": {"qq": "10001"}}]
        event, raw = _group_event("给我来张表情", segments=at_me, message_id=40)
        await plugin.on_perception("bot_001", event, raw)
        await plugin.on_message_observed("bot_001", event, raw)
        found = await plugin.registry.execute("search_meme", '{"description": "惊讶的猫"}')
        assert "啊？" in found
        queued = await plugin.registry.execute("send_meme", '{"emoji_id": 1}')
        assert "自动发出" in queued
        await plugin.ws.send_group_msg("bot_001", 123, "文字回复")
        # 表情要等本条消息处理完才发
        assert len(plugin.ws.sent) == 1

    await asyncio.create_task(handle_message())
    for _ in range(50):
        if len(plugin.ws.sent) >= 2:
            break
        await asyncio.sleep(0.02)

    assert plugin.ws.sent[0][3] == "文字回复"
    kind, bot_id, chat_id, segments = plugin.ws.sent[1]
    assert (kind, bot_id, chat_id) == ("group", "bot_001", "123")
    assert segments[0]["type"] == "image"
    assert segments[0]["data"]["sub_type"] == 1
    assert segments[0]["data"]["file"].startswith("base64://")
    assert plugin.app.db_service.get_emoji(path)["use_count"] == 1


@pytest.mark.asyncio
async def test_tools_without_message_context_fail_gracefully(plugin):
    async def outside():
        return await plugin.registry.execute("search_meme", {"description": "猫"})

    assert "不可用" in await asyncio.create_task(outside())


# ── 配置热更新 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_config_update_applies_to_running_app(plugin):
    config = dict(plugin.plugin_config)
    config.update({"meme_chance": 0.5, "send_target_blacklist": ["group:123"]})
    plugin.on_config_update(config)

    cfg = plugin.app.plugin_config
    assert cfg.meme_chance == 0.5
    event, raw = _group_event("x", message_id=50)
    stealer_event = plugin._bind_event("bot_001", event, raw)
    assert plugin.app.is_send_enabled_for_event(stealer_event) is False
