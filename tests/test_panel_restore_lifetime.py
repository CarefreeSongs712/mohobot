import asyncio
import io
import json
import sqlite3
import sys
import tempfile
import threading
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_panel_context_consistency import (
    _backup, _build_panel, _client, _login, _make_wal_db, _restore, _zip_bytes,
)
from mohobot.context_manager import ContextManager
from mohobot.db.sqlite_snapshot import restore_sqlite, snapshot_sqlite
from mohobot.file_store import _get_lock
from mohobot.services.history_barrier import HistoryBarrier
from mohobot.web_panel.app import _data_thread, _settle_data_task


def _database(path, name="before", *, wal=False):
    connection = _make_wal_db(path, [name])
    if not wal:
        connection.execute("PRAGMA journal_mode=DELETE")
    return connection


def _names(path):
    with closing(sqlite3.connect(path, timeout=1)) as connection:
        return [row[0] for row in connection.execute("SELECT username FROM users ORDER BY rowid")]


async def _until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.01)


def _worker(name):
    return any(task.get_name() == f"panel-data:{name}" and not task.done()
               for task in asyncio.all_tasks())


async def _cancel_repeatedly(task):
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done(), "cancelled request abandoned an active worker/compensation"


async def test_empty_private_sessions_backup_restore_closed_loop():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        context = ContextManager(str(root / "data"))
        panel, data = _build_panel(root, context_manager=context)
        try:
            # Capturing a first turn that never completes creates only metadata.
            first = await context.capture_session("bot_001", "private", "1001")
            chat = data / "contexts/bot_001/private/1001"
            assert not (chat / "sess_main.json").exists()
            # Creating another session also leaves the lazy default in the index.
            await context.create_session("bot_001", "private", "1002", "new")
            async with _client(panel) as client:
                auth = await _login(client)
                content = await _backup(client, auth, ["contexts"])
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    for user in ("1001", "1002"):
                        assert json.loads(archive.read(
                            f"contexts/bot_001/private/{user}/sess_main.json")) == []
                assert not (chat / "sess_main.json").exists(), "backup mutated live lazy state"
                result = await _restore(client, auth, content, ["contexts"])
                assert result.status_code == 200, result.text
                assert await context.load_context("bot_001", "private", "1001") == []
                assert not await context.append_context(
                    "bot_001", "private", "1001", [{"role": "assistant", "content": "late"}],
                    session_id=first["id"], generation=first["generation"])
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    damaged = _zip_bytes({name: archive.read(name) for name in archive.namelist()
                                          if name != "contexts/bot_001/private/1001/sess_main.json"})
                before = (chat / "session_index.json").read_bytes()
                result = await _restore(client, auth, damaged, ["contexts"])
                assert result.status_code == 400, result.text
                assert (chat / "session_index.json").read_bytes() == before
        finally:
            await panel.stop()


async def test_data_thread_drains_real_copies_even_after_repeated_cancellation():
    # Exercise the same drain for prepare, restore and compensating workers.
    for operation in (snapshot_sqlite, restore_sqlite):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, target = root / "source.sqlite", root / "target.sqlite"
            _database(source, "payload").close()
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            ready = asyncio.Event()
            loop = asyncio.get_running_loop()

            def copy():
                entered.set()
                loop.call_soon_threadsafe(ready.set)
                if not release.wait(8):
                    raise TimeoutError("test did not release SQLite worker")
                try:
                    operation(source, target)
                finally:
                    finished.set()

            task = asyncio.create_task(_data_thread(copy))
            try:
                await asyncio.wait_for(ready.wait(), 8)
                assert entered.is_set()
                await _cancel_repeatedly(task)
                assert source.is_file() and not finished.is_set()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 8)
            assert finished.is_set() and _names(target) == ["payload"]


