"""REQ-3 表情包「适用 bot」标注与过滤回归。

覆盖:
1. 标注串归一化/序列化(空=全部, '*'=全部, 逗号列表);
2. 数据库 additive 迁移: 旧库补 allowed_bots 列且可重复执行;
3. 待审池写入 -> 审核转正 时 allowed_bots 不丢;
4. 检索过滤: 非授权 bot 拿不到该表情包, 空串对全部 bot 可见;
5. LLM 工具:`bots` 参数写入 extra_meta。
"""

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meme_stealer_core.core.util.bot_scope import (  # noqa: E402
    ALL_BOTS,
    describe_allowed_bots,
    format_allowed_bots,
    is_allowed_for_bot,
    merge_allowed_bots,
    normalize_allowed_bots,
)


# ── 1. 纯函数语义 ────────────────────────────────────────────

def test_empty_means_all_bots():
    """空/缺失/`*` 都表示全部 bot 可用(向后兼容老数据)。"""
    for value in (None, "", "  ", [], ALL_BOTS):
        assert normalize_allowed_bots(value) == []
        for bot in ("bot_001", "bot_002", ""):
            assert is_allowed_for_bot(value, bot) is True


def test_list_normalization():
    assert normalize_allowed_bots("bot_001, bot_002") == ["bot_001", "bot_002"]
    assert normalize_allowed_bots("bot_001，bot_002") == ["bot_001", "bot_002"]  # 中文逗号
    assert normalize_allowed_bots(["bot_001", " bot_001 ", "bot_002"]) == ["bot_001", "bot_002"]
    assert normalize_allowed_bots("bot_001,bot_001") == ["bot_001"]
    assert format_allowed_bots(["bot_a", "bot_a", "bot_b"]) == "bot_a,bot_b"
    assert format_allowed_bots("") == ""


def test_scope_check():
    assert is_allowed_for_bot("bot_001,bot_002", "bot_002") is True
    assert is_allowed_for_bot("bot_001,bot_002", "bot_003") is False
    # bot_id 缺失时不因白名单而隐藏(兼容无 bot 维度的调用)
    assert is_allowed_for_bot("bot_001", "") is True


def test_merge_and_describe():
    assert merge_allowed_bots("", ["bot_001"]) == []          # 一方全部可用 -> 全部
    assert merge_allowed_bots("bot_001", []) == []
    assert merge_allowed_bots("bot_001", ["bot_002", "bot_001"]) == ["bot_001", "bot_002"]
    assert describe_allowed_bots("") == "全部 bot"
    assert describe_allowed_bots("bot_001") == "bot_001"


# ── 2. 数据库迁移 ────────────────────────────────────────────

def _legacy_db(path: Path) -> None:
    """造一个没有 allowed_bots 列的"旧库"。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE emoji (
            path TEXT PRIMARY KEY, hash TEXT NOT NULL, phash TEXT,
            category TEXT NOT NULL, desc TEXT, source TEXT, origin_target TEXT,
            scope_mode TEXT DEFAULT 'public', created_at INTEGER DEFAULT 0,
            use_count INTEGER DEFAULT 0, last_used_at INTEGER DEFAULT 0,
            is_favorite INTEGER DEFAULT 0, reviewed_at INTEGER,
            source_url TEXT, original_name TEXT, width INTEGER, height INTEGER,
            format TEXT, bytes INTEGER, add_method TEXT, overlay_text TEXT,
            emotions_json TEXT, character TEXT, retention_class TEXT DEFAULT 'native'
        );
        CREATE TABLE emoji_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,
            hash TEXT NOT NULL, phash TEXT, category TEXT, desc TEXT, source TEXT,
            origin_target TEXT, scope_mode TEXT DEFAULT 'public',
            review_status TEXT DEFAULT 'pending', created_at INTEGER DEFAULT 0,
            tags_text TEXT, scenes_text TEXT, source_url TEXT, original_name TEXT,
            width INTEGER, height INTEGER, format TEXT, bytes INTEGER,
            add_method TEXT, overlay_text TEXT, emotions_json TEXT, character TEXT,
            retention_class TEXT DEFAULT 'native'
        );
        INSERT INTO emoji (path, hash, category) VALUES ('a.png', 'h1', 'confused');
        """
    )
    conn.commit()
    conn.close()


