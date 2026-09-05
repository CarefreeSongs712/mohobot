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
        # 旧库迁移: 早期版本名为 luotianyi.db, 改名后自动迁移
        legacy = os.path.join(db_folder, "luotianyi.db")
        target = os.path.join(db_folder, db_file)
        if db_file != "luotianyi.db" and os.path.exists(legacy) and not os.path.exists(target):
            try:
                os.rename(legacy, target)
                logger.info(f"数据库迁移: luotianyi.db → {db_file}")
            except OSError as e:
                logger.warning(f"数据库迁移失败({e}), 将新建 {db_file}")
        self._engine = sqldb.init_sql_db(db_folder, db_file)
        logger.info(f"DatabaseManager initialized: {db_folder}/{db_file}")

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
