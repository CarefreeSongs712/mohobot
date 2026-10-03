"""Persona 管理命令隔离测试: FakeService + 临时 data, 不访问生产数据/网络。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.interceptors.command_handler import CommandHandler
from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender


class FakeService:
    """仅实现固定服务契约; 读取临时 index, 绑定不触碰上下文/情感。"""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.calls = []
        self.bindings = {}
        self.defaults = {"bot_a": "persona_001", "bot_b": "persona_002"}
        self.personas = {
            "persona_001": {"id": "persona_001", "name": "默认甲", "content": "SECRET_A"},
            "persona_002": {"id": "persona_002", "name": "默认乙", "content": "SECRET_B"},
            "persona_003": {"id": "persona_003", "name": "会话专属", "content": "PRIVATE_SECRET"},
        }

    def _index(self, bot_id, user_id):
        if bot_id not in self.defaults:
            raise ValueError(f"bot 不存在: {bot_id}")
        if not user_id.isascii() or not user_id.isdigit():
            raise ValueError("QQ 必须为数字")
        path = self.data_dir / "contexts" / bot_id / "private" / user_id / "session_index.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sessions": [], "active": None}

    def _validate(self, bot_id, user_id, session_id):
        index = self._index(bot_id, user_id)
        if not any(s["id"] == session_id for s in index["sessions"]):
            raise ValueError(f"会话不存在: {session_id}")

    async def list_personas(self):
        self.calls.append(("list",))
        return [dict(p, reference_count=0) for p in self.personas.values()]

    async def list_user_sessions(self, bot_id, user_id):
        self.calls.append(("sessions", bot_id, user_id))
        return self._index(bot_id, user_id)

    async def bind_session(self, bot_id, user_id, session_id, persona_id):
        self.calls.append(("set", bot_id, user_id, session_id, persona_id))
        self._validate(bot_id, user_id, session_id)
        if persona_id not in self.personas:
            raise ValueError(f"预设不存在: {persona_id}")
        self.bindings[(bot_id, user_id, session_id)] = persona_id
        return {"persona_id": persona_id}

    async def clear_session_binding(self, bot_id, user_id, session_id):
        self.calls.append(("clear", bot_id, user_id, session_id))
        self._validate(bot_id, user_id, session_id)
        self.bindings.pop((bot_id, user_id, session_id), None)
        return {"persona_id": ""}

    async def get_session_binding(self, bot_id, user_id, session_id):
        self.calls.append(("get", bot_id, user_id, session_id))
        self._validate(bot_id, user_id, session_id)
        pid = self.bindings.get((bot_id, user_id, session_id), "")
        default = self.defaults[bot_id]
        return {
            "persona_id": pid,
            "bot_persona_id": default,
            "effective": dict(self.personas[pid or default], source="session" if pid else "bot"),
        }


class _UntouchableContext:
    def __getattr__(self, name):
        raise AssertionError(f"Persona 命令不应操作 context: {name}")


def _event(text, group=False, user_id=1001, role="member", segments=False):
    message = [{"type": "text", "data": {"text": text}}] if segments else text
    kwargs = dict(time=0, self_id=11, post_type="message", user_id=user_id,
                  sender=Sender(user_id=user_id, role=role), message=message)
    if group:
        return GroupMessageEvent(group_id=9001, **kwargs)
    return PrivateMessageEvent(**kwargs)


class PersonaCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for bot in ("bot_a", "bot_b"):
            folder = self.root / "contexts" / bot / "private" / "2002"
            folder.mkdir(parents=True)
            (folder / "session_index.json").write_text(json.dumps({
                "sessions": [{"id": "sess_main", "name": "默认"},
                             {"id": "sess_001", "name": "历史会话"}],
                "active": "sess_main",
            }), encoding="utf-8")
            (folder / "sess_001.json").write_text('[{"role":"user","content":"历史"}]', encoding="utf-8")
        emotion = self.root / "emotion" / "memory.json"
        emotion.parent.mkdir()
        emotion.write_text('{"affection":42,"memory":"保留"}', encoding="utf-8")
        self.service = FakeService(self.root)
        self.handler = CommandHandler(_UntouchableContext(), None, None,
                                      admins=[1001], persona_service=self.service)

    async def command(self, text, **kwargs):
        handled, reply = await self.handler.intercept("bot_a", _event(text, **kwargs), {})
        self.assertTrue(handled)
        self.assertIsInstance(reply, str)
        return reply

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    async def test_all_subcommands_require_global_admin_in_private_and_group(self):
        commands = ["list", "sessions bot_b 2002", "set bot_b 2002 sess_001 persona_003",
                    "get bot_b 2002 sess_001", "clear bot_b 2002 sess_001", "", "invalid"]
        for group in (False, True):
            for sub in commands:
                with self.subTest(group=group, sub=sub):
                    reply = await self.command("/persona " + sub, group=group,
                                               user_id=2002, role="owner")
                    self.assertIn("仅全局管理员", reply)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.service.bindings, {})

    async def test_help_marks_persona_admin_and_documents_explicit_targets(self):
        entry = next(cmd for section in self.handler._build_help_sections()
                     for cmd in section["commands"] if cmd["name"] == "persona")
        self.assertTrue(entry["admin"])
        text = self.handler._help_text()
        self.assertIn("仅全局管理员", text)
        self.assertIn("/persona get <bot_id> <QQ> <session_id>", text)

    async def test_list_names_and_ids_never_contents(self):
        for group in (False, True):
            reply = await self.command("/persona list", group=group)
            for pid, persona in self.service.personas.items():
                self.assertIn(pid, reply)
                self.assertIn(persona["name"], reply)
                self.assertNotIn(persona["content"], reply)

    async def test_cross_bot_set_uses_explicit_target_and_parses_split_tail(self):
        reply = await self.command('/PERSONA SET "bot_b" 2002 "sess_001" "persona_003"', segments=True)
        self.assertIn(("set", "bot_b", "2002", "sess_001", "persona_003"), self.service.calls)
        self.assertEqual(self.service.bindings, {("bot_b", "2002", "sess_001"): "persona_003"})
        self.assertIn("会话专属 [persona_003]", reply)
        self.assertIn("session", reply)
        self.assertIn("bot=bot_b", reply)
        self.assertNotIn("PRIVATE_SECRET", reply)

    async def test_binding_is_scoped_to_bot_user_and_session(self):
        folder = self.root / "contexts" / "bot_b" / "private" / "3003"
        folder.mkdir()
        original = self.root / "contexts" / "bot_b" / "private" / "2002" / "session_index.json"
        (folder / "session_index.json").write_bytes(original.read_bytes())
        await self.command("/persona set bot_b 2002 sess_001 persona_003")
        for target, default_name in (
            ("bot_a 2002 sess_001", "默认甲"),
            ("bot_b 3003 sess_001", "默认乙"),
            ("bot_b 2002 sess_main", "默认乙"),
        ):
            with self.subTest(target=target):
                reply = await self.command("/persona get " + target)
                self.assertIn(default_name, reply)
                self.assertIn("bot 默认 (bot)", reply)
                self.assertNotIn("会话专属", reply)

    async def test_get_and_clear_report_effective_bot_or_session_source(self):
        initial = await self.command("/persona get bot_b 2002 sess_001", group=True)
        self.assertIn("默认乙 [persona_002]", initial)
        self.assertIn("bot 默认 (bot)", initial)
        await self.command("/persona set bot_b 2002 sess_001 persona_003", group=True)
        bound = await self.command("/persona get bot_b 2002 sess_001", group=True)
        self.assertIn("会话专属 [persona_003]", bound)
        self.assertIn("私聊会话覆盖 (session)", bound)
        self.assertNotIn("PRIVATE_SECRET", bound)
        cleared = await self.command("/persona clear bot_b 2002 sess_001", group=True)
        self.assertIn("默认乙 [persona_002]", cleared)
        self.assertIn("bot 默认 (bot)", cleared)
        self.assertEqual(self.service.bindings, {})
        self.assertNotIn("SECRET_B", cleared)
        again = await self.command("/persona clear bot_b 2002 sess_001")
        self.assertIn("默认乙 [persona_002]", again)

    async def test_sessions_does_not_create_or_switch(self):
        before = self.snapshot()
        reply = await self.command("/persona sessions bot_b 2002")
        self.assertIn("当前会话: sess_main", reply)
        self.assertIn("历史会话 [sess_001]", reply)
        self.assertEqual(self.service.calls, [("sessions", "bot_b", "2002")])
        self.assertIn("暂无会话", await self.command("/persona sessions bot_b 3003"))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "contexts" / "bot_b" / "private" / "3003").exists())

    async def test_set_clear_leave_history_emotion_and_active_session_unchanged(self):
        before = self.snapshot()
        await self.command("/persona set bot_b 2002 sess_001 persona_003")
        await self.command("/persona get bot_b 2002 sess_001")
        await self.command("/persona clear bot_b 2002 sess_001")
        self.assertEqual(self.snapshot(), before)

    async def test_argument_errors_do_not_infer_targets_or_call_service(self):
        invalid = ["", "unknown", "list extra", "sessions", "sessions bot_b",
                   "sessions bot_b 2002 extra", "set persona_003", "set bot_b 2002 sess_001",
                   "set bot_b 2002 sess_001 persona_003 extra", "get sess_001", "get bot_b 2002",
                   "clear", "clear bot_b 2002 sess_001 extra", 'get "" 2002 sess_001',
                   'set bot_b 2002 "sess_001 persona_003']
        for sub in invalid:
            with self.subTest(sub=sub):
                self.assertIn("用法", await self.command("/persona " + sub))
        self.assertEqual(self.service.calls, [])

    async def test_service_validates_bot_qq_session_and_persona_ids(self):
        cases = [
            ("get missing 2002 sess_001", "bot 不存在"),
            ("sessions bot_b not-qq", "QQ 必须为数字"),
            ("set bot_b 2002 missing persona_003", "会话不存在"),
            ("set bot_b 2002 sess_001 missing", "预设不存在"),
            ("get bot_b 2002 ../outside", "会话不存在"),
            ("clear bot_b 2002 missing", "会话不存在"),
            ("clear bot_b 3003 sess_main", "会话不存在"),
        ]
        before = self.snapshot()
        for sub, expected in cases:
            with self.subTest(sub=sub):
                reply = await self.command("/persona " + sub)
                self.assertIn("人设命令失败", reply)
                self.assertIn(expected, reply)
                self.assertNotIn("命令执行出错", reply)
        self.assertEqual(self.service.bindings, {})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.service.calls), len(cases))

    async def test_service_optional_and_admin_check_precedes_availability(self):
        handler = CommandHandler(None, None, None, admins=[1001])
        _, reply = await handler.intercept("bot_a", _event("/persona list"), {})
        self.assertIn("人设服务未启用", reply)
        _, reply = await handler.intercept("bot_a", _event("/persona list", user_id=2002), {})
        self.assertIn("仅全局管理员", reply)

    async def test_empty_persona_list(self):
        self.service.personas.clear()
        self.assertIn("暂无人设预设", await self.command("/persona list"))


def test_persona_commands_suite():
    import threading
    results = []
    def run():
        results.append(unittest.TextTestRunner(verbosity=0).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(PersonaCommandTests)))
    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert results and results[0].wasSuccessful(), "人设命令回归失败"


if __name__ == "__main__":
    unittest.main()
