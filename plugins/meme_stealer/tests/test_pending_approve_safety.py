"""待审审核事务安全回归（issue #132）。

覆盖：
1. 正常审核通过：文件进分类目录、图库有记录、pending 行删除；
2. 文件移动/DB 写入阶段被取消：pending 行保留、文件回滚、不产生半提交记录；
3. 提交点之后被取消：状态保持一致，不留下孤儿文件或记录；
4. 进程中断恢复标记：上次审核中断后再次审核/拒绝能补齐或回滚；
5. pending 文件丢失后按内容哈希从分类目录找回孤儿文件；
6. local 作用域必须有有效来源（group:<id>/user:<id>），否则 fail-closed。
"""

import asyncio
import hashlib
import shutil
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from meme_stealer_core.api import pending as pending_module
from meme_stealer_core.plugin_api import PluginAPI
from meme_stealer_core.core.config.config import PluginConfig
from meme_stealer_core.core.db.database_service import DatabaseService
from meme_stealer_core.core.db.pending_repository import LOCAL_SCOPE_ORIGIN_ERROR
from meme_stealer_core.core.search.meme_scope_service import MemeScopeService


class _FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def get_json(self):
        return self.payload


def _patch_http(monkeypatch, payload):
    monkeypatch.setattr(pending_module, "request", _FakeRequest(payload))
    monkeypatch.setattr(pending_module, "jsonify", lambda data: data)


@pytest.fixture
def env(tmp_path):
    data_dir = tmp_path / "data"
    cfg = PluginConfig({}, data_dir)
    db = DatabaseService(str(data_dir / "emoji.db"))
    plugin = SimpleNamespace(
        plugin_config=cfg,
        db_service=db,
        base_dir=str(data_dir),
        meme_selector=None,
        steal_pool_capacity=200,
    )
    api = PluginAPI(plugin)
    return SimpleNamespace(cfg=cfg, db=db, api=api, plugin=plugin, data_dir=data_dir)


def _write_pending_file(env, name: str = "p.png", data: bytes = b"pending-bytes") -> Path:
    path = env.cfg.pending_dir / name
    path.write_bytes(data)
    return path


async def _insert_pending(env, name="p.png", data=b"pending-bytes", **overrides):
    src = _write_pending_file(env, name, data)
    meta = {
        "path": str(src),
        "hash": hashlib.sha256(data).hexdigest(),
        "phash": "",
        "category": "happy",
        "desc": "测试",
        "source": "test",
        "origin_target": "",
        "scope_mode": "public",
    }
    meta.update(overrides)
    pending_id = await env.db.insert_pending(meta)
    assert pending_id
    return pending_id, src


def _force_local_scope(env, pending_id: int, origin_target: str) -> None:
    """直接改库构造「历史无来源 local」数据，模拟老数据/绕过 API 写入。"""
    with sqlite3.connect(str(env.data_dir / "emoji.db")) as conn:
        conn.execute(
            "UPDATE emoji_pending SET scope_mode='local', origin_target=? WHERE id=?",
            (origin_target, pending_id),
        )
        conn.commit()


def _cat_path(env, name: str = "p.png", category: str = "happy") -> Path:
    return env.cfg.categories_dir / category / name


def _emoji_entry(env, cat_path: Path, name="p.png", data=b"pending-bytes") -> dict:
    return {
        "path": str(cat_path),
        "hash": hashlib.sha256(data).hexdigest(),
        "phash": "",
        "category": "happy",
        "desc": "测试",
        "source": "test",
        "origin_target": "",
        "scope_mode": "public",
        "created_at": 0,
        "use_count": 0,
        "last_used_at": 0,
        "reviewed_at": 0,
        "tags": [],
        "scenes": [],
        "overlay_text": "",
        "emotions": [],
        "character": "",
        "source_url": None,
        "original_name": name,
        "width": None,
        "height": None,
        "format": "png",
        "bytes": len(data),
        "add_method": "test",
        "retention_class": "native",
        "allowed_bots": "",
    }


class _BlockingShutil:
    """move 前通知测试线程并阻塞，直到测试放行后才真正移动。"""

    def __init__(self, real, started: threading.Event, release: threading.Event):
        self._real = real
        self.started = started
        self.release = release

    def move(self, src, dst):
        self.started.set()
        if not self.release.wait(10):
            raise TimeoutError("test did not release move")
        return self._real.move(src, dst)


