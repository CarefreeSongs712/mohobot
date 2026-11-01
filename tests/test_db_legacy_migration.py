"""issue #2 回归: 旧库迁移不得丢 WAL 中的已提交事务。

复现路径(与 issue 描述一致):
1. 旧库 luotianyi.db 写入 checkpoint 过的记录;
2. 再写入一条已提交但未 checkpoint 的记录(WAL 残留);
3. DatabaseManager(db_file="mohobot.db") 触发迁移;
4. mohobot.db 中两条都必须可见(旧 os.rename 只剩 checkpoint 记录)。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _build_legacy_with_wal(root: Path):
    """建旧库: 真实 schema(经 sql_database) + 1 条已提交 WAL 残留记录。

    返回 (保持打开的 raw 连接, 迁移前合并视图行数)。
    连接必须保持打开——关闭会 checkpoint, WAL 残留场景就消失了。
    """
    import mohobot.db.sql_database as sqldb

    legacy_path = root / "luotianyi.db"
    engine = sqldb.init_sql_db(str(root), "luotianyi.db")
    session = sqldb.new_session()
    try:
        session.add(sqldb.Conversation(
            uuid="ck", user_id="4242", character_id="bot_001",
            type="text", source="user", content="checkpoint 过的记录"))
        session.commit()
    finally:
        session.close()
    engine.dispose()  # 关闭池化连接, 让 WAL 状态可控

    # WAL 残留: 独立连接写一条已提交记录, 保持打开(不 checkpoint)
    con = sqlite3.connect(str(legacy_path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "INSERT INTO conversations (uuid, user_id, character_id, type, source, content) "
        "VALUES ('wal', '4242', 'bot_001', 'text', 'user', 'WAL 里的已提交记录')")
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    return con, n


def test_migration_preserves_wal_committed_row() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        con, before = _build_legacy_with_wal(root)
        try:
            assert before == 2, before
            from mohobot.db.database_manager import DatabaseManager

            db = DatabaseManager(db_folder=str(root), db_file="mohobot.db")
            try:
                con2 = sqlite3.connect(str(root / "mohobot.db"))
                try:
                    rows = con2.execute(
                        "SELECT uuid FROM conversations ORDER BY uuid").fetchall()
                finally:
                    con2.close()
                uuids = [r[0] for r in rows]
                assert "ck" in uuids and "wal" in uuids, uuids
                # 旧三件套已备份
                baks = list(root.glob("luotianyi.db.bak-*"))
                assert baks, "旧三件套应已备份"
                assert not (root / "mohobot.db.migrating").exists(), "无 tmp 残留"
                assert not (root / "luotianyi.db").exists(), "旧库应已改名备份"
            finally:
                db._engine.dispose()
        finally:
            con.close()
    print("[DB1] WAL 已提交记录随迁移保留 OK")


def test_migration_fail_closed_on_corrupt_legacy() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "luotianyi.db").write_bytes(b"this is not a sqlite database" * 10)
        from mohobot.db.database_manager import DatabaseManager

        with pytest.raises(ValueError, match="旧库迁移失败"):
            DatabaseManager(db_folder=str(root), db_file="mohobot.db")
        # fail-closed: 不得静默创建空新库
        assert not (root / "mohobot.db").exists()
        # 现场保留
        assert (root / "luotianyi.db").exists()
    print("[DB2] 损坏旧库 fail-closed OK")


def test_migration_idempotent_and_absent_legacy() -> None:
    from mohobot.db.database_manager import DatabaseManager

    # 无旧库: 直接新建, 不报错
    with tempfile.TemporaryDirectory() as td:
        db = DatabaseManager(db_folder=td, db_file="mohobot.db")
        db._engine.dispose()
    # 迁移成功后再次启动: 旧库已改名 → 不再迁移, 直接打开
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        con, _ = _build_legacy_with_wal(root)
        try:
            from mohobot.db.database_manager import DatabaseManager

            DatabaseManager(db_folder=str(root), db_file="mohobot.db")._engine.dispose()
            db2 = DatabaseManager(db_folder=str(root), db_file="mohobot.db")
            db2._engine.dispose()
            assert (root / "mohobot.db").exists()
        finally:
            con.close()
    print("[DB3] 幂等/无旧库场景 OK")
