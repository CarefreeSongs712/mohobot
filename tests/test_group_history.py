"""Shared group history: concurrent writes, provenance, restart and readers."""

import asyncio
import copy
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins/chat_manager"))

from mohobot.history import archive_event, group_writer, history_path
from mohobot.message_handler import MessageHandler
from mohobot.models.onebot import Event
from mohobot.ws_server import WSServer
from review.loader import MohobotData
from chat_manager_core.records import read_recent
from chat_manager_core.target import Target, resolve_by_history


def _event(mid, *, uid=123, self_id=111, group=999, message=None):
    return {
        "post_type": "message", "message_type": "group", "group_id": group,
        "message_id": mid, "time": 100, "self_id": self_id, "user_id": uid,
        "sender": {"user_id": uid, "nickname": str(uid)},
        "message": message or [{"type": "text", "data": {"text": "hello"}}],
    }


def _manager():
    bots = [SimpleNamespace(bot_id="bot_001", qq=111, nickname="A"),
            SimpleNamespace(bot_id="bot_002", qq=222, nickname="B")]
    return SimpleNamespace(all_bots=bots,
                           get=lambda bid: next(b for b in bots if b.bot_id == bid))


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_concurrent_incoming_outgoing_and_restart():
    with tempfile.TemporaryDirectory() as td:
        manager = _manager()
        ws = WSServer(manager, data_dir=td)
        handler = MessageHandler.__new__(MessageHandler)
        handler._data_dir, handler._ws, handler._writer_registry = td, ws, {}
        try:
            raw = _event(10)
            original = copy.deepcopy(raw)
            await asyncio.gather(*[
                handler._archive_event(bid, Event.from_dict({**raw, "self_id": qq}),
                                       {**raw, "self_id": qq})
                for bid, qq in [("bot_001", 111), ("bot_002", 222)] * 10
            ])
            assert raw == original
            path = history_path(td, "bot_001", "group", 999)
            assert len(_lines(path)) == 1
            assert not (Path(td) / "history/bot_001/group").exists()

            # An observed bot utterance wins the race with its outgoing echo.
            bot_raw = _event(11, uid=222)
            await handler._archive_event("bot_001", Event.from_dict(bot_raw), bot_raw)
            await ws._write_archive_line("bot_002", "group", 999, "11", bot_raw["message"])
            # The reverse arrival order must also be deduplicated.
            await ws._write_archive_line("bot_001", "group", 999, "12", raw["message"])
            bot_raw2 = _event(12, uid=111, self_id=222)
            await handler._archive_event("bot_002", Event.from_dict(bot_raw2), bot_raw2)
            assert len(_lines(path)) == 3
            rows = MohobotData(td).filtered_rows("_merged/group/999")
            assert [(r["mid"], r["bot"], r["kind"]) for r in rows] == [
                ("11", "bot_002", "assistant"), ("12", "bot_001", "assistant")]

            # Same ID in another group is a different message.
            other = _event(10, group=998)
            await handler._archive_event("bot_001", Event.from_dict(other), other)
            assert len(_lines(history_path(td, "bot_001", "group", 998))) == 1

            await ws.stop()
            for writer in handler._writer_registry.values():
                await writer.close()
            handler._writer_registry.clear()
            # New writer reads persisted identities, not only a live cache.
            restarted = group_writer(path)
            await restarted.append(archive_event(raw, "bot_002", manager))
            assert len(_lines(path)) == 3
            await restarted.close()
        finally:
            await ws.stop()
            for writer in handler._writer_registry.values():
                await writer.close()


async def test_missing_id_fallback_and_private_isolation():
    with tempfile.TemporaryDirectory() as td:
        path = history_path(td, "bot_001", "group", 999)
        writer = group_writer(path)
        try:
            raw = _event(None)
            await writer.append(archive_event(raw, "bot_001", _manager()))
            await writer.append(archive_event({**raw, "self_id": 222}, "bot_002", _manager()))
            await writer.append(archive_event({**raw, "user_id": 124}, "bot_002", _manager()))
            await writer.append(archive_event({**raw, "time": 101}, "bot_001", _manager()))
            assert len(_lines(path)) == 3
            handler = MessageHandler.__new__(MessageHandler)
            handler._data_dir, handler._writer_registry = td, {}
            private = {**raw, "message_type": "private"}
            for bid in ("bot_001", "bot_002"):
                await handler._archive_event(bid, Event.from_dict(private), private)
                assert _lines(history_path(td, bid, "private", 123)) == [private]
            for private_writer in handler._writer_registry.values():
                await private_writer.close()
        finally:
            await writer.close()


async def test_shared_and_legacy_readers_keep_review_identity():
    with tempfile.TemporaryDirectory() as td:
        legacy = Path(td) / "history/bot_001/group/999.jsonl"
        legacy.parent.mkdir(parents=True)
        old = {**_event(1, uid=111), "post_type": "message_sent"}
        legacy.write_text(json.dumps(old) + "\n", encoding="utf-8")
        path = history_path(td, "bot_002", "group", 999)
        writer = group_writer(path)
        try:
            # Receiver A archives first, but the message addresses B only.
            mention = _event(2, message=[{"type": "at", "data": {"qq": "222"}},
                                        {"type": "text", "data": {"text": "hi B"}}])
            quote = _event(3, message=[{"type": "reply", "data": {"id": "1"}},
                                      {"type": "text", "data": {"text": "old reply"}}])
            for raw in (mention, quote, _event(4)):
                await writer.append(archive_event(raw, "bot_001", _manager()))
            # Also tolerate an overlap between legacy and shared archives.
            await writer.append(archive_event(old, "bot_001", _manager()))
            data = MohobotData(td, Path(td) / "review/cache.json")
            rows = data.filtered_rows("_merged/group/999")
            assert {r["mid"] for r in rows} == {"1", "2", "3"}, rows
            assert next(r for r in rows if r["mid"] == "2")["bot"] == "bot_002"
            sessions = data.list_sessions(force=True)
            assert len(sessions) == 1
            assert set(sessions[0]["bots"]) == {"bot_001", "bot_002"}
            assert "_merged" not in sessions[0]["bots"]
            enriched = data.enrich_entries("_merged/group/999", {"mid:1": {"status": "normal"}}, {})
            assert next(e for e in enriched if e["message_id"] == "1")["status"] == "normal"
            data.flush_cache()
            restored = MohobotData(td, Path(td) / "review/cache.json")
            assert restored.filtered_rows("_merged/group/999") == rows

            target = resolve_by_history(td, "bot_003", "999")
            assert target == Target("group", "999")
            recent = await read_recent(td, "bot_003", target, 20)
            assert len(recent) == 4  # Full history includes ordinary group chatter.
            assert {str(e["message_id"]) for e in recent} == {"1", "2", "3", "4"}
            assert len(await read_recent(td, "bot_003", target, 2)) == 2
        finally:
            await writer.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            asyncio.run(fn())
            print(f"PASS {name}")
