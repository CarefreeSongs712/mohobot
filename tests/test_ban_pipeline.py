"""封禁消息管线回归: 全部文件写入临时目录, 模型/OneBot/插件均为 fake。

直接运行本文件使用标准库 unittest, 不需要 pytest 或真实模型/网络服务。
"""

import asyncio
import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.ban.ban_filter import BanInterceptor
from mohobot.ban.store import BanStore
from mohobot.interceptors.command_handler import CommandHandler
from mohobot.message_handler import MessageHandler
from mohobot.models.config import BanConfig, BotConfig, GlobalConfig, ReplyConfig
from mohobot.models.onebot import Event


class _Store(BanStore):
    def __init__(self, data_dir, trace):
        super().__init__(data_dir=data_dir)
        self.trace = trace
        self.checks = []
        self.mutations = []

    async def is_banned(self, session_key, uid):
        self.trace.append(("ban_check", session_key, uid))
        self.checks.append((session_key, uid))
        return await super().is_banned(session_key, uid)

    async def upsert(self, list_name, uid, **kwargs):
        self.mutations.append((list_name, uid))
        return await super().upsert(list_name, uid, **kwargs)


class _Ban(BanInterceptor):
    def __init__(self, store, trace):
        super().__init__(store=store, admins=[1001])
        self.trace = trace
        self.commands = []

    async def _execute_command(self, cmd_name, rest, event, bot_id):
        self.trace.append(("ban_command", bot_id, cmd_name))
        self.commands.append((bot_id, cmd_name))
        return await super()._execute_command(cmd_name, rest, event, bot_id)


class _BotManager:
    def __init__(self, trace):
        self.trace = trace
        self.chosen = "bot_002"
        self.bots = {
            bot: SimpleNamespace(
                qq=qq, config=BotConfig(bot_id=bot, qq=qq, nickname=bot),
                is_my_message=lambda *args: False,
            )
            for bot, qq in (("bot_001", 11), ("bot_002", 22), ("bot_003", 33))
        }

    def get(self, bot_id):
        return self.bots.get(bot_id)

    def note_group_message(self, bot_id, group_id):
        self.trace.append(("note_group", bot_id, group_id))

    def bots_in_group(self, group_id):
        return sorted(self.bots)

    def pick_bot_for_group(self, group_id, message_id=None):
        self.trace.append(("pick_bot", group_id, message_id))
        return self.chosen


class _WS:
    def __init__(self, trace):
        self._bot_manager = _BotManager(trace)
        self.sent = []
        self.forwards = []
        self.api_calls = []

    async def send_private_msg(self, bot_id, user_id, message, source="auto"):
        self.sent.append(("private", bot_id, user_id, message, source))

    async def send_group_msg(self, bot_id, group_id, message, source="auto"):
        self.sent.append(("group", bot_id, group_id, message, source))

    async def send_group_forward_msg(self, bot_id, group_id, nodes, **kwargs):
        self.forwards.append((bot_id, group_id, nodes))

    async def send_to_bot(self, bot_id, action, params, **kwargs):
        self.api_calls.append((bot_id, action, params))
        if action != "get_image":
            raise AssertionError(f"Unexpected fake API: {action}")
        return {"status": "ok", "data": {"base64": base64.b64encode(b"fake image").decode()}}


class _Plugins:
    def __init__(self, trace):
        self.trace = trace
        self._plugins = []
        self.consume_observed = False

    async def collect_perception(self, bot_id, event, raw):
        self.trace.append(("perception", bot_id))
        return "fake perception"

    async def dispatch_observed(self, bot_id, event, raw):
        self.trace.append(("observe", bot_id))
        return (self.consume_observed, "observer reply" if self.consume_observed else None)

    async def intercept(self, bot_id, event, raw):
        self.trace.append(("plugin_intercept", bot_id))
        if BanInterceptor._extract_text(event) in ("赞我", "/好感度"):
            return (True, f"merged reply from {bot_id}")
        return (False, None)

    async def dispatch_notice(self, bot_id, event, raw):
        self.trace.append(("notice", bot_id))

    async def dispatch_request(self, bot_id, event, raw):
        self.trace.append(("request", bot_id))
        return False


class _Context:
    def __init__(self):
        self.loaded = []
        self.appended = []

    async def load_context(self, *args):
        self.loaded.append(args)
        return []

    async def append_context(self, *args):
        self.appended.append(args)


class _LLM:
    def __init__(self):
        self.calls = []
        self.vision_calls = []

    async def chat_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield ("fake LLM reply", True)

    async def describe_image_file(self, local_path):
        self.vision_calls.append(local_path)
        return "fake image description"


