"""Offline persona/session regression tests; all writes stay in TemporaryDirectory."""
import asyncio
import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotInstance, BotManager
from mohobot.context_manager import ContextManager
from mohobot.models.config import BotConfig
from mohobot.persona_service import (
    DEFAULT_PERSONA_CONTENT, DEFAULT_PERSONA_ID, PersonaInUseError,
    PersonaNotFoundError, PersonaService,
)


class PersonaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="persona-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.bots = BotManager(self.tmp.name)
        self.contexts = ContextManager(self.tmp.name, trim_at_rounds=1000)
        self.service = PersonaService(self.tmp.name, self.bots, self.contexts)
        await self.service.startup()
        self.bot = self.bots.create_bot("Test")

    def library(self):
        return json.loads(self.service.path.read_text(encoding="utf-8"))

    async def test_default_and_crud_nonreused_ids(self):
        self.assertEqual(self.service.path, self.base / "personas/personas.json")
        self.assertEqual(self.service.get_persona(DEFAULT_PERSONA_ID)["content"], DEFAULT_PERSONA_CONTENT)
        p = await self.service.create_persona(" A ", "  prompt\n")
        self.assertEqual(p["name"], "A")
        self.assertEqual(p["content"], "  prompt\n")
        changed = await self.service.update_persona(p["id"], "B", "new")
        self.assertEqual(changed["created"], p["created"])
        await self.service.delete_persona(p["id"])
        p2 = await self.service.create_persona("C", "body")
        self.assertEqual(p2["id"], "persona_003")
        again = PersonaService(self.tmp.name, self.bots, self.contexts)
        await again.startup()
        self.assertEqual((await again.create_persona("D", "body"))["id"], "persona_004")
        p2["content"] = "mutated"
        self.assertEqual(self.service.get_persona(p2["id"])["content"], "body")

    async def test_default_cannot_delete_even_without_bot_references(self):
        self.bots._bot_config_path(self.bot.bot_id).unlink()
        with self.assertRaises(PersonaInUseError) as caught:
            await self.service.delete_persona(DEFAULT_PERSONA_ID)
        self.assertEqual(caught.exception.references, [])
        await self.service.update_persona(DEFAULT_PERSONA_ID, "editable", "edited default")
        self.assertEqual(self.service.resolve(BotConfig(persona_id=""))["content"], "edited default")

    async def test_invalid_and_missing(self):
        for name, content in (("", "ok"), ("ok", " "), (None, "ok"), ("ok", [])):
            with self.assertRaises(ValueError):
                await self.service.create_persona(name, content)
        for action in (self.service.delete_persona, self.service.list_references):
            with self.assertRaises(PersonaNotFoundError):
                await action("missing")
        with self.assertRaises(PersonaNotFoundError):
            await self.service.update_persona("missing", "A", "B")

    async def test_failed_atomic_write_keeps_disk_cache_and_counter(self):
        old_bytes = self.service.path.read_bytes()
        old_cache = self.service.get_persona(DEFAULT_PERSONA_ID)
        with patch("mohobot.persona_service.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                await self.service.update_persona(DEFAULT_PERSONA_ID, "A", "changed")
            with self.assertRaises(OSError):
                await self.service.create_persona("B", "body")
        self.assertEqual(self.service.path.read_bytes(), old_bytes)
        self.assertEqual(self.service.get_persona(DEFAULT_PERSONA_ID), old_cache)
        self.assertEqual(list(self.service.path.parent.glob("*.tmp")), [])
        self.assertEqual((await self.service.create_persona("C", "body"))["id"], "persona_002")

    async def test_interrupted_migration_reuses_preset_and_backup(self):
        path = self.bots._bot_config_path(self.bot.bot_id)
        path.write_text(json.dumps({"bot_id": self.bot.bot_id, "nickname": "old", "persona": "legacy body"}), encoding="utf-8")
        original = path.read_bytes()
        with patch.object(BotConfig, "save", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                await self.service.startup()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.library()["personas"]), 2)
        self.assertEqual(len(list(path.parent.glob("*.persona-backup-*.bak"))), 1)
        await self.service.startup()
        self.assertEqual(len(self.library()["personas"]), 2)
        self.assertEqual(len(list(path.parent.glob("*.persona-backup-*.bak"))), 1)
        self.assertEqual(self.service.get_persona(self.bots.load_bot_config(self.bot.bot_id).persona_id)["content"], "legacy body")

    async def test_migration_reuse_backup_idempotence(self):
        legacy_paths = []
        for n in range(2, 5):
            bot_id = f"bot_{n:03d}"
            path = self.bots._bot_config_path(bot_id)
            path.parent.mkdir(parents=True)
            raw = {"bot_id": bot_id, "nickname": f"legacy{n}", "persona": "same old text" if n < 4 else DEFAULT_PERSONA_CONTENT}
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            legacy_paths.append((path, path.read_bytes()))
        self.bots._bots["bot_002"] = BotInstance("bot_002", None, BotConfig(bot_id="bot_002"))
        emotion = self.base / "emotion/sentinel.json"
        emotion.parent.mkdir()
        emotion.write_text('{"history": "unchanged"}', encoding="utf-8")
        old_emotion = emotion.read_bytes()
        await self.service.startup()
        first = self.bots.load_bot_config("bot_002")
        second = self.bots.load_bot_config("bot_003")
        self.assertEqual(first.persona_id, second.persona_id)
        self.assertEqual(first.persona, "")
        self.assertEqual(self.service.get_persona(first.persona_id)["name"], "legacy2")
        self.assertEqual(self.bots.load_bot_config("bot_004").persona_id, DEFAULT_PERSONA_ID)
        self.assertEqual(self.bots.get("bot_002").config.persona_id, first.persona_id)
        for path, original in legacy_paths:
            backups = list(path.parent.glob("*.persona-backup-*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
        old_library = self.service.path.read_bytes()
        await self.service.startup()
        self.assertEqual(self.service.path.read_bytes(), old_library)
        self.assertEqual(emotion.read_bytes(), old_emotion)
        for path, _ in legacy_paths:
            self.assertEqual(len(list(path.parent.glob("*.persona-backup-*.bak"))), 1)

    async def test_legacy_constructor_default_reference_migrates_text(self):
        path = self.bots._bot_config_path(self.bot.bot_id)
        cfg = BotConfig(bot_id=self.bot.bot_id, nickname="old caller", persona="旧构造器文本")
        self.assertEqual(cfg.persona_id, DEFAULT_PERSONA_ID)
        cfg.save(path)
        original = path.read_bytes()
        await self.service.startup()
        migrated = self.bots.load_bot_config(self.bot.bot_id)
        self.assertNotEqual(migrated.persona_id, DEFAULT_PERSONA_ID)
        self.assertEqual(self.service.get_persona(migrated.persona_id)["content"], "旧构造器文本")
        backups = list(path.parent.glob("*.persona-backup-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        await self.service.startup()
        self.assertEqual(len(list(path.parent.glob("*.persona-backup-*.bak"))), 1)

    async def test_index_validation_preserves_malformed_data(self):
        path = self.base / f"contexts/{self.bot.bot_id}/private/123/session_index.json"
        path.parent.mkdir(parents=True)
        base = {"sessions": [{"id": "sess_main", "name": "main", "generation": "a" * 32, "persona_id": ""}], "active": "sess_main"}
        for key, value in (("id", []), ("persona_id", {}), ("persona_id", None), ("generation", []), ("generation", "not uuid")):
            data = copy.deepcopy(base)
            data["sessions"][0][key] = value
            path.write_text(json.dumps(data), encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                await self.contexts.read_existing_session_index(self.bot.bot_id, "private", "123")
            self.assertEqual(path.read_bytes(), original)
        data = {"sessions": [{"id": "sess_main", "name": "legacy"}], "active": "sess_main"}
        path.write_text(json.dumps(data), encoding="utf-8")
        upgraded = await self.contexts.read_existing_session_index(self.bot.bot_id, "private", "123")
        self.assertRegex(upgraded["sessions"][0]["generation"], r"^[0-9a-f]{32}$")
        self.assertEqual(upgraded["sessions"][0]["persona_id"], "")

    async def test_corrupt_library_never_overwritten(self):
        for broken in ("{", "", '{"version":1,"next_id":2,"personas":[]}'):
            self.service.path.write_text(broken, encoding="utf-8")
            with self.assertRaises(ValueError):
                await self.service.startup()
            self.assertEqual(self.service.path.read_text(encoding="utf-8"), broken)

    async def test_bot_update_offline_and_online_validation(self):
        p = await self.service.create_persona("A", "body")
        self.bots._bots[self.bot.bot_id] = BotInstance(self.bot.bot_id, None, self.bot)
        cfg = await self.service.update_bot_config(self.bot.bot_id, {"nickname": "changed", "persona_id": p["id"], "unknown": "ignored", "save": "not callable", "to_dict": "shadow"})
        self.assertTrue(callable(cfg.save) and callable(cfg.to_dict))
        self.assertEqual(cfg.nickname, "changed")
        self.assertIs(self.bots.get(self.bot.bot_id).config, cfg)
        self.assertEqual(self.bots.load_bot_config(self.bot.bot_id).persona_id, p["id"])
        for data in ({"persona": "legacy"}, {"persona_id": "missing"}, {"bot_id": "another"}, []):
            with self.assertRaises(ValueError):
                await self.service.update_bot_config(self.bot.bot_id, data)
        with self.assertRaises(ValueError):
            await self.service.update_bot_config("../outside", {})
        refs = await self.service.list_references(p["id"])
        self.assertEqual(refs[0]["type"], "bot")
        with self.assertRaises(PersonaInUseError) as caught:
            await self.service.delete_persona(p["id"])
        self.assertEqual(caught.exception.references, refs)

    async def test_session_bindings_reference_counts_and_shared_edits(self):
        p = await self.service.create_persona("A", "first")
        q = await self.service.create_persona("B", "bot body")
        await self.service.update_bot_config(self.bot.bot_id, {"persona_id": q["id"]})
        capture = await self.contexts.capture_session(self.bot.bot_id, "private", "123")
        result = await self.service.bind_session(self.bot.bot_id, "123", capture["id"], p["id"])
        self.assertEqual(result["effective"]["source"], "session")
        await self.contexts.create_session(self.bot.bot_id, "private", "123", "second")
        other = await self.contexts.capture_session(self.bot.bot_id, "private", "456")
        await self.service.bind_session(self.bot.bot_id, "456", other["id"], p["id"])
        refs = await self.service.list_references(p["id"])
        self.assertEqual(len(refs), 2)
        records = await self.service.list_personas()
        self.assertEqual(next(r for r in records if r["id"] == p["id"])["reference_count"], 2)
        with self.assertRaises(PersonaInUseError):
            await self.service.delete_persona(p["id"])
        await self.service.update_persona(p["id"], "A", "second body")
        result = await self.service.get_session_binding(self.bot.bot_id, "123", capture["id"])
        self.assertEqual(result["effective"]["content"], "second body")
        result = await self.service.clear_session_binding(self.bot.bot_id, "123", capture["id"])
        self.assertEqual(result["effective"]["id"], q["id"])
        await self.service.clear_session_binding(self.bot.bot_id, "456", other["id"])
        await self.service.delete_persona(p["id"])

    async def test_service_reads_do_not_create_or_switch_sessions(self):
        self.assertEqual(await self.service.list_user_sessions(self.bot.bot_id, "123"), {"sessions": [], "active": ""})
        for fn, args in ((self.service.get_session_binding, ("sess_main",)),
                         (self.service.bind_session, ("sess_main", DEFAULT_PERSONA_ID)),
                         (self.service.clear_session_binding, ("sess_main",))):
            with self.assertRaises(ValueError):
                await fn(self.bot.bot_id, "123", *args)
        self.assertFalse((self.base / f"contexts/{self.bot.bot_id}/private/123").exists())
        for user_id in ("../123", "a123", "123/4"):
            with self.assertRaises(ValueError):
                await self.service.list_user_sessions(self.bot.bot_id, user_id)
        with self.assertRaises(ValueError):
            await self.service.list_user_sessions("bot_missing", "123")
        await self.contexts.capture_session(self.bot.bot_id, "private", "123")
        sid = await self.contexts.create_session(self.bot.bot_id, "private", "123", "new")
        await self.service.bind_session(self.bot.bot_id, "123", "sess_main", DEFAULT_PERSONA_ID)
        self.assertEqual((await self.service.list_user_sessions(self.bot.bot_id, "123"))["active"], sid)

    async def test_capture_switch_delete_recreate_generation(self):
        sid = await self.contexts.create_session(self.bot.bot_id, "private", "123", "old")
        old = await self.contexts.capture_session(self.bot.bot_id, "private", "123")
        await self.contexts.switch_session(self.bot.bot_id, "private", "123", "sess_main")
        self.assertTrue(await self.contexts.append_context(self.bot.bot_id, "private", "123", [{"role": "user", "content": "old session"}], session_id=sid, generation=old["generation"]))
        self.assertEqual(len(await self.contexts.load_context(self.bot.bot_id, "private", "123", session_id=sid, generation=old["generation"])), 1)
        self.assertEqual(await self.contexts.load_context(self.bot.bot_id, "private", "123"), [])
        await self.contexts.delete_session(self.bot.bot_id, "private", "123", sid)
        self.assertFalse(await self.contexts.append_context(self.bot.bot_id, "private", "123", [], session_id=sid, generation=old["generation"]))
        path = self.base / f"contexts/{self.bot.bot_id}/private/123/{sid}.json"
        self.assertFalse(path.exists())
        self.assertEqual(await self.contexts.create_session(self.bot.bot_id, "private", "123", "reborn"), sid)
        new = await self.contexts.capture_session(self.bot.bot_id, "private", "123")
        self.assertNotEqual(new["generation"], old["generation"])
        self.assertFalse(await self.contexts.append_context(self.bot.bot_id, "private", "123", [{"content": "stale"}], session_id=sid, generation=old["generation"]))
        self.assertEqual(await self.contexts.load_context(self.bot.bot_id, "private", "123", session_id=sid, generation=old["generation"]), [])

    async def test_explicit_missing_never_creates_chat(self):
        self.assertEqual(await self.contexts.load_context(self.bot.bot_id, "private", "123", session_id="sess_missing"), [])
        self.assertFalse(await self.contexts.append_context(self.bot.bot_id, "private", "123", [], session_id="sess_missing"))
        self.assertFalse((self.base / "contexts").exists())

    async def test_reset_preserves_binding_and_group_metadata(self):
        capture = await self.contexts.capture_session(self.bot.bot_id, "private", "123")
        await self.service.bind_session(self.bot.bot_id, "123", capture["id"], DEFAULT_PERSONA_ID)
        await self.contexts.append_context(self.bot.bot_id, "private", "123", [{"content": "msg"}])
        self.assertTrue(await self.contexts.reset_session(self.bot.bot_id, "private", "123", capture["id"]))
        session = await self.contexts.get_session(self.bot.bot_id, "private", "123")
        self.assertEqual(session["persona_id"], DEFAULT_PERSONA_ID)
        self.assertEqual(session["generation"], capture["generation"])
        group = await self.contexts.capture_session(self.bot.bot_id, "group", "789")
        self.assertEqual(group, {"id": "main", "generation": "group-main", "persona_id": ""})
        self.assertTrue(await self.contexts.append_context(self.bot.bot_id, "group", "789", [{"content": "group"}], session_id=group["id"], generation=group["generation"]))
        self.assertEqual((await self.contexts.list_sessions(self.bot.bot_id, "group", "789"))[0]["persona_id"], "")

    async def test_concurrent_session_creation_append_and_delete(self):
        ids = await asyncio.gather(*[self.contexts.create_session(self.bot.bot_id, "private", "123", str(i)) for i in range(12)])
        self.assertEqual(len(set(ids)), 12)
        captured = await self.contexts.capture_session(self.bot.bot_id, "private", "123")
        await asyncio.gather(*[self.contexts.append_context(self.bot.bot_id, "private", "123", [{"content": str(i)}], session_id=captured["id"], generation=captured["generation"]) for i in range(20)])
        self.assertEqual(len(await self.contexts.load_context(self.bot.bot_id, "private", "123")), 20)
        await asyncio.gather(self.contexts.delete_session(self.bot.bot_id, "private", "123", captured["id"]),
                             *[self.contexts.append_context(self.bot.bot_id, "private", "123", [{"content": "late"}], session_id=captured["id"], generation=captured["generation"]) for _ in range(10)])
        self.assertFalse((self.base / f"contexts/{self.bot.bot_id}/private/123/{captured['id']}.json").exists())

    async def test_delete_during_summarizer_never_revives(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def summarize(entries):
            entered.set()
            await finish.wait()
            return "summary"
        self.contexts.set_summarizer(summarize)
        self.contexts.set_trim_config(at_rounds=2, remove_rounds=1)
        sid = await self.contexts.create_session(self.bot.bot_id, "private", "123", "old")
        capture = await self.contexts.capture_session(self.bot.bot_id, "private", "123")
        entries = [{"role": "user", "content": str(i)} for i in range(4)]
        task = asyncio.create_task(self.contexts.append_context(self.bot.bot_id, "private", "123", entries, session_id=sid, generation=capture["generation"]))
        await asyncio.wait_for(entered.wait(), 2)
        await self.contexts.delete_session(self.bot.bot_id, "private", "123", sid)
        await self.contexts.create_session(self.bot.bot_id, "private", "123", "new")
        finish.set()
        await task
        self.assertEqual(await self.contexts.load_context(self.bot.bot_id, "private", "123"), [])

    async def test_fallback_warnings_and_priority(self):
        p = await self.service.create_persona("A", "body")
        cfg = BotConfig(persona_id=p["id"], persona="never used")
        self.assertEqual(self.service.resolve(cfg, "missing")["source"], "bot")
        self.assertIn("warnings", self.service.resolve(cfg, "missing"))
        cfg.persona_id = "also-missing"
        result = self.service.resolve(cfg, "missing")
        self.assertEqual(result["source"], "default")
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(result["content"], DEFAULT_PERSONA_CONTENT)

    async def test_restore_validation_references_highwater_cache(self):
        old = self.library()
        p = await self.service.create_persona("A", "body")
        await self.service.update_bot_config(self.bot.bot_id, {"persona_id": p["id"]})
        previous = self.service.path.read_bytes()
        with self.assertRaises(ValueError):
            await self.service.restore_library(old)
        self.assertEqual(self.service.path.read_bytes(), previous)
        await self.service.update_bot_config(self.bot.bot_id, {"persona_id": DEFAULT_PERSONA_ID})
        await self.service.restore_library(old)
        self.assertIsNone(self.service.get_persona(p["id"]))
        self.assertEqual((await self.service.create_persona("B", "new"))["id"], "persona_003")
        current = self.library()
        current["personas"][0]["content"] = "restored default"
        async with self.service.lock:
            validated = await self.service.validate_restore(current)
            await self.service.restore_library_locked(validated)
        self.assertEqual(self.service.resolve(BotConfig())["content"], "restored default")
        with self.assertRaises(ValueError):
            await self.service.restore_library(current, extra_persona_ids=["persona_999"])
        for transform in (lambda d: d["personas"].append(copy.deepcopy(d["personas"][0])),
                          lambda d: d.update(next_id=1),
                          lambda d: d["personas"].pop(0),
                          lambda d: d["personas"][0].update(created="bad")):
            invalid = copy.deepcopy(current)
            transform(invalid)
            with self.assertRaises(ValueError):
                await self.service.restore_library(invalid)

    async def test_restore_rejects_inactive_session_reference(self):
        old = self.library()
        p = await self.service.create_persona("A", "body")
        session = await self.contexts.capture_session(self.bot.bot_id, "private", "123")
        await self.service.bind_session(self.bot.bot_id, "123", session["id"], p["id"])
        await self.contexts.create_session(self.bot.bot_id, "private", "123", "active")
        with self.assertRaises(ValueError):
            await self.service.restore_library(old)

    async def test_bind_delete_race_is_serialized(self):
        session = await self.contexts.capture_session(self.bot.bot_id, "private", "123")
        p = await self.service.create_persona("A", "body")
        results = await asyncio.gather(
            self.service.bind_session(self.bot.bot_id, "123", session["id"], p["id"]),
            self.service.delete_persona(p["id"]), return_exceptions=True)
        binding = await self.service.get_session_binding(self.bot.bot_id, "123", session["id"])
        if binding["persona_id"]:
            self.assertIsNotNone(self.service.get_persona(binding["persona_id"]))
            self.assertIsInstance(results[1], PersonaInUseError)
        else:
            self.assertIsInstance(results[0], PersonaNotFoundError)


def test_persona_service_suite():
    """Keep unittest coverage visible to the project's function-based runner."""
    import threading
    results = []
    def run():
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(PersonaServiceTests)
        results.append(unittest.TextTestRunner(verbosity=0).run(suite))
    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert results and results[0].wasSuccessful(), "人设服务回归失败"


if __name__ == "__main__":
    unittest.main(verbosity=2)
