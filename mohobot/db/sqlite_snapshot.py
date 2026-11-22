"""SQLite 一致性快照/恢复工具 — 面板在线备份使用 SQLite backup API。

数据库以 WAL 模式在线写入时, 直接复制 ``.db`` 文件可能拿到不一致的页;
``sqlite3.Connection.backup`` 由 SQLite 自身完成一致性页复制, 因此面板
备份/恢复统一走本模块(issue #126: "SQLite 使用一致性备份")。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: 主库必须存在的基础表(空库也存在, 用于拒绝明显不是本系统的 SQLite 文件)。
REQUIRED_TABLES: tuple[str, ...] = ("users", "conversations")


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=30)


def validate_sqlite(path: Path, required_tables: tuple[str, ...] = ()) -> None:
    """校验 SQLite 文件完整性(quick_check)与必需表存在。失败抛 ValueError。"""
    if not path.is_file():
        raise ValueError(f"SQLite 文件不存在: {path.name}")
    conn = _connect(path)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        result = row[0] if row else None
        if result != "ok":
            raise ValueError(f"SQLite 完整性校验失败: {result!r}")
        if required_tables:
            names = {item[0] for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [name for name in required_tables if name not in names]
            if missing:
                raise ValueError(f"SQLite 缺少必要数据表: {', '.join(missing)}")
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"SQLite 文件无法读取: {exc}") from exc
    finally:
        conn.close()


def snapshot_sqlite(
    src: Path, dst: Path, *, required_tables: tuple[str, ...] = REQUIRED_TABLES
) -> None:
    """把在线写入中的 ``src`` 一致性复制到 ``dst``, 并校验副本。"""
    if not src.is_file():
        raise ValueError(f"数据库文件不存在: {src.name}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    src_conn = _connect(src)
    dst_conn = _connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    validate_sqlite(dst, required_tables)


def restore_sqlite(
    snapshot: Path, target: Path, *, required_tables: tuple[str, ...] = REQUIRED_TABLES
) -> None:
    """用 SQLite backup API 把已校验快照写回在线目标库(目标库保持同一文件)。

    调用方必须先在替换前完成快照校验; 写回失败时目标库由 SQLite 事务保护,
    但调用方仍应准备回滚快照以保证多文件恢复的一致性。
    """
    validate_sqlite(snapshot, required_tables)
    target.parent.mkdir(parents=True, exist_ok=True)
    src_conn = _connect(snapshot)
    dst_conn = _connect(target)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    validate_sqlite(target, required_tables)


__all__ = ["REQUIRED_TABLES", "snapshot_sqlite", "restore_sqlite", "validate_sqlite"]