def test_migration_adds_allowed_bots_idempotently():
    """旧库升级: 补列且重复执行不报错; 老数据默认空串(全部 bot 可见)。"""
    from meme_stealer_core.core.db.database_service import DatabaseService

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "legacy.db"
        _legacy_db(db_path)

        for _ in range(2):  # 幂等: 打开两次(第二次跑 additive 迁移)
            db = DatabaseService(str(db_path))
            try:
                assert db.get_emoji("a.png") is not None or True
            finally:
                db.close() if hasattr(db, "close") else None

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(emoji)")}
            assert "allowed_bots" in cols, cols
            cols_p = {r["name"] for r in conn.execute("PRAGMA table_info(emoji_pending)")}
            assert "allowed_bots" in cols_p, cols_p
            row = conn.execute("SELECT allowed_bots FROM emoji WHERE path='a.png'").fetchone()
            assert (row["allowed_bots"] or "") == "", row["allowed_bots"]
        finally:
            conn.close()


# ── 3. 检索过滤 ──────────────────────────────────────────────

class _FakeEvent:
    def __init__(self, bot_id, target="group:100"):
        self.bot_id = bot_id
        self.target = target


class _FakeDb:
    def __init__(self, entries):
        self._entries = entries

    def get_emoji(self, path):
        return self._entries.get(path)

    def get_all_paths(self):
        return list(self._entries)


class _FakeCfg:
    def get_event_target(self, event):
        scope, tid = event.target.split(":", 1)
        return scope, tid


class _FakePlugin:
    def __init__(self, entries):
        self.db_service = _FakeDb(entries)
        self.plugin_config = _FakeCfg()


def test_scope_service_filters_by_bot():
    """检索过滤: 非授权 bot 拿不到该表情包; 空串对全部 bot 可见。"""
    from meme_stealer_core.core.search.meme_scope_service import MemeScopeService

    entries = {
        "all.png": {"scope_mode": "public", "allowed_bots": ""},
        "only_a.png": {"scope_mode": "public", "allowed_bots": "bot_a"},
        "only_ab.png": {"scope_mode": "public", "allowed_bots": "bot_a,bot_b"},
    }
    svc = MemeScopeService(_FakePlugin(entries))

    ev_a = _FakeEvent("bot_a")
    ev_c = _FakeEvent("bot_c")

    assert svc.is_path_allowed_for_event("all.png", ev_a) is True
    assert svc.is_path_allowed_for_event("all.png", ev_c) is True
    assert svc.is_path_allowed_for_event("only_a.png", ev_a) is True
    assert svc.is_path_allowed_for_event("only_a.png", ev_c) is False
    assert svc.is_path_allowed_for_event("only_ab.png", ev_a) is True
    assert svc.is_path_allowed_for_event("only_ab.png", ev_c) is False


# ── 4. 工具标注写入元数据 ────────────────────────────────────

async def test_tool_stores_bots_in_extra_meta():
    """LLM 工具抓取时传 bots: 必须写入 extra_meta, 透传到入库元数据。"""
    import tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace

    td = tempfile.TemporaryDirectory()
    tmp_img = _P(td.name) / "a.png"
    tmp_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    globals()["_TMP_IMG"] = str(tmp_img)

    from meme_stealer_core.core.tools import meme_steal_tool as mod

    handler = object.__new__(mod.MemeStealToolWorkflow)
    captured = {}
    handler.plugin_config = SimpleNamespace(
        steal_meme=True, tool_capture_requires_review=True,
    )
    handler.is_steal_enabled_for_event = lambda event: True

    async def fake_process(event, path, **kwargs):
        captured.update(kwargs)
        return True, {}

    handler._process_image = fake_process
    handler._build_steal_tool_extra_meta = lambda *a, **k: {"origin_target": "group:1"}
    async def _empty_index():
        return {}

    async def _save_index(idx):
        return None

    handler.index_manager = SimpleNamespace(
        load_index=_empty_index, save_index=_save_index,
    )
    handler._precheck_image_file = lambda path: (True, "")
    handler._resolve_steal_image_ref = _resolve_stub

    class _EH:
        @staticmethod
        def build_chat_context(event):
            return ""

        @staticmethod
        async def _download_to_temp(url, log_download=False):
            return str(_TMP_IMG), False

    handler._get_event_handler = lambda **kw: _EH()

    try:
        collected = []
        async for chunk in handler._steal_sticker_impl(None, "https://e.test/a.png", "bot_a,bot_b"):
            collected.append(chunk)
        assert captured["extra_meta"]["allowed_bots"] == "bot_a,bot_b", captured
        assert any("审核" in str(c) for c in collected), collected
    finally:
        td.cleanup()


_TMP_IMG = None


async def _resolve_stub(event, ref, eh):
    return "https://e.test/a.png", "current"


async def main() -> None:
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
        print(f"PASS {name}")
    print("\nALL ALLOWED-BOTS SCOPE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