async def _cancel_locked_restore(*, snapshot=False, backup=False):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        live, payload = root / "db/live.sqlite", root / "payload.sqlite"
        connection = _database(live, wal=not snapshot)
        _database(payload, "payload").close()
        context = ContextManager(str(root / "data"))
        barrier = HistoryBarrier()
        panel, _ = _build_panel(root, context_manager=context, history_barrier=barrier,
                                database=(live.parent, live.name))
        connection.execute("BEGIN EXCLUSIVE" if snapshot else "BEGIN IMMEDIATE")
        task = waiter = None
        try:
            async with _client(panel) as client:
                auth = await _login(client)
                if backup:
                    task = asyncio.create_task(_backup(client, auth, ["database"]))
                else:
                    content = _zip_bytes({"database/live.sqlite": payload.read_bytes()})
                    task = asyncio.create_task(_restore(client, auth, content, ["database", "history"]))
                await _until(lambda: _worker("snapshot_sqlite" if snapshot else "restore_sqlite")
                             or (backup and _worker("_snapshot_database_to")))
                await _cancel_repeatedly(task)
                assert panel._data_maintenance_lock.locked()
                if not backup:
                    assert context.maintenance_lock.locked() and barrier.frozen
                    assert list(Path(tempfile.gettempdir()).glob("mohobot_restore_*"))
                # Another maintenance request cannot overtake the cancelled one.
                waiter = asyncio.create_task(_backup(client, auth, ["database"]))
                await asyncio.sleep(0)
                assert not waiter.done()
                connection.rollback()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 8)
                await asyncio.wait_for(waiter, 8)
                connection.execute("INSERT INTO users(uuid, username, password) "
                                   "VALUES ('after-cancel', 'after-cancel', '')")
                connection.commit()
                assert _names(live) == ["before", "after-cancel"]
                assert not panel._data_maintenance_lock.locked()
                assert not context.maintenance_lock.locked() and not barrier.frozen
                assert not _worker("restore_sqlite") and not _worker("snapshot_sqlite")
        finally:
            connection.rollback()
            connection.close()
            for pending in (task, waiter):
                if pending is not None and not pending.done():
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        pass
            await panel.stop()


async def test_cancelled_restore_drains_sqlite_before_compensation_and_unlock():
    await _cancel_locked_restore()


async def test_cancelled_prepare_snapshot_retains_maintenance_resources():
    await _cancel_locked_restore(snapshot=True)


async def test_cancelled_backup_snapshot_retains_temporary_files_and_lock():
    await _cancel_locked_restore(snapshot=True, backup=True)


async def test_failed_joint_restore_restores_database_existence_and_files():
    for existed in (False, True):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live, payload = root / "db/live.sqlite", root / "payload.sqlite"
            if existed:
                _database(live).close()
            _database(payload, "payload").close()
            context = ContextManager(str(root / "data"))
            panel, data = _build_panel(root, context_manager=context,
                                       database=(live.parent, live.name))
            # A real filesystem obstruction, reached after database and ban copy.
            (data / "contexts").write_text("existing obstruction", encoding="utf-8")
            (data / "ban").mkdir()
            (data / "ban/original.json").write_text("old", encoding="utf-8")
            content = _zip_bytes({
                "database/live.sqlite": payload.read_bytes(), "ban/replacement.json": "new",
                "contexts/bot_001/group/1001/main.json": "[]",
            })
            try:
                async with _client(panel) as client:
                    result = await _restore(client, await _login(client), content,
                                            ["database", "ban", "contexts"])
                    assert result.status_code == 500, result.text
                assert (data / "contexts").read_text(encoding="utf-8") == "existing obstruction"
                assert (data / "ban/original.json").read_text(encoding="utf-8") == "old"
                assert not (data / "ban/replacement.json").exists()
                if existed:
                    assert _names(live) == ["before"]
                else:
                    assert not live.exists()
                    assert not any(Path(str(live) + suffix).exists()
                                   for suffix in ("-wal", "-shm", "-journal"))
                assert not context.maintenance_lock.locked()
            finally:
                await panel.stop()


