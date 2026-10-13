"""数据库管理器 — 用户/会话/记忆的读写操作。

history 从 JSONL 迁移到数据库: 写入 conversations 表(mohobot 独立 SQLite)。
用户以 QQ 号为外部标识,username 存为 "qq_{qq}"。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import text

from mohobot.db import sql_database as sqldb
from mohobot.db.sql_database import Conversation, ConversationContext, User

LEGACY_DB_FILE = "luotianyi.db"


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """各用户表的行数快照, 用于迁移前后比对。"""
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    counts: dict[str, int] = {}
    for name in names:
        quoted = '"' + str(name).replace('"', '""') + '"'
        counts[str(name)] = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
    return counts


def _verify_migrated(path: str, expected: dict[str, int]) -> None:
    """校验迁移产物: 完整性检查 + 行数一致。任何异常都由调用方处理为迁移失败。"""
    conn = sqlite3.connect(path)
    try:
        state = conn.execute("PRAGMA quick_check").fetchone()
        if not state or str(state[0]).strip().lower() != "ok":
            raise ValueError(f"quick_check 未通过: {state!r}")
        actual = _table_counts(conn)
    finally:
        conn.close()
    if actual != expected:
        raise ValueError(f"迁移后行数不一致: {expected} -> {actual}")


def _fsync_path(path: str) -> None:
    """尽力 fsync(文件或目录); 不支持时只告警, 不让迁移因此失败。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        logger.warning(f"fsync 打开失败({path}): {e}")
        return
    try:
        os.fsync(fd)
    except OSError as e:
        logger.warning(f"fsync 失败({path}): {e}")
    finally:
        os.close(fd)


