"""Persona-aware backup restore validates references before touching live data."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mohobot.bot_manager import BotManager
from mohobot.context_manager import ContextManager
from mohobot.persona_service import PersonaService
from mohobot.web_panel.app import WebPanel


async def setup(root):
    manager = BotManager(str(root))
    bot = manager.create_bot(nickname="test", qq=1001)
    context = ContextManager(str(root))
    service = PersonaService(root, manager, context)
    await service.startup()
    await context.capture_session(bot.bot_id, "private", "3001")
    panel = WebPanel(data_dir=str(root), config_path=str(root / "unused.yaml"),
                     password_hash=WebPanel._hash_password("test"), bot_manager=manager,
                     context_manager=context, persona_service=service)
    return bot, context, service, panel


async def test_restore_updates_cache_and_preserves_id_highwater():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as backup:
        root = Path(td)
        bot, _, service, panel = await setup(root)
        try:
            preset = await service.create_persona("角色", "旧正文")
            await service.update_bot_config(bot.bot_id, {"persona_id": preset["id"]})
            data = json.loads(service.path.read_text(encoding="utf-8"))
            data["personas"][-1]["content"] = "恢复的正文"
            extra = await service.create_persona("额外", "临时")
            await service.delete_persona(extra["id"])
            source = Path(backup) / "personas" / "personas.json"
            source.parent.mkdir()
            source.write_text(json.dumps(data), encoding="utf-8")
            assert await panel._restore_persona_data(Path(backup), None, {"personas"}) == 1
            assert service.get_persona(preset["id"])["content"] == "恢复的正文"
            later = await service.create_persona("之后", "之后")
            assert later["id"] > extra["id"]
        finally:
            await panel.stop()


async def test_restore_rejects_missing_reference_before_overwrite():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as backup:
        root = Path(td)
        bot, _, service, panel = await setup(root)
        try:
            preset = await service.create_persona("角色", "正文")
            await service.bind_session(bot.bot_id, "3001", "sess_main", preset["id"])
            original = service.path.read_bytes()
            data = json.loads(original)
            data["personas"] = [p for p in data["personas"] if p["id"] != preset["id"]]
            source = Path(backup) / "personas" / "personas.json"
            source.parent.mkdir()
            source.write_text(json.dumps(data), encoding="utf-8")
            try:
                await panel._restore_persona_data(Path(backup), None, {"personas"})
            except ValueError as exc:
                assert "缺少" in str(exc)
            else:
                raise AssertionError("Missing referenced persona was restored")
            assert service.path.read_bytes() == original
        finally:
            await panel.stop()


async def test_context_restore_changes_generation_and_checks_binding():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as backup:
        root = Path(td)
        bot, context, service, panel = await setup(root)
        try:
            captured = await context.capture_session(bot.bot_id, "private", "3001")
            target = Path(backup) / "contexts" / bot.bot_id / "private" / "3001"
            target.mkdir(parents=True)
            index = json.loads((root / "contexts" / bot.bot_id / "private" / "3001" / "session_index.json").read_text())
            (target / "session_index.json").write_text(json.dumps(index), encoding="utf-8")
            (target / "sess_main.json").write_text('[]', encoding="utf-8")
            assert await panel._restore_persona_data(Path(backup), None, {"contexts"}) == 2
            fresh = await context.capture_session(bot.bot_id, "private", "3001")
            assert fresh["generation"] != captured["generation"]
            assert not await context.append_context(bot.bot_id, "private", "3001", [{"role":"assistant","content":"old"}],
                                                    session_id=captured["id"], generation=captured["generation"])
            index["sessions"][0]["persona_id"] = "persona_999"
            (target / "session_index.json").write_text(json.dumps(index), encoding="utf-8")
            try:
                await panel._restore_persona_data(Path(backup), None, {"contexts"})
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid binding was restored")
            assert (await context.capture_session(bot.bot_id, "private", "3001"))["generation"] == fresh["generation"]
        finally:
            await panel.stop()


async def test_joint_restore_drops_replaced_session_only_reference():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as backup:
        root = Path(td)
        bot, context, service, panel = await setup(root)
        try:
            library_before = service.path.read_text(encoding="utf-8")
            context_path = root / "contexts" / bot.bot_id / "private" / "3001"
            index_before = (context_path / "session_index.json").read_text()
            preset = await service.create_persona("以后新增", "后来正文")
            await service.bind_session(bot.bot_id, "3001", "sess_main", preset["id"])
            source = Path(backup) / "personas" / "personas.json"
            source.parent.mkdir()
            source.write_text(library_before, encoding="utf-8")
            target = Path(backup) / "contexts" / bot.bot_id / "private" / "3001"
            target.mkdir(parents=True)
            (target / "session_index.json").write_text(index_before)
            (target / "sess_main.json").write_text('[]')
            assert await panel._restore_persona_data(Path(backup), None, {"personas", "contexts"}) == 3
            assert service.get_persona(preset["id"]) is None
            assert not (await service.get_session_binding(bot.bot_id, "3001", "sess_main"))["persona_id"]
        finally:
            await panel.stop()


async def test_failed_library_publish_leaves_context_untouched():
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as backup:
        root = Path(td)
        bot, context, service, panel = await setup(root)
        try:
            await context.append_context(bot.bot_id, "private", "3001", [{"role":"user", "content":"old"}])
            live = root / "contexts" / bot.bot_id / "private" / "3001" / "sess_main.json"
            original = live.read_bytes()
            library = service.path.read_bytes()
            source = Path(backup) / "personas" / "personas.json"
            source.parent.mkdir()
            source.write_bytes(library)
            target = Path(backup) / "contexts" / bot.bot_id / "private" / "3001"
            target.mkdir(parents=True)
            (target / "session_index.json").write_bytes(live.with_name("session_index.json").read_bytes())
            (target / "sess_main.json").write_text('[]')
            with patch("mohobot.persona_service.os.replace", side_effect=OSError("test publish failure")):
                try:
                    await panel._restore_persona_data(Path(backup), None, {"personas", "contexts"})
                except OSError:
                    pass
                else:
                    raise AssertionError("Expected publish failure")
            assert live.read_bytes() == original and service.path.read_bytes() == library
        finally:
            await panel.stop()


async def test_failed_context_copy_rolls_back_library_and_context():
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as backup:
        root = Path(td)
        bot, context, service, panel = await setup(root)
        try:
            library = service.path.read_bytes()
            source = Path(backup) / "personas" / "personas.json"
            source.parent.mkdir()
            data = json.loads(library)
            data["personas"][0]["content"] = "restored default"
            source.write_text(json.dumps(data), encoding="utf-8")
            def fail_copy(*args):
                raise OSError("test copy failure")
            with patch.object(panel, "_restore_from", side_effect=fail_copy):
                try:
                    await panel._restore_persona_data(Path(backup), None, {"personas"})
                except OSError:
                    pass
                else:
                    raise AssertionError("Expected copy failure")
            assert service.path.read_bytes() == library
            assert service.get_persona("persona_001")["content"] != "restored default"
        finally:
            await panel.stop()


async def main():
    for name, target in sorted(globals().items()):
        if name.startswith("test_"):
            await target()
            print("PASS", name)

if __name__ == "__main__":
    asyncio.run(main())