# ── 1. 正常路径 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_moves_file_and_commits(env):
    pending_id, src = await _insert_pending(env)

    ok, msg = await env.api._approve_one(env.db, pending_id)

    assert (ok, msg) == (True, "")
    cat_path = _cat_path(env)
    assert cat_path.is_file()
    assert not src.exists()
    assert env.db.get_pending(pending_id) is None
    assert env.db.get_emoji(str(cat_path)) is not None
    assert not env.api._read_approve_marker(pending_id)


# ── 2. 移动阶段被取消 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_during_move_rolls_back(env, monkeypatch):
    pending_id, src = await _insert_pending(env)
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(
        pending_module, "shutil", _BlockingShutil(shutil, started, release)
    )

    task = asyncio.create_task(env.api._approve_one(env.db, pending_id))
    assert await asyncio.to_thread(started.wait, 10)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    cat_path = _cat_path(env)
    assert src.is_file(), "取消后待审文件应回到 pending 路径"
    assert not cat_path.exists()
    assert env.db.get_pending(pending_id) is not None, "pending 行必须保留"
    assert env.db.get_emoji(str(cat_path)) is None, "不能留下半提交图库记录"
    assert not env.api._read_approve_marker(pending_id)


# ── 3. DB 写入等待/提交被取消 ───────────────────────────────


@pytest.mark.asyncio
async def test_cancel_while_waiting_db_write_lock_rolls_back(env, monkeypatch):
    pending_id, src = await _insert_pending(env)

    async with env.db._write_lock:
        task = asyncio.create_task(env.api._approve_one(env.db, pending_id))
        # 等任务完成 move 并阻塞在 insert_batch 拿写锁
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not src.exists():
                break
        assert not src.exists()
        task.cancel()
        await asyncio.sleep(0.05)
    with pytest.raises(asyncio.CancelledError):
        await task

    cat_path = _cat_path(env)
    assert src.is_file()
    assert not cat_path.exists()
    assert env.db.get_pending(pending_id) is not None
    assert env.db.get_emoji(str(cat_path)) is None


@pytest.mark.asyncio
async def test_cancel_after_insert_compensates(env, monkeypatch):
    pending_id, src = await _insert_pending(env)
    inserted_event, release = asyncio.Event(), asyncio.Event()
    original_insert = env.db.insert_batch

    async def slow_insert(entries):
        result = await original_insert(entries)
        inserted_event.set()
        await release.wait()
        return result

    monkeypatch.setattr(env.db, "insert_batch", slow_insert)

    task = asyncio.create_task(env.api._approve_one(env.db, pending_id))
    await asyncio.wait_for(inserted_event.wait(), 10)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    cat_path = _cat_path(env)
    assert src.is_file(), "已写入的图库记录必须被补偿删除，文件回滚"
    assert not cat_path.exists()
    assert env.db.get_pending(pending_id) is not None
    assert env.db.get_emoji(str(cat_path)) is None


# ── 4. 提交点之后被取消 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_after_commit_keeps_consistent_state(env, monkeypatch):
    pending_id, src = await _insert_pending(env)
    promote_started, release = asyncio.Event(), asyncio.Event()
    original_promote = env.db.promote_source_pending_path

    async def slow_promote(pending_path, path):
        promote_started.set()
        await release.wait()
        return await original_promote(pending_path, path)

    monkeypatch.setattr(env.db, "promote_source_pending_path", slow_promote)

    task = asyncio.create_task(env.api._approve_one(env.db, pending_id))
    await asyncio.wait_for(promote_started.wait(), 10)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    cat_path = _cat_path(env)
    # 提交点已过（pending 行已删、文件已入分类目录）：不得回滚成半状态
    assert cat_path.is_file()
    assert not src.exists()
    assert env.db.get_pending(pending_id) is None
    assert env.db.get_emoji(str(cat_path)) is not None
    assert not env.api._read_approve_marker(pending_id)


# ── 5. 中断恢复 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recover_interrupted_approve_before_db_insert(env):
    pending_id, src = await _insert_pending(env)
    cat_path = _cat_path(env)
    env.api._write_approve_marker(pending_id, str(src), str(cat_path))
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(cat_path))

    ok, msg = await env.api._approve_one(env.db, pending_id)

    assert (ok, msg) == (True, "")
    assert cat_path.is_file()
    assert env.db.get_pending(pending_id) is None
    assert env.db.get_emoji(str(cat_path)) is not None
    assert not env.api._read_approve_marker(pending_id)


@pytest.mark.asyncio
async def test_recover_interrupted_approve_after_db_insert(env):
    pending_id, src = await _insert_pending(env)
    cat_path = _cat_path(env)
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(cat_path))
    await env.db.insert_batch([_emoji_entry(env, cat_path)])
    env.api._write_approve_marker(pending_id, str(src), str(cat_path))

    ok, msg = await env.api._approve_one(env.db, pending_id)

    assert (ok, msg) == (True, "")
    assert env.db.get_pending(pending_id) is None
    assert env.db.get_emoji(str(cat_path)) is not None
    assert not env.api._read_approve_marker(pending_id)