class _ImageCache:
    def __init__(self):
        self.calls = []

    async def get_or_describe(self, ref, vision_callback):
        self.calls.append(ref)
        return ("fake-path", await vision_callback(ref, "fake-path"))

    async def peek_description(self, ref):
        return "cached fake description"


class _Handler(MessageHandler):
    def __init__(self, trace, **kwargs):
        self.trace = trace
        super().__init__(**kwargs)

    async def _archive_event(self, bot_id, event, raw):
        await super()._archive_event(bot_id, event, raw)
        self.trace.append(("archive", bot_id, event.message_id))

    async def _try_merged_group_reply(self, bot_id, event, raw):
        self.trace.append(("merged", bot_id))
        return await super()._try_merged_group_reply(bot_id, event, raw)

    async def _check_image_rate_limit(self, bot_id, event, raw):
        self.trace.append(("image_rate", bot_id))
        return await super()._check_image_rate_limit(bot_id, event, raw)

    async def _normalize_image_segments(self, bot_id, event):
        self.trace.append(("normalize", bot_id))
        return await super()._normalize_image_segments(bot_id, event)


class BanPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ban_pipeline_")
        self.trace = []
        self.config = GlobalConfig(
            admins=[1001], data_dir=self.tmp.name,
            reply=ReplyConfig(segment_reply=False, reply_quote=False),
            group_recent_msgs_count=4,
        )
        self.store = _Store(self.tmp.name, self.trace)
        self.ban = _Ban(self.store, self.trace)
        self.ws = _WS(self.trace)
        self.plugins = _Plugins(self.trace)
        self.ctx = _Context()
        self.llm = _LLM()
        self.image_cache = _ImageCache()
        self.handler = _Handler(
            self.trace, ws_server=self.ws, context_manager=self.ctx,
            llm_service=self.llm, plugin_system=self.plugins,
            data_dir=self.tmp.name, global_config=self.config,
            reply_config=self.config.reply, image_cache=self.image_cache,
        )
        self.command = CommandHandler(
            self.ctx, self.llm, self.ws, plugin_system=self.plugins, admins=[1001],
        )
        self.handler.set_interceptors([self.ban, self.plugins, self.command])
        self.next_mid = 0

    async def asyncTearDown(self):
        await self.handler.close()
        self.tmp.cleanup()

    def message(self, message, *, private=False, user_id=9999, message_id=None):
        self.next_mid += 1
        raw = {
            "time": 1000, "self_id": 11, "post_type": "message",
            "message_type": "private" if private else "group",
            "message_id": self.next_mid if message_id is None else message_id,
            "user_id": user_id, "sender": {"user_id": user_id, "nickname": "fake user"},
            "message": copy.deepcopy(message),
        }
        if not private:
            raw["group_id"] = 2001
        return Event.from_dict(copy.deepcopy(raw)), raw

    async def dispatch(self, message, *, bot_id="bot_001", **kwargs):
        event, raw = self.message(message, **kwargs)
        await self.handler.handle_event(bot_id, event, raw)
        return event, raw

    def archive(self, *, private=False, bot_id="bot_001", user_id=9999, legacy=False):
        root = Path(self.tmp.name) / "history"
        if private:
            path = root / bot_id / "private" / f"{user_id}.jsonl"
        elif legacy:
            path = root / bot_id / "group" / "2001.jsonl"
        else:
            path = root / "group" / "2001.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def assert_no_downstream(self):
        blocked = {"note_group", "perception", "merged", "observe", "plugin_intercept",
                   "image_rate", "normalize", "ban_command"}
        self.assertFalse(blocked.intersection(item[0] for item in self.trace), self.trace)
        self.assertEqual(self.handler._group_recent_msgs, {})
        self.assertEqual(self.handler._perception_text, {})
        self.assertEqual(self.handler._repeat_state, {})
        self.assertEqual(self.handler._last_image_time, {})
        self.assertEqual(self.ws.api_calls, [])
        self.assertEqual(self.ws.sent, [])
        self.assertEqual(self.ws.forwards, [])
        self.assertEqual(self.ctx.loaded, [])
        self.assertEqual(self.ctx.appended, [])
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.llm.vision_calls, [])
        self.assertEqual(self.image_cache.calls, [])

    async def test_banned_text_image_at_image_and_empty_are_archived_first(self):
        await self.store.upsert("ban-all", "9999")
        self.plugins.consume_observed = True  # 未修复时观察钩子会抢先消费。
        image = {"type": "image", "data": {"file": "fake.png"}}
        at = {"type": "at", "data": {"qq": "11"}}
        cases = [
            (False, "hello"), (False, "/help"), (False, "ping"),
            (False, [{"type": "text", "data": {"text": "hello"}}]),
            (False, [at, {"type": "text", "data": {"text": "hello"}}]),
            (False, [image]), (False, [at, image]), (False, []),
            (True, "hello"), (True, [image]), (True, []),
            (True, [{"type": "text", "data": {"text": "hello"}}]),
            (True, "[自动回复] fake reply"),
        ]
        for private, message in cases:
            with self.subTest(private=private, message=message):
                self.trace.clear()
                self.store.checks.clear()
                event, raw = await self.dispatch(message, private=private)
                self.assertEqual(self.trace[0][0], "archive")
                self.assertEqual(self.trace[1][0], "ban_check")
                self.assertEqual(len(self.store.checks), 1)
                self.assert_no_downstream()
                row = self.archive(private=private)[-1]
                self.assertEqual(row["message_id"], event.message_id)
                self.assertEqual(row["message"], raw["message"])
                if not private:
                    self.assertEqual(self.archive(legacy=True)[-1], raw)

    async def test_banned_merged_triggers_never_collect_other_bot_replies(self):
        await self.store.upsert("ban", "9999", session_key="group:2001")
        for text in ("赞我", "/好感度"):
            with self.subTest(text=text):
                self.trace.clear()
                await self.dispatch(text, bot_id="bot_002")
                self.assert_no_downstream()
        self.assertEqual(len(self.archive()), 2)

    async def test_standalone_interceptor_checks_messages_without_text(self):
        await self.store.upsert("ban-all", "9999")
        for message in ([], [{"type": "image", "data": {"file": "fake.png"}}]):
            event, raw = self.message(message, private=True)
            self.assertEqual(await self.ban.intercept("bot_001", event, raw), (True, None))

    async def test_unbanned_message_keeps_observation_and_original_chain_order(self):
        trace = self.trace

        class Probe:
            async def intercept(self, bot_id, event, raw):
                trace.append(("chain_probe", bot_id))
                return (False, None)

        self.handler.set_interceptors([self.ban, Probe(), self.plugins, self.command])
        await self.dispatch("normal text", private=True)
        stages = [item[0] for item in self.trace]
        self.assertLess(stages.index("archive"), stages.index("ban_check"))
        self.assertLess(stages.index("observe"), stages.index("chain_probe"))
        self.assertLess(stages.index("chain_probe"), stages.index("plugin_intercept"))
        self.assertEqual(len(self.store.checks), 1, "后段拦截链不应重复查询名单")
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(len(self.ctx.appended), 1)
        self.assertEqual(self.ws.sent[0][3], "fake LLM reply")

    async def test_unbanned_images_use_fake_api_vision_and_keep_raw_archive(self):
        image = {"type": "image", "data": {"file": "fake.png"}}
        for private, message in (
            (True, [image]),
            (False, [{"type": "at", "data": {"qq": "11"}}, image]),
        ):
            with self.subTest(private=private):
                event, raw = await self.dispatch(message, private=private)
                self.assertTrue(event.message[-1]["data"]["url"].startswith("data:"))
                self.assertEqual(self.archive(private=private)[-1]["message"], raw["message"])
                self.assertNotIn("url", raw["message"][-1]["data"])
        # 私聊归一化 1 次; 群聊归一化 + 最近消息图片解析各 1 次。
        self.assertEqual(len(self.ws.api_calls), 3)
        self.assertTrue(all(call[1] == "get_image" for call in self.ws.api_calls))
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(len(self.llm.vision_calls), 1)  # 群最近消息图片经 fake cache 识别。
        self.assertEqual(len(self.ctx.appended), 2)
        self.assertEqual(len(self.store.checks), 2)

    async def test_unbanned_observer_and_merged_reply_still_work(self):
        self.plugins.consume_observed = True
        await self.dispatch("observer text")  # 无 @ 群消息依然允许观察钩子消费。
        self.assertEqual(self.ws.sent[0][3], "observer reply")
        self.plugins.consume_observed = False
        await self.dispatch("赞我", bot_id="bot_002")
        self.assertEqual(len(self.ws.forwards), 1)
        nodes = self.ws.forwards[0][2]
        self.assertEqual([n["data"]["user_id"] for n in nodes], ["11", "22", "33"])
        self.assertEqual(self.llm.calls, [])

    async def test_session_pass_and_disabled_bans_are_respected(self):
        await self.store.upsert("ban", "9999", session_key="group:2001")
        await self.dispatch("private is not session-banned", private=True)
        await self.store.upsert("pass", "9999", session_key="group:2001")
        await self.dispatch("ping")
        self.assertEqual(self.ws.sent[-1][3], "PONG")
        await self.store.upsert("ban-all", "8888")
        self.ban.sync_config(enabled=False)
        before = len(self.store.checks)
        await self.dispatch("disabled ban", private=True, user_id=8888)
        self.assertEqual(len(self.store.checks), before)
        self.assertEqual(len(self.llm.calls), 2)

    async def test_banned_admin_management_commands_bypass_observer_and_merge(self):
        await self.store.upsert("ban-all", "1001")
        self.plugins.consume_observed = True
        # 即使合并触发词将来与封禁命令重合, 也不能抢先执行。
        self.handler._MERGED_GROUP_TRIGGERS = ("/ban", "/pass")
        commands = (
            ("/ban 222 1h reason", "已封禁 222"),
            ("/pass 222 1h", "已临时解禁 222"),
            ("/ban-disable", "临时禁用"),
            ("/ban-enable", "临时启用"),
            ("/ban-reset 222", "已清除用户 222"),
        )
        for text, reply in commands:
            with self.subTest(text=text):
                await self.dispatch(text, private=True, user_id=1001)
                self.assertIn(reply, self.ws.sent[-1][3])
        stages = [item[0] for item in self.trace]
        self.assertFalse({"perception", "observe", "merged", "normalize"}.intersection(stages))
        self.assertEqual(len(self.ban.commands), len(commands))
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.store.checks, [])
        self.assertEqual(len(self.archive(private=True, user_id=1001)), len(commands))

    async def test_management_permission_denial_keeps_original_semantics(self):
        self.plugins.consume_observed = True
        for banned in (False, True):
            if banned:
                await self.store.upsert("ban-all", "9999")
            before = len(self.store.mutations)
            await self.dispatch("/ban 222 1h", private=True)
            self.assertIn("没有权限", self.ws.sent[-1][3])
            self.assertEqual(len(self.store.mutations), before)
        self.assertEqual(self.ban.commands, [])
        self.assertEqual(self.store.checks, [])
        self.assertNotIn("observe", [item[0] for item in self.trace])

    async def test_public_banlist_and_help_work_even_for_banned_non_admin(self):
        await self.store.upsert("ban-all", "9999")
        self.plugins.consume_observed = True
        for text, expected in (("/banlist", "全局封禁"), ("/ban-help", "封禁系统使用指南")):
            await self.dispatch(text, private=True)
            self.assertIn(expected, self.ws.sent[-1][3])
        self.assertEqual(len(self.ban.commands), 2)
        self.assertEqual(self.store.checks, [])
        self.assertNotIn("observe", [item[0] for item in self.trace])

    async def test_multi_bot_ban_command_executes_once_after_dedup(self):
        await self.store.upsert("ban-all", "1001")
        self.store.mutations.clear()
        self.plugins.consume_observed = True
        # 默认前缀、大小写/空格和自定义前缀都要由同一选中 bot 执行。
        for prefix, text in (("/", "/ban 222 1h"), ("/", "/ BAN 333 1h"), ("!", "!ban 444 1h")):
            with self.subTest(text=text):
                self.ban._command_prefix = prefix
                self.handler._MERGED_GROUP_TRIGGERS = (prefix + "ban",)
                self.trace.clear()
                before = len(self.ban.commands)
                mid = 100 + before
                await asyncio.gather(*(
                    self.dispatch(text, bot_id=bot, user_id=1001, message_id=mid)
                    for bot in ("bot_003", "bot_001", "bot_002")
                ))
                self.assertEqual(len(self.ban.commands), before + 1)
                self.assertEqual(self.ban.commands[-1], ("bot_002", "ban"))
                stages = [item[0] for item in self.trace]
                self.assertLess(stages.index("pick_bot"), stages.index("ban_command"))
                self.assertFalse({"observe", "merged", "perception", "normalize"}.intersection(stages))
                self.assertEqual(sum(row["message_id"] == mid for row in self.archive()), 1)
                for bot in ("bot_001", "bot_002", "bot_003"):
                    self.assertEqual(self.archive(bot_id=bot, legacy=True)[-1]["message_id"], mid)
        self.assertEqual(len(self.store.mutations), 3)
        self.assertEqual(len(self.ws.sent), 3)
        self.assertTrue(all(row[1] == "bot_002" for row in self.ws.sent))
        self.assertEqual(self.store.checks, [])

    async def test_notice_and_request_semantics_are_unchanged(self):
        await self.store.upsert("ban-all", "9999")
        for raw in (
            {"time": 1000, "self_id": 11, "post_type": "notice", "notice_type": "notify",
             "sub_type": "poke", "user_id": 9999, "target_id": 11},
            {"time": 1000, "self_id": 11, "post_type": "request", "request_type": "friend",
             "user_id": 9999, "flag": "fake flag"},
        ):
            await self.handler.handle_event("bot_001", Event.from_dict(raw), raw)
        self.assertEqual(len(self.ws.sent), 1, "戳回复仍执行; 未消费申请不自动批准/拒绝")
        self.assertEqual(self.ws.api_calls, [])
        self.assertEqual(self.store.checks, [])
        self.assertIn("notice", [item[0] for item in self.trace])
        self.assertIn("request", [item[0] for item in self.trace])
        self.assertFalse((Path(self.tmp.name) / "history").exists())

    async def test_sync_config_preserves_shared_identity_and_refreshes_copies(self):
        shared = self.config
        self.ws.shared_config = self.llm.shared_config = shared
        buffers = self.handler._group_recent_msgs
        long_buffer = [{"content": i} for i in range(6)]
        buffers.update({"bot_001:2001": long_buffer, "bot_002:2001": [{"content": 9}]})
        updated = GlobalConfig(
            admins=[7777], ban=BanConfig(enabled=False), group_recent_msgs_count=2,
            ignore_auto_reply=False, llm_excluded_groups=[2001], history_dual_write=False,
            reply=ReplyConfig(
                stream=False, segment_reply=True, segment_min_len=20, segment_max_len=100,
                segment_delay_min=0.1, segment_delay_max=0.9, reply_quote=True,
            ),
        )
        self.handler.sync_config(updated)
        self.assertIs(self.handler._global_config, shared)
        self.assertIs(self.ws.shared_config, shared)
        self.assertIs(self.llm.shared_config, shared)
        for attr, value in (
            ("_stream", False), ("_segment_reply", True), ("_seg_min_len", 20),
            ("_seg_max_len", 100), ("_seg_delay_min", 0.1), ("_seg_delay_max", 0.9),
            ("_reply_quote", True), ("_group_recent_count", 2),
        ):
            self.assertEqual(getattr(self.handler, attr), value)
        self.assertIs(self.handler._group_recent_msgs, buffers)
        self.assertIs(buffers["bot_001:2001"], long_buffer)
        self.assertEqual(long_buffer, [{"content": 4}, {"content": 5}])
        self.assertEqual(len(buffers["bot_002:2001"]), 1)
        self.assertEqual(self.command._admins, {"7777"})
        self.assertTrue(self.command._is_admin(SimpleNamespace(user_id=7777)))
        self.assertFalse(self.command._is_admin(SimpleNamespace(user_id=1001)))
        self.assertTrue(self.ban.is_admin(7777))
        self.assertFalse(self.ban.is_admin(1001))
        self.assertFalse(self.ban._enabled)
        self.assertFalse(self.handler._ignore_auto_reply_enabled())
        self.assertTrue(self.handler._group_llm_excluded(2001))
        self.assertFalse(shared.history_dual_write)
        # main 已更新共享对象再同步的场景也必须刷新复制字段和缓冲。
        shared.reply.stream = True
        shared.admins = []
        shared.group_recent_msgs_count = 0
        self.handler.sync_config(shared)
        self.assertTrue(self.handler._stream)
        self.assertEqual(self.command._admins, set())
        self.assertFalse(self.ban.is_admin(7777))
        self.assertIs(self.handler._group_recent_msgs, buffers)
        self.assertEqual(buffers, {})
        shared.group_recent_msgs_count = -1
        self.handler.sync_config(shared)
        self.assertEqual(self.handler._group_recent_count, 0)

    async def test_new_minimal_stubs_without_new_attributes_remain_compatible(self):
        await self.store.upsert("ban-all", "9999")
        stub = MessageHandler.__new__(MessageHandler)
        stub._interceptors = [self.ban]  # 没有 _ban_interceptor / _ws / 其他下游属性。
        event, raw = self.message([])
        await stub._handle_message("bot_001", event, raw)
        self.assertEqual(self.store.checks, [("group:2001", "9999")])
        stub = MessageHandler.__new__(MessageHandler)
        self.assertIsNone(stub._get_ban_interceptor())
        stub.sync_config(GlobalConfig(group_recent_msgs_count=0))
        self.assertEqual(stub._group_recent_count, 0)
        self.assertTrue(stub._stream)


def main() -> None:
    """供项目测试发现器调用, 完整运行本模块的 unittest TestCase。"""
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("Ban pipeline tests failed")


if __name__ == "__main__":
    main()
