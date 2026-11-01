"""数据库管理器 — 用户/会话/记忆的读写操作。

history 从 JSONL 迁移到数据库: 写入 conversations 表(mohobot 独立 SQLite)。
用户以 QQ 号为外部标识,username 存为 "qq_{qq}"。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import text

from mohobot.db import sql_database as sqldb
from mohobot.db.sql_database import Conversation, ConversationContext, User


class DatabaseManager:
    """封装独立 SQLite 的读写。"""

    def __init__(self, db_folder: str = "data/database", db_file: str = "mohobot.db"):
        self._migrate_legacy_db(db_folder, db_file)
        self._engine = sqldb.init_sql_db(db_folder, db_file)
        logger.info(f"DatabaseManager initialized: {db_folder}/{db_file}")

    def _migrate_legacy_db(self, db_folder: str, db_file: str) -> None:
        """旧库迁移: luotianyi.db → db_file (issue #2)。

        旧库可能残留未 checkpoint 的 -wal(异常退出/SIGKILL): 直接 os.rename
        只搬主库文件, 已提交事务留在旧 -wal 里, 新库静默丢最近记录。
        改用 SQLite backup API 完整迁移(会读取 WAL 中的已提交内容),
        quick_check + 核心表行数校验通过后原子替换; 旧 .db/-wal/-shm/-journal
        备份保留(SIGKILL 后 close 会 checkpoint 进主库, 故备份集以实际存在
        的文件为准); 校验失败 fail-closed(拒绝静默新建空库, 现场留给人工处理)。
        假设迁移期间无其他进程在写旧库(启动时单进程, README 运维流成立);
        旧进程若仍活跃写入, 行数校验会拦截(fail-closed)。
        """
        import sqlite3
        import time as _time

        legacy = os.path.join(db_folder, "luotianyi.db")
        target = os.path.join(db_folder, db_file)
        if db_file == "luotianyi.db" or not os.path.exists(legacy) or os.path.exists(target):
            return
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        tmp = target + ".migrating"
        if os.path.exists(tmp):
            # 上次中断残留: 整体重来(合法半写 tmp 会被本次迁移覆盖, 安全)
            try:
                os.remove(tmp)
            except OSError:
                pass
        logger.info(f"检测到旧库 {legacy}, 开始迁移 → {target} (SQLite backup API)")
        try:
            src = sqlite3.connect(legacy)
            try:
                check = src.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise ValueError(f"旧库 quick_check 未通过: {check!r}")
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst)
                    dst.commit()
                finally:
                    dst.close()
            finally:
                src.close()
            # 行数一致性校验: 旧库存在的核心表在副本中行数必须一致
            src2 = sqlite3.connect(legacy)
            try:
                dst2 = sqlite3.connect(tmp)
                try:
                    src_tables = {
                        r[0] for r in src2.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'")
                    }
                    for table in ("conversations", "users"):
                        if table in src_tables:
                            a = src2.execute(
                                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                            b = dst2.execute(
                                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                            if a != b:
                                raise ValueError(
                                    f"迁移校验失败: {table} 行数 {a} → {b}")
                finally:
                    dst2.close()
            finally:
                src2.close()
            os.replace(tmp, target)
        except (sqlite3.Error, ValueError, OSError) as e:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise ValueError(
                f"旧库迁移失败, 已中止启动以保护数据: {e}; "
                f"请手动处理 {legacy}(及其 -wal/-shm)后重试"
            ) from e
        for suffix in ("", "-wal", "-shm", "-journal"):
            srcf = legacy + suffix
            if os.path.exists(srcf):
                try:
                    os.replace(srcf, f"{legacy}.bak-{stamp}{suffix}")
                    logger.info(f"旧库备份: {srcf} → {legacy}.bak-{stamp}{suffix}")
                except OSError as e:
                    logger.warning(f"旧库备份失败({srcf}): {e}")
        logger.info(f"数据库迁移完成: luotianyi.db → {db_file} (backup API)")

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