@pytest.mark.asyncio
async def test_reject_recovers_interrupted_approve(env, monkeypatch):
    pending_id, src = await _insert_pending(env)
    cat_path = _cat_path(env)
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(cat_path))
    await env.db.insert_batch([_emoji_entry(env, cat_path)])
    env.api._write_approve_marker(pending_id, str(src), str(cat_path))
    _patch_http(monkeypatch, {"id": pending_id})

    payload = await env.api.handle_pending_reject()

    assert payload["success"] is True
    assert env.db.get_pending(pending_id) is None
    assert env.db.get_emoji(str(cat_path)) is None
    assert not cat_path.exists()
    assert not env.api._read_approve_marker(pending_id)


# ── 6. 孤儿文件找回 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_recovers_orphan_file_by_hash(env):
    pending_id, src = await _insert_pending(env, name="lost.png", data=b"orphan-data")
    cat_path = _cat_path(env, name="lost.png")
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    cat_path.write_bytes(b"orphan-data")
    src.unlink()

    ok, msg = await env.api._approve_one(env.db, pending_id)

    assert (ok, msg) == (True, "")
    assert cat_path.is_file()
    assert env.db.get_pending(pending_id) is None
    assert env.db.get_emoji(str(cat_path)) is not None


@pytest.mark.asyncio
async def test_approve_missing_file_without_orphan_cleans_row(env):
    pending_id, src = await _insert_pending(env)
    src.unlink()

    ok, msg = await env.api._approve_one(env.db, pending_id)

    assert (ok, msg) == (False, "pending file missing")
    assert env.db.get_pending(pending_id) is None


# ── 7. local 作用域来源校验 ─────────────────────────────────


@pytest.mark.asyncio
async def test_db_rejects_local_without_origin_on_insert(env):
    src = _write_pending_file(env)
    with pytest.raises(ValueError, match="local"):
        await env.db.insert_pending(
            {
                "path": str(src),
                "hash": "h",
                "category": "happy",
                "scope_mode": "local",
                "origin_target": "",
            }
        )


@pytest.mark.asyncio
async def test_db_rejects_local_without_origin_on_update(env):
    pending_id, _ = await _insert_pending(env)
    with pytest.raises(ValueError, match="local"):
        await env.db.update_pending(pending_id, {"scope_mode": "local"})


@pytest.mark.asyncio
async def test_approve_rejects_local_without_valid_origin(env):
    pending_id, src = await _insert_pending(env)
    _force_local_scope(env, pending_id, "")

    ok, msg = await env.api._approve_one(env.db, pending_id)

    assert (ok, msg) == (False, LOCAL_SCOPE_ORIGIN_ERROR)
    assert src.is_file(), "校验失败时不得移动文件"
    assert env.db.get_pending(pending_id) is not None
    assert not _cat_path(env).exists()


@pytest.mark.asyncio
async def test_approve_rejects_local_with_malformed_origin(env):
    pending_id, src = await _insert_pending(env)
    _force_local_scope(env, pending_id, "not-a-target")
    ok, msg = await env.api._approve_one(env.db, pending_id)
    assert ok is False
    assert src.is_file()


@pytest.mark.asyncio
async def test_approve_accepts_local_with_valid_origin(env):
    pending_id, src = await _insert_pending(env)
    _force_local_scope(env, pending_id, "group:1102231856")

    ok, msg = await env.api._approve_one(env.db, pending_id)

    assert (ok, msg) == (True, "")
    assert _cat_path(env).is_file()
    row = env.db.get_emoji(str(_cat_path(env)))
    assert row["origin_target"] == "group:1102231856"


@pytest.mark.asyncio
async def test_update_local_without_origin_is_rejected(env, monkeypatch):
    pending_id, _ = await _insert_pending(env)
    _patch_http(monkeypatch, {"id": pending_id, "scope_mode": "local"})

    payload = await env.api.handle_pending_update()

    assert payload["success"] is False
    assert payload["error"] == LOCAL_SCOPE_ORIGIN_ERROR


@pytest.mark.asyncio
async def test_update_local_with_valid_origin_succeeds(env, monkeypatch):
    pending_id, _ = await _insert_pending(env)
    _force_local_scope(env, pending_id, "group:42")
    _patch_http(monkeypatch, {"id": pending_id, "desc": "新描述"})

    payload = await env.api.handle_pending_update()

    assert payload["success"] is True
    assert payload["item"]["scope_mode"] == "local"