async def test_repeated_cancellation_during_real_database_compensation():
    from mohobot.bot_manager import BotManager
    from mohobot.persona_service import PersonaService

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        live, payload = root / "db/live.sqlite", root / "payload.sqlite"
        connection = _database(live, wal=True)
        _database(payload, "payload").close()
        data = root / "data"
        manager = BotManager(str(data))
        context = ContextManager(str(data))
        service = PersonaService(data, manager, context)
        await service.startup()
        barrier = HistoryBarrier()
        panel, _ = _build_panel(root, context_manager=context, persona_service=service,
                                history_barrier=barrier, database=(live.parent, live.name))
        library_before = service.path.read_bytes()
        library = json.loads(library_before)
        library["personas"][0]["content"] = "replacement"
        content = _zip_bytes({"database/live.sqlite": payload.read_bytes(),
                              "personas/personas.json": json.dumps(library)})
        lock = _get_lock(str(service.path.absolute()))
        await lock.acquire()
        task = None
        try:
            async with _client(panel) as client:
                auth = await _login(client)
                task = asyncio.create_task(_restore(client, auth, content,
                                                    ["database", "personas", "history"]))
                await _until(lambda: _names(live) == ["payload"])
                # Publication is waiting on a real path lock. Block compensation
                # with an actual SQLite write transaction before cancellation.
                connection.execute("BEGIN IMMEDIATE")
                task.cancel()
                await _until(lambda: _worker("restore_sqlite"))
                await _cancel_repeatedly(task)
                assert panel._data_maintenance_lock.locked()
                assert context.maintenance_lock.locked() and service.lock.locked()
                assert barrier.frozen
                connection.rollback()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 8)
                assert _names(live) == ["before"]
                assert service.path.read_bytes() == library_before
                assert service.get_persona("persona_001")["content"] != "replacement"
                assert not service.lock.locked() and not context.maintenance_lock.locked()
                assert not barrier.frozen
        finally:
            lock.release()
            connection.rollback()
            connection.close()
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await panel.stop()


async def test_real_restore_compensation_failure_survives_cancellation():
    from mohobot.bot_manager import BotManager
    from mohobot.persona_service import PersonaService

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        live, payload = root / "db/live.sqlite", root / "payload.sqlite"
        _database(live).close()
        _database(payload, "payload").close()
        data = root / "data"
        context = ContextManager(str(data))
        service = PersonaService(data, BotManager(str(data)), context)
        await service.startup()
        panel, _ = _build_panel(root, context_manager=context, persona_service=service,
                                database=(live.parent, live.name))
        upload = root / "upload"
        (upload / "personas").mkdir(parents=True)
        (upload / "personas/personas.json").write_bytes(service.path.read_bytes())
        lock = _get_lock(str(service.path.absolute()))
        await lock.acquire()
        task = None
        try:
            # Observe the transaction boundary directly: a disconnected HTTP
            # caller cannot receive a response, but its compensation error must
            # remain available to the owning task instead of becoming CancelledError.
            transaction = asyncio.create_task(panel._restore_persona_data(
                upload, None, {"database", "personas"}, database=(payload, live)))
            task = asyncio.create_task(_settle_data_task(transaction, cancel_on_request=True))
            await _until(lambda: _names(live) == ["payload"])
            # Real local filesystem damage makes the compensating DB open
            # fail while publication remains blocked by its actual file lock.
            live.unlink()
            live.mkdir()
            task.cancel()
            with pytest.raises(RuntimeError, match="恢复补偿失败"):
                await asyncio.wait_for(task, 8)
            assert not service.lock.locked() and not context.maintenance_lock.locked()
            assert not _worker("restore_sqlite")
        finally:
            lock.release()
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await panel.stop()


async def test_transaction_compensation_error_is_not_hidden_by_request_cancellation():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source, target = root / "source.sqlite", root / "target.sqlite"
        _database(source, "payload").close()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def transaction():
            try:
                entered.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                # A real SQLite/file error in compensation must be observable.
                target.mkdir()
                await _data_thread(restore_sqlite, source, target)

        task = asyncio.create_task(_settle_data_task(
            asyncio.create_task(transaction()), cancel_on_request=True))
        await entered.wait()
        await _cancel_repeatedly(task)
        release.set()
        with pytest.raises(sqlite3.OperationalError):
            await task
