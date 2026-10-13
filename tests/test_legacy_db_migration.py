"""旧库迁移回归 — 真实 schema + 真实 WAL + 真实 DatabaseManager 启动路径。

覆盖 Issue #2。

缺陷：``DatabaseManager.__init__`` 检测到旧库 ``luotianyi.db`` 时直接
``os.rename`` 主库文件。SQLite 默认 WAL 模式，异常退出会留下含**已提交事务**的
``luotianyi.db-wal``；改名只带走主文件，新库看不到这些事务，最近的会话记录静默
丢失；被落下的 ``-wal`` 还成了与主库脱钩的孤儿文件。

子进程用 ``os._exit(0)`` 退出，刻意不做 checkpoint、不关闭连接，以复现真实异常
退出后残留 WAL 的状态。
"""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 子进程: 建真实 schema -> 写一条并 checkpoint -> 再写一条(留在 WAL) -> 硬退出
_SEED_WITH_WAL = """
import os, sys
sys.path.insert(0, os.getcwd())
from mohobot.db.database_manager import DatabaseManager
folder, first, second = sys.argv[1], sys.argv[2], sys.argv[3]
m = DatabaseManager(folder, "luotianyi.db")
m.add_conversation(123, "bot_001", "user", first)
with m._engine.connect() as c:
    c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
m.add_conversation(123, "bot_001", "agent", second)
os._exit(0)
"""

# 子进程: 只写一条并正常 checkpoint(用于失败回退用例)
_SEED_PLAIN = """
import os, sys
sys.path.insert(0, os.getcwd())
from mohobot.db.database_manager import DatabaseManager
folder, content = sys.argv[1], sys.argv[2]
m = DatabaseManager(folder, "luotianyi.db")
m.add_conversation(123, "bot_001", "user", content)
with m._engine.connect() as c:
    c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
os._exit(0)
"""


def _seed(folder, script, *args):
    """在隔离子进程里造真实库; 断言子进程成功退出。"""
    run = subprocess.run(
        [sys.executable, "-c", script, str(folder), *args],
        cwd=str(ROOT), text=True, capture_output=True,
    )
    assert run.returncode == 0, (run.returncode, run.stdout, run.stderr)


def _contents(db_path, readonly=True):
    target = f"file:{db_path}?mode=ro" if readonly else str(db_path)
    conn = sqlite3.connect(target, uri=readonly)
    try:
        rows = conn.execute(
            "SELECT content FROM conversations ORDER BY timestamp, rowid"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _main_file_only(db_path, workdir):
    """把主库文件单独复制出来再读, 从而**看不到 WAL** —— 用于证明存在未 checkpoint
    的已提交事务。直接读原文件是证明不了这一点的: 普通连接会透明地读到 WAL 内容。"""
    copy = Path(workdir) / "main-only.db"
    shutil.copy2(db_path, copy)
    return _contents(copy)


def _make_manager(folder, db_file="mohobot.db"):
    from mohobot.db.database_manager import DatabaseManager
    return DatabaseManager(str(folder), db_file)


def test_legacy_migration_preserves_commits_that_were_only_in_wal():
    """改名迁移后, 原本只存在于 WAL 里的已提交记录必须还在。"""
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as probe:
        folder = Path(td)
        _seed(folder, _SEED_WITH_WAL, "CHECKPOINTED_OLD", "COMMITTED_WAL_NEW")
        legacy = folder / "luotianyi.db"
        wal = folder / "luotianyi.db-wal"

        # 前置条件: 第二行确实还没 checkpoint 进主文件(只存在 WAL 里)。
        assert wal.exists() and wal.stat().st_size > 32, "未构造出残留 WAL"
        assert _main_file_only(legacy, probe) == ["CHECKPOINTED_OLD"], "前置条件不成立"
        assert _contents(legacy) == ["CHECKPOINTED_OLD", "COMMITTED_WAL_NEW"]

        manager = _make_manager(folder)
        try:
            target = folder / "mohobot.db"
            assert target.exists(), "迁移应发布目标库"
            after = _contents(target, readonly=False)
        finally:
            manager._engine.dispose()

        # 关键断言: 只 rename 主文件时这里只会剩 CHECKPOINTED_OLD。
        assert after == ["CHECKPOINTED_OLD", "COMMITTED_WAL_NEW"], after


def test_legacy_migration_archives_whole_db_family_for_rollback():
    """迁移成功后旧库三件套整体留档, 且备份本身仍可完整读出(可回滚)。"""
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        _seed(folder, _SEED_WITH_WAL, "CHECKPOINTED_OLD", "COMMITTED_WAL_NEW")
        manager = _make_manager(folder)
        try:
            assert (folder / "mohobot.db").exists()
        finally:
            manager._engine.dispose()

        assert not (folder / "luotianyi.db").exists(), "旧库原名应已整体移入备份目录"
        backups = sorted(p for p in folder.glob("luotianyi.db.pre-migration-*") if p.is_dir())
        assert backups, "应保留旧库备份目录"
        # 备份目录内保留原文件名, 因此 SQLite 仍能把 -wal 与主库配对; 只加后缀改名
        # 会让备份里的 WAL 脱钩, 回滚时同样丢数据。
        assert (backups[0] / "luotianyi.db").exists()
        assert _contents(backups[0] / "luotianyi.db") == ["CHECKPOINTED_OLD", "COMMITTED_WAL_NEW"]


def test_migration_failure_is_fail_closed_and_creates_no_empty_new_db():
    """迁移无法完成时不得静默新建空库: 返回 False, 旧库与数据保持可用。

    这条断言的是本次新引入的失败语义(旧实现改名失败后直接 ``init_sql_db`` 建新库)。
    """
    from mohobot.db.database_manager import migrate_legacy_database

    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        _seed(folder, _SEED_PLAIN, "ONLY_ROW")
        legacy, target = folder / "luotianyi.db", folder / "mohobot.db"
        # 注入失败: 用目录占住迁移临时路径, 让 backup 无法创建目标文件。
        (folder / "mohobot.db.migrating").mkdir()

        assert migrate_legacy_database(str(folder), str(legacy), str(target)) is False
        assert not target.exists(), "迁移失败时不得发布新库"
        assert legacy.exists() and _contents(legacy) == ["ONLY_ROW"]


def test_manager_falls_back_to_legacy_db_when_migration_fails():
    """迁移失败时 DatabaseManager 必须落到旧库上继续工作, 而不是对着空库。"""
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        _seed(folder, _SEED_PLAIN, "ONLY_ROW")
        (folder / "mohobot.db.migrating").mkdir()

        manager = _make_manager(folder)
        try:
            assert not (folder / "mohobot.db").exists(), "不得新建空库掩盖失败"
            assert _contents(folder / "luotianyi.db") == ["ONLY_ROW"]
            with manager._engine.connect() as conn:
                count = conn.exec_driver_sql("SELECT COUNT(*) FROM conversations").scalar()
            assert count == 1, count
        finally:
            manager._engine.dispose()


def main():
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
    print("\nALL LEGACY MIGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