@pytest.mark.asyncio
async def test_update_unknown_pending_returns_error(env, monkeypatch):
    _patch_http(monkeypatch, {"id": 999999, "desc": "x"})
    payload = await env.api.handle_pending_update()
    assert payload == {"success": False, "error": "pending 不存在"}


# ── 8. 检索侧 fail-closed ──────────────────────────────────


class _ScopeEvent:
    def __init__(self, group_id: str = "100", user_id: str = "", bot_id: str = "bot_001"):
        self.group_id = group_id
        self.user_id = user_id
        self.bot_id = bot_id

    def get_group_id(self) -> str:
        return self.group_id

    def get_sender_id(self) -> str:
        return self.user_id


def _scope_event(group_id: str = "100"):
    return _ScopeEvent(group_id=group_id)


def test_scope_local_requires_valid_origin(env):
    service = MemeScopeService(SimpleNamespace(plugin_config=env.cfg))
    event = _scope_event("100")
    assert service._is_entry_allowed_for_event(
        {"scope_mode": "local", "origin_target": "group:100"}, event
    )
    assert not service._is_entry_allowed_for_event(
        {"scope_mode": "local", "origin_target": "group:200"}, event
    )
    assert not service._is_entry_allowed_for_event(
        {"scope_mode": "local", "origin_target": ""}, event
    )
    assert not service._is_entry_allowed_for_event(
        {"scope_mode": "local", "origin_target": "bogus"}, event
    )
    assert not service._is_entry_allowed_for_event(
        {"scope_mode": "local"}, event
    )
    # 非 local 不受来源限制
    assert service._is_entry_allowed_for_event({"scope_mode": "public"}, event)


# ── 7. 恢复标记写失败 / 已提交行 / 回滚哈希保护 ─────────────


@pytest.mark.asyncio
async def test_marker_write_failure_aborts_before_any_change(env, monkeypatch):
    """无法写恢复标记时宁可不审批: 不移动文件、不写库、pending 行保留(#132 review)。"""
    pending_id, src = await _insert_pending(env)

    def broken_replace(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pending_module.os, "replace", broken_replace)

    ok, msg = await env.api._approve_one(env.db, pending_id)

    assert ok is False
    assert "恢复标记" in msg
    cat_path = _cat_path(env)
    assert src.is_file(), "标记失败不得移动待审文件"
    assert not cat_path.exists()
    assert env.db.get_pending(pending_id) is not None
    assert env.db.get_emoji(str(cat_path)) is None
    marker_dir = env.cfg.cache_dir / "pending_approve"
    assert not list(marker_dir.glob("*.json")), "失败后不能残留恢复标记"
    assert not list(marker_dir.glob("*.tmp")), "失败后不能残留临时文件"


@pytest.mark.asyncio
async def test_reject_skips_recovery_when_pending_already_committed(env, monkeypatch):
    """审核已提交但崩溃残留标记时, 拒绝不能反删已入库的图库记录(#132 review)。"""
    pending_id, src = await _insert_pending(env)
    ok, _msg = await env.api._approve_one(env.db, pending_id)
    assert ok
    cat_path = _cat_path(env)
    # 模拟提交点后清标记前崩溃
    env.api._write_approve_marker(pending_id, str(src), str(cat_path))
    _patch_http(monkeypatch, {"id": pending_id})

    payload = await env.api.handle_pending_reject()

    assert payload["success"] is True
    assert env.db.get_emoji(str(cat_path)) is not None, "已提交记录不能被拒绝路径删除"
    assert cat_path.is_file(), "已提交文件不能被拒绝路径删除"
    assert not env.api._read_approve_marker(pending_id)


@pytest.mark.asyncio
async def test_rollback_skips_record_with_mismatched_hash(env):
    """回滚只删本次写入的记录; 同名路径哈希已变时不得误删(#132 review)。"""
    pending_id, _src = await _insert_pending(env)
    cat_path = _cat_path(env)
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    cat_path.write_bytes(b"pending-bytes")
    entry = _emoji_entry(env, cat_path)
    entry["hash"] = "someone-elses-hash"
    await env.db.insert_batch([entry])

    await env.api._rollback_approval(
        env.db,
        pending_id,
        str(env.cfg.pending_dir / "p.png"),
        str(cat_path),
        inserted=True,
        expected_hash="expected-hash",
    )

    assert env.db.get_emoji(str(cat_path)) is not None, "哈希不匹配时不得删除他人记录"
    assert cat_path.is_file()
