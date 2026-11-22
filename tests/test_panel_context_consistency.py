"""面板与上下文一致性回归测试 — issue #126.

覆盖:
1. 在线备份取静态快照: contexts 正在被重写(文件截断)时备份必须等待维护锁,
   不能打包半截文件; 备份内容完整, 恢复后旧 generation 的在途回复写回失败。
2. 恢复前全量校验: 损坏的 contexts 索引/会话 JSON 被拒绝(400), 现有数据不被替换。
3. clear/reset 推进私聊与群聊 generation, 在途回复写入被拒。
4. 维护屏障 freeze 在任意 closer await 点被取消时回滚 frozen 状态。
5. delete_session unlink 失败可回滚: 索引/文件保持一致, 面板返回可重试 409;
   删除中的 tombstone 文件不会进入备份包。
6. database 范围: SQLite backup API 一致性快照(含未 checkpoint 的 WAL 提交),
   损坏快照拒绝恢复且现有库不变。
"""

import asyncio
import hashlib
import io
import json
import secrets
import sqlite3
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import contextmanager


@contextmanager
def _patch_attr(obj, name, value):
    """临时替换属性(不依赖 pytest monkeypatch, 项目自研 runner 无 fixture)。"""
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)

PASSWORD = "secret123"


def make_pwd_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return f"pbkdf2_sha256${salt}${h}"


def _build_panel(
    tmp: Path,
    *,
    context_manager=None,
    history_barrier=None,
    persona_service=None,
    database: tuple[Path, str] | None = None,
):
    from mohobot.web_panel.app import WebPanel

    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    lines = ["data_dir: ./data"]
    if database is not None:
        folder, filename = database
        lines += [
            "database:",
            "  enabled: true",
            f"  folder: \"{folder.as_posix()}\"",
            f"  file: \"{filename}\"",
        ]
    cfg_path = tmp / "global.yaml"
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    panel = WebPanel(
        host="127.0.0.1",
        port=0,
        username="admin",
        password_hash=make_pwd_hash(PASSWORD),
        data_dir=str(data_dir),
        config_path=str(cfg_path),
        context_manager=context_manager,
        history_barrier=history_barrier,
        persona_service=persona_service,
    )
    return panel, data_dir


def _client(panel) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel._app), base_url="http://testserver",
    )


async def _login(client: httpx.AsyncClient) -> dict:
    r = await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _write_private_chat(
    data_dir: Path,
    bot_id: str = "bot_001",
    chat_id: str = "1001",
    session_id: str = "sess_main",
    entries: list[dict] | None = None,
    generation: str | None = None,
) -> Path:
    chat_dir = data_dir / "contexts" / bot_id / "private" / chat_id
    chat_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "sessions": [{
            "id": session_id,
            "name": "默认会话",
            "created": 1700000000,
            "generation": generation or secrets.token_hex(16),
            "persona_id": "",
        }],
        "active": session_id,
    }
    (chat_dir / "session_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    (chat_dir / f"{session_id}.json").write_text(
        json.dumps(entries or [], ensure_ascii=False), encoding="utf-8")
    return chat_dir