def _archive_legacy_files(legacy: str) -> None:
    """迁移成功后把旧库三件套整体移入备份目录, 便于回滚。

    必须放进**同一个目录并保留原文件名**: SQLite 是靠 ``<主库名>-wal`` 找日志的,
    若只给三个文件各加后缀, 备份里的 WAL 就不再配对, 回滚时会丢同样的数据。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = f"{legacy}.pre-migration-{stamp}"
    suffix = 0
    while os.path.exists(backup_dir):
        suffix += 1
        backup_dir = f"{legacy}.pre-migration-{stamp}-{suffix}"
    try:
        os.makedirs(backup_dir)
    except OSError as e:
        logger.warning(f"旧库留档目录创建失败({backup_dir}): {e}")
        return
    for extra in ("", "-wal", "-shm"):
        source = legacy + extra
        if not os.path.exists(source):
            continue
        try:
            os.replace(source, os.path.join(backup_dir, os.path.basename(source)))
        except OSError as e:
            logger.warning(f"旧库留档失败({source}): {e}")


def migrate_legacy_database(db_folder: str, legacy: str, target: str) -> bool:
    """把旧库(含 WAL 中已提交事务)迁移到新文件名。返回是否已成功发布新库。

    旧实现直接 ``os.rename`` 主库文件: WAL 模式下异常退出会留下
    ``luotianyi.db-wal``(其中有已提交事务), 改名后新库读不到它, 最近的会话
    记录静默丢失。这里改用 SQLite 官方 backup 接口(它读取的是完整逻辑库,
    自然包含 WAL 中已提交的内容), 校验通过后再原子发布。

    失败时**不新建空库**: 返回 False, 由调用方继续使用旧库文件名, 数据保持可用。
    """
    tmp = target + ".migrating"
    if os.path.isdir(tmp):
        logger.error(f"数据库迁移失败: 临时路径被目录占用 {tmp}")
        return False
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        src = sqlite3.connect(legacy)
        try:
            src.execute("PRAGMA busy_timeout=15000")
            expected = _table_counts(src)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        _verify_migrated(tmp, expected)
        _fsync_path(tmp)
        os.replace(tmp, target)          # 同目录内原子替换
        _fsync_path(db_folder)
    except Exception as e:                # noqa: BLE001 - 任何失败都必须回退而不是丢数据
        logger.error(f"数据库迁移失败({e}); 保留旧库并继续使用 {os.path.basename(legacy)}")
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False

    _archive_legacy_files(legacy)
    return True


class DatabaseManager:
    """封装独立 SQLite 的读写。"""

    def __init__(self, db_folder: str = "data/database", db_file: str = "mohobot.db"):
        # 旧库迁移: 早期版本名为 luotianyi.db, 迁移到新名后继续使用
        os.makedirs(db_folder, exist_ok=True)
        legacy = os.path.join(db_folder, LEGACY_DB_FILE)
        target = os.path.join(db_folder, db_file)
        effective_file = db_file
        if db_file != LEGACY_DB_FILE:
            if os.path.exists(legacy) and not os.path.exists(target):
                if migrate_legacy_database(db_folder, legacy, target):
                    logger.info(f"数据库迁移: {LEGACY_DB_FILE} → {db_file}(含 WAL 已提交内容)")
                else:
                    # 迁移未完成时继续用旧库文件: 绝不静默新建空库让用户以为数据丢了。
                    effective_file = LEGACY_DB_FILE
            elif os.path.exists(legacy) and os.path.exists(target):
                logger.info(f"检测到旧库 {LEGACY_DB_FILE}, 但 {db_file} 已存在, 跳过迁移")
        self._engine = sqldb.init_sql_db(db_folder, effective_file)
        logger.info(f"DatabaseManager initialized: {db_folder}/{effective_file}")

    # ── 用户 ─────────────────────────────────────────────────

    def get_or_create_user(self, qq: int | str, nickname: str = "") -> User:
        """按 QQ 号查找或创建用户记录。username = 'qq_{qq}'。"""
        qq_str = str(qq)
        username = f"qq_{qq_str}"
        db = sqldb.new_session()
        try:
            user = db.query(User).filter(User.username == username).first()
            if user is None:
                user = User(
                    username=username,
                    password="",  # mohobot 用户无密码
                    nickname=nickname or f"用户{qq_str}",
                )
                db.add(user)
                db.commit()
                db.refresh(user)
            elif nickname and user.nickname in ("你", f"用户{qq_str}"):
                user.nickname = nickname
                db.commit()
            return user
        finally:
            db.close()

    def get_user(self, qq: int | str) -> User | None:
        qq_str = str(qq)
        db = sqldb.new_session()
        try:
            return db.query(User).filter(User.username == f"qq_{qq_str}").first()
        finally:
            db.close()

    def get_user_description(self, qq: int | str) -> str:
        """用户画像(users.description)。"""
        user = self.get_user(qq)
        return user.description if user else ""

    def update_user_description(self, qq: int | str, description: str, commit: bool = True) -> None:
        db = sqldb.new_session()
        try:
            user = db.query(User).filter(User.username == f"qq_{qq}").first()
            if user is not None:
                user.description = description
                if commit:
                    db.commit()
        finally:
            db.close()

    def get_user_preferences(self, qq: int | str) -> dict[str, Any]:
        user = self.get_user(qq)
        if user is None or not user.preferences:
            return {}
        try:
            return json.loads(user.preferences)
        except json.JSONDecodeError:
            return {}

    # ── 会话记录(history) ────────────────────────────────────

    def add_conversation(
        self,
        qq: int | str,
        character_id: str,
        source: str,          # user / agent
        content: str,
        msg_type: str = "text",
        meta_data: dict | None = None,
        speaker_id: str | None = None,
        speaker_nickname: str | None = None,
    ) -> str:
        """写一条消息到 conversations 表(取代 JSONL history)。

        speaker_* 为实际发言人归属: 群聊时 qq 传群号、speaker_id 传发言人 QQ;
        私聊两者一致; agent 消息不传(character_id 已标识 bot)。
        """
        user = self.get_or_create_user(qq)
        db = sqldb.new_session()
        try:
            row = Conversation(
                user_id=user.uuid,
                character_id=character_id,
                source=source,
                type=msg_type,
                content=content,
                speaker_id=speaker_id,
                speaker_nickname=speaker_nickname,
                meta_data=json.dumps(meta_data, ensure_ascii=False) if meta_data else None,
            )
            db.add(row)
            db.commit()
            return row.uuid
        finally:
            db.close()

    def get_recent_conversations(
        self,
        qq: int | str,
        character_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """读取最近的会话记录(按时间升序)。"""
        user = self.get_user(qq)
        if user is None:
            return []
        db = sqldb.new_session()
        try:
            rows = (
                db.query(Conversation)
                .filter(Conversation.user_id == user.uuid,
                        Conversation.character_id == character_id)
                .order_by(Conversation.timestamp.desc())
                .limit(limit)
                .all()
            )
            rows.reverse()
            return [
                {
                    "uuid": r.uuid,
                    "source": r.source,
                    "type": r.type,
                    "content": r.content,
                    "speaker_id": r.speaker_id,
                    "speaker_nickname": r.speaker_nickname,
                    "timestamp": r.timestamp,
                }
                for r in rows
            ]
        finally:
            db.close()

    # ── 上下文状态 ───────────────────────────────────────────

    def get_context_snapshot(self, qq: int | str, character_id: str) -> dict[str, Any]:
        """读取 conversation_contexts 摘要 + 最近对话,构造提示词载荷。"""
        user = self.get_user(qq)
        if user is None:
            return {"summary": "", "recent_conversation": []}
        db = sqldb.new_session()
        try:
            ctx = (
                db.query(ConversationContext)
                .filter(ConversationContext.user_id == user.uuid,
                        ConversationContext.character_id == character_id)
                .first()
            )
            summary = ctx.context_summary if ctx else ""
            count = ctx.context_memory_count if ctx else 0
        finally:
            db.close()

        recent = self.get_recent_conversations(qq, character_id, limit=30)
        lines = [
            f"{'user' if r['source'] == 'user' else 'agent'}: {r['content']}"
            for r in recent
        ]
        return {
            "summary": summary,
            "context_memory_count": count,
            "recent_conversation": lines,
        }

    def get_context_count(self, qq: int | str, character_id: str) -> int:
        user = self.get_user(qq)
        if user is None:
            return 0
        db = sqldb.new_session()
        try:
            return (
                db.query(Conversation)
                .filter(Conversation.user_id == user.uuid,
                        Conversation.character_id == character_id)
                .count()
            )
        finally:
            db.close()

    def compact_conversation_context(
        self,
        qq: int | str,
        character_id: str,
        summary: str,
        keep_recent: int = 30,
    ) -> None:
        """压缩上下文:写摘要,保留最近 keep_recent 条。"""
        user = self.get_or_create_user(qq)
        db = sqldb.new_session()
        try:
            ctx = (
                db.query(ConversationContext)
                .filter(ConversationContext.user_id == user.uuid,
                        ConversationContext.character_id == character_id)
                .first()
            )
            if ctx is None:
                ctx = ConversationContext(
                    user_id=user.uuid, character_id=character_id,
                    context_summary=summary,
                )
                db.add(ctx)
            else:
                ctx.context_summary = summary
            db.commit()
        finally:
            db.close()

        # 保留最近 keep_recent 条(旧的仍在表中,只是摘要已覆盖)
        recent = self.get_recent_conversations(qq, character_id, limit=keep_recent)
        logger.debug(
            f"Compacted context for {qq}/{character_id}: summary={len(summary)} chars, "
            f"keep={len(recent)} recent"
        )

    def write_memory_update(self, qq: int | str, cmd_type: str, content: str, cmd_uuid: str | None = None) -> None:
        """(legacy 兼容)记录记忆更新命令到 memory_update_records 表。"""
        user = self.get_or_create_user(qq)
        db = sqldb.new_session()
        try:
            from mohobot.db.sql_database import Base
            # 该表可能不存在(旧库);这里用原生 SQL 写入,避免引入额外模型
            db.execute(
                text(
                    "INSERT INTO memory_update_records (update_cmd_uuid, user_id, update_command, created_at) "
                    "VALUES (:u, :uid, :cmd, :ts)"
                ),
                {
                    "u": cmd_uuid or str(uuid.uuid4()),
                    "uid": user.uuid,
                    "cmd": json.dumps({"uuid": cmd_uuid, "content": content, "type": cmd_type},
                                      ensure_ascii=False),
                    "ts": datetime.now(),
                },            )
            db.commit()
        except Exception as e:
            logger.debug(f"write_memory_update skipped: {e}")
        finally:
            db.close()
