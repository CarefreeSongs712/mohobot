"""维护/同步脚本的安全性回归测试(issue #134)。

覆盖:
- 迁移脚本按群隔离去重、dry-run 完全只读、坏行中止、--yes 确认、幂等;
- 知识库清理脚本 WAL 一致备份、备份文件不覆盖、dry-run 只读、确认语义;
- 两个同步脚本按成功/部分失败/全失败返回不同退出码。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from _test_helpers import patched_attrs as _patched_attrs

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, rows: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── migrate_history_layout ────────────────────────────────────────────


class _NotATTY:
    def isatty(self) -> bool:
        return False


def _non_interactive(func):
    """以非交互 stdin 执行测试(不依赖 pytest monkeypatch fixture)。"""

    def wrapper(tmp_path: Path):
        old = sys.stdin
        sys.stdin = _NotATTY()
        try:
            return func(tmp_path)
        finally:
            sys.stdin = old

    return wrapper


def test_migrate_keeps_same_content_in_different_groups(tmp_path: Path):
    """无 message_id 的相同内容属于不同群时必须各自保留(原全局 seen 会吞掉)。"""
    data = tmp_path
    same = [
        {"time": 100, "user_id": 7, "raw_message": "同样的话", "message": "同样的话"}
    ]
    _write_jsonl(data / "history" / "bot_001" / "group" / "111.jsonl", same)
    _write_jsonl(data / "history" / "bot_001" / "group" / "222.jsonl", same)

    module = _load_script("migrate_history_layout")
    assert module.main(["--data-dir", str(data), "--yes"]) == 0

    g111 = _read_jsonl(data / "history" / "group" / "111.jsonl")
    g222 = _read_jsonl(data / "history" / "group" / "222.jsonl")
    assert len(g111) == 1
    assert len(g222) == 1, "不同群的相同内容不能被全局去重吞掉"


def test_migrate_dedupes_same_message_across_bots_within_group(tmp_path: Path):
    data = tmp_path
    row = {"time": 100, "message_id": 42, "user_id": 7, "raw_message": "x", "message": "x"}
    _write_jsonl(data / "history" / "bot_001" / "group" / "111.jsonl", [row])
    _write_jsonl(data / "history" / "bot_002" / "group" / "111.jsonl", [row])

    module = _load_script("migrate_history_layout")
    assert module.main(["--data-dir", str(data), "--yes"]) == 0

    merged = _read_jsonl(data / "history" / "group" / "111.jsonl")
    assert len(merged) == 1
    assert merged[0]["bot_id"] == "bot_001"


def test_migrate_dry_run_is_read_only(tmp_path: Path):
    data = tmp_path
    src = data / "history" / "bot_001" / "group" / "111.jsonl"
    _write_jsonl(src, [{"time": 100, "message_id": 1, "user_id": 7}])
    before = src.read_bytes()
    tree_before = sorted(p.relative_to(data).as_posix() for p in data.rglob("*"))

    module = _load_script("migrate_history_layout")
    assert module.main(["--data-dir", str(data), "--dry-run"]) == 0

    assert not (data / "history" / "group").exists(), "dry-run 不应创建目标目录"
    assert not list(data.glob("history_legacy_*")), "dry-run 不应创建备份目录"
    assert src.read_bytes() == before
    assert sorted(p.relative_to(data).as_posix() for p in data.rglob("*")) == tree_before


def test_migrate_aborts_on_bad_lines_without_changes(tmp_path: Path):
    data = tmp_path
    src = data / "history" / "bot_001" / "group" / "111.jsonl"
    _write_jsonl(
        src,
        [{"time": 100, "message_id": 1, "user_id": 7}, "{broken json"],
    )

    module = _load_script("migrate_history_layout")
    assert module.main(["--data-dir", str(data), "--yes"]) == 2
    assert src.exists(), "中止时来源文件不能移动"
    assert not (data / "history" / "group").exists()
    assert not list(data.glob("history_legacy_*"))


def test_migrate_skip_invalid_lines_migrates_valid_rows(tmp_path: Path):
    data = tmp_path
    _write_jsonl(
        data / "history" / "bot_001" / "group" / "111.jsonl",
        [{"time": 100, "message_id": 1, "user_id": 7}, "{broken json"],
    )

    module = _load_script("migrate_history_layout")
    assert module.main(["--data-dir", str(data), "--yes", "--skip-invalid-lines"]) == 0
    rows = _read_jsonl(data / "history" / "group" / "111.jsonl")
    assert len(rows) == 1
    assert list(data.glob("history_legacy_*")), "成功迁移后来源应进备份目录"


@_non_interactive
def test_migrate_requires_confirmation_in_non_interactive(tmp_path: Path):
    data = tmp_path
    src = data / "history" / "bot_001" / "group" / "111.jsonl"
    _write_jsonl(src, [{"time": 100, "message_id": 1, "user_id": 7}])

    module = _load_script("migrate_history_layout")
    assert module.main(["--data-dir", str(data)]) == 1
    assert src.exists()
    assert not (data / "history" / "group").exists()


def test_migrate_is_idempotent(tmp_path: Path):
    data = tmp_path
    _write_jsonl(
        data / "history" / "bot_001" / "group" / "111.jsonl",
        [{"time": 100, "message_id": 1, "user_id": 7}],
    )
    module = _load_script("migrate_history_layout")
    assert module.main(["--data-dir", str(data), "--yes"]) == 0
    target = data / "history" / "group" / "111.jsonl"
    content = target.read_text(encoding="utf-8")

    assert module.main(["--data-dir", str(data), "--yes"]) == 0
    assert target.read_text(encoding="utf-8") == content
    assert len(list(data.glob("history_legacy_*"))) == 1


# ── clean_knowledge_db ────────────────────────────────────────────────


def _make_song_db(path: Path, *, wal: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE songs ("
        "uuid TEXT PRIMARY KEY, introduction TEXT, lyrics TEXT, uploader TEXT, "
        "singers TEXT, lyricist TEXT, composer TEXT, arranger TEXT, mixer TEXT, "
        "tuner TEXT, mastering TEXT, pv TEXT, illustrator TEXT)"
    )
    conn.execute(
        "INSERT INTO songs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "u1",
            "'''简介''' {{VOCALOID中文殿堂曲题头}}",
            "<ref>注释</ref><center>歌词</center>",
            "[[上传者]]",
            "[[洛天依]]",
            "[[词作]]",
            "[[曲作]]",
            "[[编曲]]",
            "[[混音]]",
            "[[调教]]",
            "[[母带]]",
            "[[PV]]",
            "[[曲绘]]",
        ),
    )
    conn.commit()
    return conn


def test_clean_backup_uses_sqlite_api_with_uncheckpointed_wal(tmp_path: Path):
    """WAL 里已提交但未 checkpoint 的行也必须出现在备份里。"""
    db_path = tmp_path / "knowledge_db.db"
    writer = _make_song_db(db_path, wal=True)
    writer.execute(
        "INSERT INTO songs (uuid, introduction, lyrics) VALUES ('wal-row', '简介', '歌词')"
    )
    writer.commit()
    # 不执行 checkpoint, 保持 WAL 中有已提交数据
    assert db_path.with_name(db_path.name + "-wal").exists()

    module = _load_script("clean_knowledge_db")
    backup = tmp_path / "snapshot.db"
    rows = module._backup_database(db_path, backup)

    assert rows == 2
    check = sqlite3.connect(backup)
    try:
        uuids = {row[0] for row in check.execute("SELECT uuid FROM songs")}
    finally:
        check.close()
    assert "wal-row" in uuids
    writer.close()


def test_clean_backup_paths_never_overwrite(tmp_path: Path):
    module = _load_script("clean_knowledge_db")
    db_path = tmp_path / "knowledge_db.db"
    db_path.write_bytes(b"x")
    first = module._unique_backup_path(db_path)
    first.write_bytes(b"old")
    second = module._unique_backup_path(db_path)
    assert first != second
    assert first.read_bytes() == b"old"


def test_clean_dry_run_does_not_write_or_backup(tmp_path: Path):
    db_path = tmp_path / "knowledge_db.db"
    conn = _make_song_db(db_path)
    conn.close()
    before = db_path.read_bytes()

    module = _load_script("clean_knowledge_db")
    assert module.main(["--db", str(db_path), "--dry-run"]) == 0

    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-*"))


@_non_interactive
def test_clean_requires_confirmation_in_non_interactive(tmp_path: Path):
    db_path = tmp_path / "knowledge_db.db"
    conn = _make_song_db(db_path)
    conn.close()
    before = db_path.read_bytes()

    module = _load_script("clean_knowledge_db")
    assert module.main(["--db", str(db_path)]) == 1
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-*"))


def test_clean_writes_after_backup_and_reports(tmp_path: Path):
    db_path = tmp_path / "knowledge_db.db"
    conn = _make_song_db(db_path)
    conn.close()

    module = _load_script("clean_knowledge_db")
    assert module.main(["--db", str(db_path), "--yes"]) == 0

    check = sqlite3.connect(db_path)
    try:
        intro, lyrics = check.execute("SELECT introduction, lyrics FROM songs").fetchone()
    finally:
        check.close()
    assert intro is not None and "'''" not in intro
    assert lyrics is not None and "<ref>" not in lyrics
    backups = list(tmp_path.glob("knowledge_db.db.bak-*"))
    assert len(backups) == 1


# ── sync_vcpedia ──────────────────────────────────────────────────────


def _vcpedia_exit_code(result: dict) -> int:
    module = _load_script("sync_vcpedia")
    with _patched_attrs(
        module,
        load_music_knowledge_config=lambda: {},
        sync_vcpedia_new_songs=lambda _cfg: result,
    ):
        return module.main([])


def test_sync_vcpedia_exit_codes():
    assert _vcpedia_exit_code({"added": ["a"], "failed": [], "skipped": 1}) == 0
    assert _vcpedia_exit_code({"added": ["a"], "failed": ["b"]}) == 1
    assert _vcpedia_exit_code({"added": [], "failed": ["b"]}) == 2


def test_sync_vcpedia_config_failure_returns_nonzero():
    module = _load_script("sync_vcpedia")

    def boom():
        raise RuntimeError("config missing")

    with _patched_attrs(module, load_music_knowledge_config=boom):
        assert module.main([]) == 2


# ── sync_songs ────────────────────────────────────────────────────────


class _Adapter:
    def __init__(self, name: str) -> None:
        self.name = name


def _sync_songs_exit_code(sources, *, adapters=None, argv=()) -> int:
    module = _load_script("sync_songs")
    import mohobot.music_knowledge.sync as sync_module

    built = adapters if adapters is not None else [_Adapter("a"), _Adapter("b")]

    class _Report:
        def __init__(self):
            self.sources = sources

        def summary_text(self) -> str:
            return "report"

    with _patched_attrs(module, load_music_config=lambda: {"sync": {"mode": "new_only"}}):
        with _patched_attrs(
            sync_module,
            build_adapters=lambda _cfg: built,
            set_db_location=lambda *_a: None,
            sync_all=lambda *_a, **_k: _Report(),
        ):
            return module.main(list(argv))


def _stat(source: str, failed: int = 0, aborted: str = ""):
    from types import SimpleNamespace

    return SimpleNamespace(source=source, failed=failed, aborted=aborted)


def test_sync_songs_exit_codes():
    ok = [_stat("a"), _stat("b")]
    partial = [_stat("a"), _stat("b", failed=3)]
    failed = [_stat("a", aborted="boom"), _stat("b", failed=1)]
    assert _sync_songs_exit_code(ok) == 0
    assert _sync_songs_exit_code(partial) == 1
    assert _sync_songs_exit_code(failed) == 2


def test_sync_songs_no_adapters_is_failure():
    assert _sync_songs_exit_code([], adapters=[], argv=["--source", "nope"]) == 2