def _zip_bytes(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()


async def _backup(client, auth, dirs: list[str], bots="all") -> bytes:
    r = await client.post(
        "/api/data/backup",
        json={"data": {"bots": bots, "dirs": dirs}},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    return r.content


async def _restore(client, auth, content: bytes, dirs: list[str], bots="all"):
    return await client.post(
        "/api/data/restore",
        files={"file": ("backup.zip", content, "application/zip")},
        data={
            "password": PASSWORD,
            "scope": json.dumps({"bots": bots, "dirs": dirs}),
        },
        headers=auth,
    )


# ── 1. 在线备份必须等维护锁, 取静态快照 ─────────────────────────


async def test_backup_waits_for_in_flight_context_write_and_restore_invalidates(tmp_path):
    from mohobot.context_manager import ContextManager

    data_dir = tmp_path / "data"
    chat_dir = _write_private_chat(data_dir)
    session_path = chat_dir / "sess_main.json"
    session_path.write_text("[]", encoding="utf-8")

    cm = ContextManager(data_dir=str(data_dir))
    panel, _ = _build_panel(tmp_path, context_manager=cm)

    full = [{"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"}]
    started = asyncio.Event()
    finish = asyncio.Event()

    async def in_flight_write():
        # 模拟 append_context 的锁内 read-modify-write: 文件先被截断。
        async with cm.maintenance_lock, cm.chat_lock("bot_001", "private", "1001"):
            session_path.write_text("[", encoding="utf-8")
            started.set()
            await finish.wait()
            session_path.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")

    writer = asyncio.create_task(in_flight_write())
    await asyncio.wait_for(started.wait(), 5)
    async with _client(panel) as client:
        auth = await _login(client)
        backup_task = asyncio.create_task(
            client.post("/api/data/backup",
                        json={"data": {"bots": "all", "dirs": ["contexts"]}},
                        headers=auth))
        await asyncio.sleep(0.3)
        assert not backup_task.done(), "备份不能在 contexts 维护锁内直读被重写的文件"
        finish.set()
        await writer
        response = await asyncio.wait_for(backup_task, 15)
        assert response.status_code == 200, response.text
        payload = response.content

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        name = "contexts/bot_001/private/1001/sess_main.json"
        assert name in zf.namelist()
        packaged = json.loads(zf.read(name))
    assert packaged == full, "备份必须打包写完成的完整 contexts, 不能是截断文件"

    async with _client(panel) as client:
        auth = await _login(client)
        stale = await cm.capture_session("bot_001", "private", "1001")
        response = await _restore(client, auth, payload, ["contexts"])
        assert response.status_code == 200, response.text

        # 恢复重写了会话 generation: 恢复前捕获的在途回复不得写回。
        ok = await cm.append_context(
            "bot_001", "private", "1001", [{"role": "user", "content": "stale"}],
            session_id=stale["id"], generation=stale["generation"])
        assert ok is False
        fresh = await cm.capture_session("bot_001", "private", "1001")
        assert fresh["generation"] != stale["generation"]
        ok = await cm.append_context(
            "bot_001", "private", "1001", [{"role": "assistant", "content": "new"}],
            session_id=fresh["id"], generation=fresh["generation"])
        assert ok is True
        assert await cm.load_context("bot_001", "private", "1001") == full + [
            {"role": "assistant", "content": "new"}]


# ── 2. 恢复前全量校验, 校验失败不替换现有数据 ──────────────────


async def test_restore_rejects_corrupted_contexts_backup(tmp_path):
    data_dir = tmp_path / "data"
    chat_dir = _write_private_chat(data_dir, entries=[{"role": "user", "content": "keep"}])
    panel, _ = _build_panel(tmp_path)
    before = {p: p.read_bytes() for p in (data_dir / "contexts").rglob("*") if p.is_file()}

    good_index = {
        "sessions": [{"id": "sess_main", "name": "默认会话", "created": 1700000000,
                      "generation": secrets.token_hex(16), "persona_id": ""}],
        "active": "sess_main",
    }
    corrupt_cases = {
        "truncated index json": {"session_index.json": "{\"sessions\": ["},
        "index not dict": {"session_index.json": "[]"},
        "empty sessions": {"session_index.json": json.dumps(
            {"sessions": [], "active": "sess_main"})},
        "active missing": {"session_index.json": json.dumps(
            {**good_index, "active": "sess_404"})},
        "duplicate session id": {"session_index.json": json.dumps({
            "sessions": good_index["sessions"] * 2, "active": "sess_main"})},
        "session file not list": {
            "session_index.json": json.dumps(good_index),
            "sess_main.json": "{\"role\": \"user\"}",
        },
        "session file truncated": {
            "session_index.json": json.dumps(good_index),
            "sess_main.json": "[{\"role\":",
        },
        "session file missing": {"session_index.json": json.dumps(good_index)},
    }

    async with _client(panel) as client:
        auth = await _login(client)
        for label, files in corrupt_cases.items():
            payload = _zip_bytes({
                "contexts/bot_001/private/1001/" + name: content
                for name, content in files.items()
            })
            response = await _restore(client, auth, payload, ["contexts"])
            assert response.status_code == 400, f"{label}: {response.text}"
            after = {p: p.read_bytes() for p in (data_dir / "contexts").rglob("*") if p.is_file()}
            assert after == before, f"{label}: 校验失败后现有数据必须原样保留"

    assert json.loads((chat_dir / "session_index.json").read_text(encoding="utf-8"))
    assert json.loads((chat_dir / "sess_main.json").read_text(encoding="utf-8")) == [
        {"role": "user", "content": "keep"}]


async def test_cleanup_contexts_advances_group_generation(tmp_path):
    """清理 contexts 后, 清理前捕获的群聊在途回复不得重建上下文。"""
    from mohobot.context_manager import ContextManager

    data_dir = tmp_path / "data"
    _write_private_chat(data_dir)
    cm = ContextManager(data_dir=str(data_dir))
    cap = await cm.capture_session("bot_001", "group", "999")
    assert await cm.append_context(
        "bot_001", "group", "999", [{"role": "user", "content": "before"}],
        session_id=cap["id"], generation=cap["generation"]) is True

    panel, _ = _build_panel(tmp_path, context_manager=cm)
    async with _client(panel) as client:
        auth = await _login(client)
        r = await client.post(
            "/api/data/cleanup",
            json={"data": {"password": PASSWORD,
                           "scope": {"bots": "all", "dirs": ["contexts"]}}},
            headers=auth,
        )
        assert r.status_code == 200, r.text

    assert await cm.append_context(
        "bot_001", "group", "999", [{"role": "user", "content": "stale"}],
        session_id=cap["id"], generation=cap["generation"]) is False
    fresh = await cm.capture_session("bot_001", "group", "999")
    assert fresh["generation"] != cap["generation"]
    assert await cm.append_context(
        "bot_001", "group", "999", [{"role": "assistant", "content": "new"}],
        session_id=fresh["id"], generation=fresh["generation"]) is True


# ── 3. clear/reset 推进 generation ─────────────────────────────


async def test_clear_and_reset_advance_generation(tmp_path):
    from mohobot.context_manager import ContextManager

    cm = ContextManager(data_dir=str(tmp_path / "data"))

    # 私聊 clear
    cap = await cm.capture_session("bot_x", "private", "u1")
    assert await cm.append_context(
        "bot_x", "private", "u1", [{"role": "user", "content": "a"}],
        session_id=cap["id"], generation=cap["generation"]) is True
    await cm.clear_context("bot_x", "private", "u1")
    assert await cm.append_context(
        "bot_x", "private", "u1", [{"role": "user", "content": "stale"}],
        session_id=cap["id"], generation=cap["generation"]) is False
    assert await cm.load_context("bot_x", "private", "u1") == []
    cap2 = await cm.capture_session("bot_x", "private", "u1")
    assert cap2["generation"] != cap["generation"]

    # 私聊 reset
    assert await cm.append_context(
        "bot_x", "private", "u1", [{"role": "user", "content": "b"}],
        session_id=cap2["id"], generation=cap2["generation"]) is True
    cap_before_reset = await cm.capture_session("bot_x", "private", "u1")
    assert await cm.reset_session("bot_x", "private", "u1", cap_before_reset["id"]) is True
    assert await cm.append_context(
        "bot_x", "private", "u1", [{"role": "user", "content": "stale"}],
        session_id=cap_before_reset["id"],
        generation=cap_before_reset["generation"]) is False
    cap3 = await cm.capture_session("bot_x", "private", "u1")
    assert cap3["generation"] != cap_before_reset["generation"]

    # 群聊 clear / reset
    gcap = await cm.capture_session("bot_x", "group", "g1")
    assert gcap["generation"] == "group-main"
    assert await cm.append_context(
        "bot_x", "group", "g1", [{"role": "user", "content": "g"}],
        session_id=gcap["id"], generation=gcap["generation"]) is True
    await cm.clear_context("bot_x", "group", "g1")
    assert await cm.append_context(
        "bot_x", "group", "g1", [{"role": "user", "content": "stale"}],
        session_id=gcap["id"], generation=gcap["generation"]) is False
    gcap2 = await cm.capture_session("bot_x", "group", "g1")
    assert gcap2["generation"] not in ("group-main", gcap["generation"])
    assert await cm.reset_session("bot_x", "group", "g1", "main") is True
    assert await cm.append_context(
        "bot_x", "group", "g1", [{"role": "user", "content": "stale"}],
        session_id=gcap2["id"], generation=gcap2["generation"]) is False
    gcap3 = await cm.capture_session("bot_x", "group", "g1")
    assert gcap3["generation"] != gcap2["generation"]


# ── 4. 屏障 freeze 取消回滚 ───────────────────────────────────


async def test_freeze_cancellation_rolls_back_frozen_state():
    from mohobot.services.history_barrier import HistoryBarrier

    for cancel_after in (0, 1, 2):
        barrier = HistoryBarrier()
        gate = asyncio.Event()
        reached: list[int] = []

        def make_closer(index: int):
            async def closer():
                reached.append(index)
                if index >= cancel_after:
                    await gate.wait()  # 永远不 set: 冻结卡在这里
            return closer

        for i in range(3):
            barrier.register_closer(make_closer(i))
        task = asyncio.create_task(barrier.freeze())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert barrier.frozen is False, f"取消点 {cancel_after}: 冻结状态必须回滚"
        assert reached == list(range(cancel_after + 1))
        # 回滚状态可重试: thaw 幂等, 不会再次抛错。
        await barrier.thaw()
        assert barrier.frozen is False

    # 正常路径: freeze 成功 → frozen; thaw 幂等 → 未冻结
    barrier = HistoryBarrier()
    closed: list[int] = []

    async def ok_closer():
        closed.append(1)

    barrier.register_closer(ok_closer)
    async with barrier as acquired:
        assert acquired is barrier and barrier.frozen is True
        assert closed == [1]
    assert barrier.frozen is False
    await barrier.thaw()
    assert barrier.frozen is False


# ── 5. 删除会话的 unlink 失败回滚 + tombstone 不进备份 ────────


async def test_delete_session_unlink_failure_rolls_back(tmp_path):
    from mohobot.context_manager import ContextManager

    data_dir = tmp_path / "data"
    cm = ContextManager(data_dir=str(data_dir))
    await cm.create_session("bot_x", "private", "u1", "第一")
    sid = await cm.create_session("bot_x", "private", "u1", "第二")
    chat_dir = data_dir / "contexts" / "bot_x" / "private" / "u1"
    session_path = chat_dir / f"{sid}.json"
    index_path = chat_dir / "session_index.json"
    kept = [{"role": "user", "content": "keep"}]
    session_path.write_text(json.dumps(kept), encoding="utf-8")

    real_unlink = Path.unlink
    calls = {"failed": 0}

    def failing_unlink(self, *args, **kwargs):
        if ".deleting-" in self.name:
            calls["failed"] += 1
            raise PermissionError("文件被占用")
        return real_unlink(self, *args, **kwargs)

    with _patch_attr(Path, "unlink", failing_unlink):
        with pytest.raises(OSError) as excinfo:
            await cm.delete_session("bot_x", "private", "u1", sid)
    assert "重试" in str(excinfo.value)
    assert calls["failed"] == 1
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert sid in [s["id"] for s in index["sessions"]], "回滚后索引必须仍包含该会话"
    assert session_path.exists(), "回滚后会话文件必须恢复"
    assert json.loads(session_path.read_text(encoding="utf-8")) == kept
    assert not [p for p in chat_dir.iterdir() if ".deleting-" in p.name]

    assert await cm.delete_session("bot_x", "private", "u1", sid) is True
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert sid not in [s["id"] for s in index["sessions"]]
    assert not session_path.exists()


async def test_delete_endpoint_returns_409_and_retry_succeeds(tmp_path):
    from mohobot.context_manager import ContextManager

    data_dir = tmp_path / "data"
    cm = ContextManager(data_dir=str(data_dir))
    await cm.create_session("bot_x", "private", "u1", "第一")
    sid = await cm.create_session("bot_x", "private", "u1", "第二")
    panel, _ = _build_panel(tmp_path, context_manager=cm)

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if ".deleting-" in self.name:
            raise PermissionError("文件被占用")
        return real_unlink(self, *args, **kwargs)

    async with _client(panel) as client:
        auth = await _login(client)
        with _patch_attr(Path, "unlink", failing_unlink):
            r = await client.delete(
                f"/api/contexts/bot_x/private/u1/session/{sid}", headers=auth)
        assert r.status_code == 409, r.text
        assert "重试" in r.json()["detail"]
        r = await client.delete(
            f"/api/contexts/bot_x/private/u1/session/{sid}", headers=auth)
        assert r.status_code == 200, r.text
        index = json.loads(
            (data_dir / "contexts/bot_x/private/u1/session_index.json").read_text(
                encoding="utf-8"))
        assert sid not in [s["id"] for s in index["sessions"]]


async def test_tombstone_files_excluded_from_backup(tmp_path):
    data_dir = tmp_path / "data"
    chat_dir = _write_private_chat(data_dir)
    tombstone = chat_dir / ".sess_002.json.deleting-deadbeef"
    tombstone.write_text(json.dumps([{"role": "user", "content": "deleted"}]), encoding="utf-8")
    panel, _ = _build_panel(tmp_path)

    async with _client(panel) as client:
        auth = await _login(client)
        payload = await _backup(client, auth, ["contexts"])
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = zf.namelist()
    assert any(name.endswith("sess_main.json") for name in names)
    assert not any(".deleting-" in name for name in names), "删除中的会话文件不能进备份"


# ── 6. database 范围: SQLite 一致性备份/恢复 ─────────────────


def _make_wal_db(path: Path, names: list[str]) -> sqlite3.Connection:
    """创建 WAL 模式的真实 SQLite 库并保持连接, 让提交留在 WAL 中未 checkpoint。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY)")
    for name in names:
        conn.execute("INSERT INTO users(name) VALUES (?)", (name,))
    conn.commit()
    return conn


def _sqlite_reader(blob: bytes, temp_root: Path) -> list[str]:
    path = temp_root / f"read_{secrets.token_hex(4)}.db"
    path.write_bytes(blob)
    conn = sqlite3.connect(str(path))
    try:
        return [row[0] for row in conn.execute("SELECT name FROM users ORDER BY name")]
    finally:
        conn.close()


async def test_database_scope_backup_restore_and_validation(tmp_path):
    db_folder = tmp_path / "db"
    db_path = db_folder / "mohobot.db"
    live = _make_wal_db(db_path, ["wal-live"])
    try:
        panel, _ = _build_panel(tmp_path, database=(db_folder, "mohobot.db"))

        async with _client(panel) as client:
            auth = await _login(client)

            # 备份: 未 checkpoint 的 WAL 提交也要出现在快照里
            payload = await _backup(client, auth, ["database"])
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                names = zf.namelist()
                assert names == ["database/mohobot.db"]
                snapshot = zf.read("database/mohobot.db")
            assert _sqlite_reader(snapshot, tmp_path) == ["wal-live"]

            # database 是全局范围: 不选 Bot(bots=[]) 也能备份
            payload_global = await _backup(client, auth, ["database"], bots=[])
            with zipfile.ZipFile(io.BytesIO(payload_global)) as zf:
                assert zf.namelist() == ["database/mohobot.db"]

            # 恢复前再写入一行, 恢复后应回到备份时的状态
            live.execute("INSERT INTO users(name) VALUES (?)", ("after-backup",))
            live.commit()
            r = await _restore(client, auth, payload, ["database"])
            assert r.status_code == 200, r.text
            assert r.json()["restored"] == 1
            check = sqlite3.connect(str(db_path))
            try:
                assert [row[0] for row in check.execute(
                    "SELECT name FROM users ORDER BY name")] == ["wal-live"]
            finally:
                check.close()

            # 损坏的数据库快照: 400 且现有库不变
            corrupt = _zip_bytes({"database/mohobot.db": b"not a sqlite database"})
            r = await _restore(client, auth, corrupt, ["database"])
            assert r.status_code == 400, r.text
            check = sqlite3.connect(str(db_path))
            try:
                assert [row[0] for row in check.execute(
                    "SELECT name FROM users ORDER BY name")] == ["wal-live"]
            finally:
                check.close()

            # 缺少必要表的快照也必须拒绝
            empty_db = tmp_path / "empty.db"
            conn = sqlite3.connect(str(empty_db))
            conn.execute("CREATE TABLE IF NOT EXISTS unrelated (id INTEGER)")
            conn.commit()
            conn.close()
            payload_bad = _zip_bytes({"database/mohobot.db": empty_db.read_bytes()})
            r = await _restore(client, auth, payload_bad, ["database"])
            assert r.status_code == 400, r.text

            # 数据库不支持在线清理
            r = await client.post(
                "/api/data/cleanup",
                json={"data": {"password": PASSWORD,
                               "scope": {"bots": "all", "dirs": ["database"]}}},
                headers=auth)
            assert r.status_code == 400, r.text

            # 组合范围: contexts + database, restored 计数同时包含两者
            _write_private_chat(tmp_path / "data")
            combo = await _backup(client, auth, ["contexts", "database"])
            r = await _restore(client, auth, combo, ["contexts", "database"])
            assert r.status_code == 200, r.text
            assert r.json()["restored"] == 3
    finally:
        live.close()
