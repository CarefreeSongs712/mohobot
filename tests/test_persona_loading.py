"""Offline loading/cache regressions; all data is synthetic and temporary."""
import asyncio
import copy
import io
import json
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotManager
from mohobot.context_manager import ContextManager
from mohobot.persona_service import (
    DEFAULT_PERSONA_ID, PersonaInUseError, PersonaService,
)


async def _setup(root):
    bots = BotManager(str(root))
    contexts = ContextManager(str(root), trim_at_rounds=1000)
    service = PersonaService(root, bots, contexts)
    await service.startup()
    bot = bots.create_bot("loading-test")
    return bots, contexts, service, bot


def _write_index(root, bot_id, user_id, persona_id="", *, legacy=False):
    path = root / "contexts" / bot_id / "private" / str(user_id) / "session_index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    sessions = [{"id": "sess_main", "name": "active"},
                {"id": "sess_001", "name": "inactive"}]
    if not legacy:
        for session in sessions:
            session.update(generation="a" * 32, persona_id="")
        sessions[1]["persona_id"] = persona_id
    path.write_text(json.dumps({"sessions": sessions, "active": "sess_main"},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _snapshot(root):
    return {path.relative_to(root): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _record(records, persona_id):
    return next(record for record in records if record["id"] == persona_id)


@contextmanager
def _raises(error_type):
    try:
        yield
    except error_type:
        pass
    else:
        raise AssertionError(f"Expected {error_type.__name__}")


@contextmanager
def _no_options_io(bots, contexts, service):
    """Any storage access, reference scan or lock lookup is a regression."""
    with ExitStack() as stack:
        for method in ("open", "read_text", "read_bytes", "glob", "rglob",
                       "iterdir", "exists", "is_file", "stat"):
            stack.enter_context(patch.object(Path, method, side_effect=AssertionError("options did filesystem I/O")))
        stack.enter_context(patch("builtins.open", side_effect=AssertionError("options opened a file")))
        stack.enter_context(patch.object(io, "open", side_effect=AssertionError("options opened a file")))
        for owner, method in ((bots, "list_bot_configs"), (bots, "load_bot_config"),
                              (contexts, "read_existing_session_index"),
                              (service, "_scan_references")):
            stack.enter_context(patch.object(owner, method, side_effect=AssertionError("options consulted storage")))
        stack.enter_context(patch("mohobot.persona_service._get_lock", side_effect=AssertionError("options looked up a lock")))
        stack.enter_context(patch("mohobot.persona_service.asyncio.to_thread", side_effect=AssertionError("options dispatched I/O")))
        yield


async def test_options_return_while_both_service_and_maintenance_locks_are_held():
    with tempfile.TemporaryDirectory(prefix="persona-loading-") as td:
        root = Path(td)
        bots, contexts, service, bot = await _setup(root)
        persona = await service.create_persona("choice", "prompt")
        path = _write_index(root, bot.bot_id, "1001")
        path.write_text("{broken index", encoding="utf-8")
        async with service.lock, contexts.maintenance_lock:
            with _no_options_io(bots, contexts, service):
                options = await asyncio.wait_for(service.list_persona_options(), 0.05)
                concurrent = await asyncio.wait_for(asyncio.gather(
                    *[service.list_persona_options() for _ in range(8)]), 0.1)
        assert all(result == options for result in concurrent)
        assert _record(options, persona["id"]) == service.get_persona(persona["id"])
        assert all("reference_count" not in item for item in options)
        options[0]["content"] = "caller mutation"
        options.pop()
        concurrent[0][0]["name"] = "another caller mutation"
        fresh = await service.list_persona_options()
        assert len(fresh) == 2
        assert _record(fresh, DEFAULT_PERSONA_ID)["content"] != "caller mutation"
        assert _record(fresh, DEFAULT_PERSONA_ID)["name"] != "another caller mutation"
        assert path.read_text(encoding="utf-8") == "{broken index"


async def test_thousand_legacy_and_modern_indices_are_readonly_batched_and_nonblocking():
    with tempfile.TemporaryDirectory(prefix="persona-loading-1000-") as td:
        root = Path(td)
        bots, contexts, service, bot = await _setup(root)
        persona = await service.create_persona("large", "prompt")
        for number in range(1000):
            # Orphaned bot contexts and inactive sessions are still references.
            owner = "bot_orphan" if number % 3 == 0 else bot.bot_id
            _write_index(root, owner, 10000 + number, persona["id"], legacy=number < 500)
        before = _snapshot(root)
        real_to_thread = asyncio.to_thread
        with patch.object(contexts, "read_existing_session_index", side_effect=AssertionError("list used upgrading ContextManager getter")), \
                patch.object(service, "_scan_references", wraps=service._scan_references) as scan, \
                patch("mohobot.persona_service.asyncio.to_thread", wraps=real_to_thread) as dispatch:
            started = time.perf_counter()
            records = await asyncio.wait_for(service.list_personas(), 3)
            elapsed = time.perf_counter() - started
            print(f"1000 synthetic indices (500 legacy + 500 modern): first list {elapsed:.4f}s")
            assert _record(records, persona["id"])["reference_count"] == 500
            assert _record(records, DEFAULT_PERSONA_ID)["reference_count"] == 1
            references = await service.list_references(persona["id"])
            assert len(references) == 500
            assert all(reference["session_id"] == "sess_001" for reference in references)
            assert any(reference["bot_id"] == "bot_orphan" for reference in references)
            assert scan.call_count == dispatch.call_count == 1
        assert _snapshot(root) == before, "listing must not upgrade legacy indices or write files"

        service.invalidate_references()
        entered, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        real_scan = service._scan_references
        heartbeats = []

        def held_worker_scan():
            assert threading.get_ident() != loop_thread, "reference scan ran on event-loop thread"
            entered.set()
            assert release.wait(1), "event-loop heartbeat could not release scanner"
            return real_scan()

        async def heartbeat():
            try:
                while not entered.is_set():
                    await asyncio.sleep(0)
                for _ in range(5):
                    heartbeats.append(time.perf_counter())
                    await asyncio.sleep(0)
                # The list holds service.lock, but dropdown choices must remain available.
                options = await asyncio.wait_for(service.list_persona_options(), 0.1)
                assert _record(options, persona["id"])["content"] == "prompt"
            finally:
                release.set()

        with patch.object(service, "_scan_references", side_effect=held_worker_scan) as scan, \
                patch.object(contexts, "read_existing_session_index", side_effect=AssertionError("list upgraded an index")):
            records, _ = await asyncio.wait_for(asyncio.gather(service.list_personas(), heartbeat()), 3)
        assert len(heartbeats) == 5 and scan.call_count == 1
        assert _record(records, persona["id"])["reference_count"] == 500
        assert _snapshot(root) == before


async def test_sequential_and_concurrent_lists_share_reference_scan_and_defensive_copies():
    with tempfile.TemporaryDirectory(prefix="persona-loading-") as td:
        root = Path(td)
        _, _, service, bot = await _setup(root)
        persona = await service.create_persona("cached", "prompt")
        _write_index(root, bot.bot_id, "1001", persona["id"])
        with patch.object(service, "_scan_references", wraps=service._scan_references) as scan:
            first = await service.list_personas()
            second = await service.list_personas()
            refs = await service.list_references(persona["id"])
            assert first == second and len(refs) == 1
            assert scan.call_count == 1
            first[0]["content"] = "caller changed library"
            refs[0]["persona_id"] = DEFAULT_PERSONA_ID
            refs[0]["name"] = "caller changed reference"
            assert (await service.list_references(persona["id"]))[0]["name"] == "inactive"
            assert (await service.list_personas())[0]["content"] != "caller changed library"
            service.invalidate_references()
            simultaneous = await asyncio.wait_for(asyncio.gather(
                *[service.list_personas() for _ in range(10)],
                *[service.list_references(persona["id"]) for _ in range(10)]), 3)
            assert scan.call_count == 2, "cold concurrent list/ref calls must share one scan"
            assert all(records == second for records in simultaneous[:10])
            assert all(len(records) == 1 for records in simultaneous[10:])


async def test_reference_cache_expires_after_thirty_seconds():
    with tempfile.TemporaryDirectory(prefix="persona-loading-") as td:
        root = Path(td)
        _, _, service, bot = await _setup(root)
        persona = await service.create_persona("ttl", "prompt")
        # Patch only the service clock, not asyncio's timeout clock.
        with patch("mohobot.persona_service.time") as clock, \
                patch.object(service, "_scan_references", wraps=service._scan_references) as scan:
            clock.monotonic.return_value = 1000.0
            assert _record(await service.list_personas(), persona["id"])["reference_count"] == 0
            _write_index(root, bot.bot_id, "1001", persona["id"])
            clock.monotonic.return_value = 1029.9
            assert await service.list_references(persona["id"]) == []
            assert scan.call_count == 1
            clock.monotonic.return_value = 1030.1
            assert _record(await service.list_personas(), persona["id"])["reference_count"] == 1
            assert scan.call_count == 2
            assert len(await service.list_references(persona["id"])) == 1
            assert scan.call_count == 2


async def test_library_mutations_and_publish_invalidate_reference_cache():
    with tempfile.TemporaryDirectory(prefix="persona-loading-") as td:
        root = Path(td)
        _, _, service, bot = await _setup(root)
        persona = await service.create_persona("existing", "prompt")
        with patch.object(service, "_scan_references", wraps=service._scan_references) as scan:
            await service.list_personas()
            assert scan.call_count == 1
            created = await service.create_persona("created", "new prompt")
            options = await service.list_persona_options()
            assert _record(options, created["id"])["content"] == "new prompt"
            await service.list_personas()
            assert scan.call_count == 2
            await service.update_persona(persona["id"], "edited", "edited prompt")
            assert _record(await service.list_persona_options(), persona["id"])["content"] == "edited prompt"
            await service.list_personas()
            assert scan.call_count == 3
            # A publish invalidates even when only content changed, not a binding.
            async with service.lock:
                await service._publish(copy.deepcopy(service._library))
            await service.list_personas()
            assert scan.call_count == 4
            await service.delete_persona(created["id"])
            assert scan.call_count == 5, "delete must force a real scan"
            assert all(record["id"] != created["id"] for record in await service.list_persona_options())
            await service.list_personas()
            assert scan.call_count == 6, "successful deletion invalidates its forced scan"


async def test_bot_update_bind_and_clear_invalidate_reference_cache():
    with tempfile.TemporaryDirectory(prefix="persona-loading-") as td:
        root = Path(td)
        _, contexts, service, bot = await _setup(root)
        persona = await service.create_persona("binding", "prompt")
        captured = await contexts.capture_session(bot.bot_id, "private", "1001")
        with patch.object(service, "_scan_references", wraps=service._scan_references) as scan:
            assert _record(await service.list_personas(), persona["id"])["reference_count"] == 0
            assert scan.call_count == 1
            await service.update_bot_config(bot.bot_id, {"persona_id": persona["id"]})
            assert _record(await service.list_personas(), persona["id"])["reference_count"] == 1
            assert scan.call_count == 2
            await service.update_bot_config(bot.bot_id, {"nickname": "renamed"})
            assert (await service.list_references(persona["id"]))[0]["nickname"] == "renamed"
            assert scan.call_count == 3
            await service.bind_session(bot.bot_id, "1001", captured["id"], persona["id"])
            assert _record(await service.list_personas(), persona["id"])["reference_count"] == 2
            assert scan.call_count == 4
            await service.clear_session_binding(bot.bot_id, "1001", captured["id"])
            assert _record(await service.list_personas(), persona["id"])["reference_count"] == 1
            assert scan.call_count == 5
            await service.update_bot_config(bot.bot_id, {"persona_id": DEFAULT_PERSONA_ID})
            assert await service.list_references(persona["id"]) == []
            assert scan.call_count == 6


async def test_delete_and_restore_force_scan_when_cached_zero_hides_raw_inactive_binding():
    with tempfile.TemporaryDirectory(prefix="persona-loading-") as td:
        root = Path(td)
        _, _, service, bot = await _setup(root)
        old_library = json.loads(service.path.read_text(encoding="utf-8"))
        persona = await service.create_persona("protected", "prompt")
        assert _record(await service.list_personas(), persona["id"])["reference_count"] == 0
        assert await service.list_references(persona["id"]) == []
        path = _write_index(root, "bot_orphan", "1001", persona["id"])
        before = _snapshot(root)
        with patch.object(service, "_scan_references", wraps=service._scan_references) as scan:
            try:
                await service.delete_persona(persona["id"])
            except PersonaInUseError as exc:
                assert len(exc.references) == 1
                assert exc.references[0]["session_id"] == "sess_001"
                assert exc.references[0]["bot_id"] == "bot_orphan"
            else:
                raise AssertionError("cached zero allowed deletion of raw inactive binding")
            assert scan.call_count == 1
            with _raises(ValueError):
                await service.restore_library(old_library)
            assert scan.call_count == 2, "restore validation must force its own real scan"
            async with service.lock:
                with _raises(ValueError):
                    await service.validate_restore(old_library)
            assert scan.call_count == 3
        assert service.get_persona(persona["id"]) is not None
        assert _snapshot(root) == before and path.is_file()


async def test_delete_and_restore_ignore_cached_references_removed_directly_from_disk():
    with tempfile.TemporaryDirectory(prefix="persona-loading-") as td:
        root = Path(td)
        _, _, service, bot = await _setup(root)
        persona = await service.create_persona("delete later", "prompt")
        restored_persona = await service.create_persona("restore later", "prompt")
        path = _write_index(root, bot.bot_id, "1001", persona["id"])
        assert len(await service.list_references(persona["id"])) == 1
        path.unlink()
        # The non-expired cache still contains a reference, but deletion is strict.
        assert len(await service.list_references(persona["id"])) == 1
        await service.delete_persona(persona["id"])
        assert service.get_persona(persona["id"]) is None
        path = _write_index(root, bot.bot_id, "1001", restored_persona["id"])
        assert len(await service.list_references(restored_persona["id"])) == 1
        path.unlink()
        data = json.loads(service.path.read_text(encoding="utf-8"))
        data["personas"] = [record for record in data["personas"] if record["id"] != restored_persona["id"]]
        await service.restore_library(data)
        assert service.get_persona(restored_persona["id"]) is None
        assert not path.exists(), "strict scans must never recreate deleted indices"


async def test_expired_cache_does_not_hide_new_raw_reference_from_delete():
    with tempfile.TemporaryDirectory(prefix="persona-loading-") as td:
        root = Path(td)
        _, _, service, bot = await _setup(root)
        persona = await service.create_persona("expired", "prompt")
        with patch("mohobot.persona_service.time") as clock:
            clock.monotonic.return_value = 1000.0
            assert await service.list_references(persona["id"]) == []
            _write_index(root, bot.bot_id, "1001", persona["id"])
            clock.monotonic.return_value = 1031.0
            with _raises(PersonaInUseError):
                await service.delete_persona(persona["id"])
        assert service.get_persona(persona["id"]) is not None


async def test_malformed_indices_fail_closed_without_blocking_memory_options():
    malformed = ["{", "", "null", "[]", '{"sessions":{}}',
                 '{"sessions":[null],"active":"sess_main"}',
                 '{"sessions":[{"id":[]}],"active":"sess_main"}',
                 '{"sessions":[{"id":"../escape"}],"active":"../escape"}',
                 '{"sessions":[{"id":"sess_main"},{"id":"sess_main"}],"active":"sess_main"}',
                 '{"sessions":[{"id":"sess_main","persona_id":null}],"active":"sess_main"}',
                 '{"sessions":[{"id":"sess_main","persona_id":{}}],"active":"sess_main"}',
                 '{"sessions":[{"id":"sess_main","persona_id":"not-a-persona"}],"active":"sess_main"}']
    with tempfile.TemporaryDirectory(prefix="persona-loading-bad-") as td:
        root = Path(td)
        bots, contexts, service, bot = await _setup(root)
        old_library = json.loads(service.path.read_text(encoding="utf-8"))
        persona = await service.create_persona("cannot delete blindly", "prompt")
        # Leave a cached zero: delete/restore must not trust it over corrupt storage.
        assert await service.list_references(persona["id"]) == []
        path = _write_index(root, bot.bot_id, "1001")
        for broken in malformed:
            path.write_text(broken, encoding="utf-8")
            before = _snapshot(root)
            with _raises(ValueError):
                await service.delete_persona(persona["id"])
            with _raises(ValueError):
                await service.restore_library(old_library)
            service.invalidate_references()
            with _raises(ValueError):
                await service.list_personas()
            with _raises(ValueError):
                await service.list_references(persona["id"])
            with _no_options_io(bots, contexts, service):
                options = await asyncio.wait_for(service.list_persona_options(), 0.1)
            assert _record(options, persona["id"])["content"] == "prompt"
            assert service.get_persona(persona["id"]) is not None
            assert _snapshot(root) == before, "malformed indices must never be rewritten"


async def test_cancelled_scan_keeps_maintenance_barrier_until_worker_finishes():
    with tempfile.TemporaryDirectory() as td:
        _, contexts, service, _ = await _setup(Path(td))
        entered = threading.Event()
        release = threading.Event()
        def scan():
            entered.set()
            release.wait(3)
            return []
        with patch.object(service, "_scan_references", side_effect=scan):
            task = asyncio.create_task(service.list_personas())
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(0.01)
            assert contexts.maintenance_lock.locked()
            assert service.lock.locked()
            task.cancel()
            await asyncio.sleep(0.01)
            assert contexts.maintenance_lock.locked()
            assert service.lock.locked()
            options = await asyncio.wait_for(service.list_persona_options(), 0.1)
            assert options
            release.set()
            with _raises(asyncio.CancelledError):
                await task
            assert not contexts.maintenance_lock.locked()
            assert not service.lock.locked()
            assert service._references_cache is None


async def main():
    for name, target in sorted(globals().items()):
        if name.startswith("test_") and asyncio.iscoroutinefunction(target):
            await target()
            print("PASS", name)


if __name__ == "__main__":
    asyncio.run(main())
