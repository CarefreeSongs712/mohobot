"""mohobot 数据库 — 独立 SQLite (SQLAlchemy)。

表结构借鉴 Agent-LuoTianyi 的设计, 但为 mohobot 独立使用,
不与任何外部项目共享数据库文件。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine, event, text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


# ── 用户 ──────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    uuid = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String, unique=True, index=True, nullable=False)
    password = Column(String, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.now)
    last_login = Column(DateTime, nullable=True)
    nickname = Column(String, default="你")
    description = Column(Text, default="")
    context_summary = Column(Text, default="")
    context_memory_count = Column(Integer, default=0)
    all_memory_count = Column(Integer, default=0)
    auth_token = Column(String, nullable=True)
    preferences = Column(Text, default="{}")
    affection_score = Column(Integer, default=0)
    affection_total_gained = Column(Integer, default=0)


# ── 会话记录 ──────────────────────────────────────────────────


class Conversation(Base):
    __tablename__ = "conversations"

    uuid = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.uuid"), nullable=False, index=True)
    character_id = Column(String, nullable=False, default="bot", server_default="bot", index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    source = Column(String, nullable=False)  # user / agent
    type = Column(String, nullable=False)    # text / image / sing
    content = Column(Text, nullable=False)
    meta_data = Column(Text, nullable=True)


class ConversationContext(Base):
    __tablename__ = "conversation_contexts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.uuid"), nullable=False)
    character_id = Column(String, nullable=False, default="bot", server_default="bot")
    context_summary = Column(Text, default="")
    context_memory_count = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("user_id", "character_id", name="uq_conversation_context_user_character"),
    )


# ── 记忆正本 ──────────────────────────────────────────────────


class AgentMemoryRecord(Base):
    __tablename__ = "agent_memory_records"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_character_id = Column(String, nullable=False, index=True)
    subject_user_id = Column(String, nullable=True, index=True)
    memory_type = Column(String, nullable=False, index=True)
    visibility = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False, index=True)
    content = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    importance = Column(Float, default=0.5)
    confidence = Column(Float, default=1.0)
    emotional_valence = Column(Float, nullable=True)
    happened_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    last_accessed_at = Column(DateTime, nullable=True)
    meta_data = Column(Text, nullable=True)


class MemoryChunkRecord(Base):
    __tablename__ = "memory_chunks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    memory_record_id = Column(String, ForeignKey("agent_memory_records.id"), nullable=False, index=True)
    chunk_text = Column(Text, nullable=False)
    chunk_type = Column(String, default="content")
    embedding_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    meta_data = Column(Text, nullable=True)


# ── 引擎管理 ──────────────────────────────────────────────────

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def init_sql_db(db_folder: str = "data/database", db_file: str = "mohobot.db") -> Engine:
    """初始化 SQLite 引擎(独立库, 参数与 Agent-LuoTianyi 风格一致)。"""
    global _engine, _SessionLocal
    if not os.path.exists(db_folder):
        os.makedirs(db_folder, exist_ok=True)

    url = f"sqlite:///{os.path.join(db_folder, db_file)}"
    _engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30},
        isolation_level="AUTOCOMMIT",
    )

    @event.listens_for(_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    Base.metadata.create_all(bind=_engine)
    _migrate_sqlite_schema(_engine)
    return _engine


def _migrate_sqlite_schema(db_engine: Engine) -> None:
    """对已有数据库做增量迁移(旧库可能缺少新列)。"""
    with db_engine.begin() as connection:
        # conversations 表可能缺少 character_id(旧库)
        conversation_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(conversations)").fetchall()
        }
        if "character_id" not in conversation_columns:
            connection.exec_driver_sql(
                "ALTER TABLE conversations ADD COLUMN character_id VARCHAR NOT NULL DEFAULT 'bot'"
            )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_conversations_character_id ON conversations (character_id)"
        )
        # conversations 表可能缺少 meta_data(旧库)
        if "meta_data" not in conversation_columns:
            connection.exec_driver_sql("ALTER TABLE conversations ADD COLUMN meta_data TEXT")


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("SQL database not initialized, call init_sql_db() first")
    return _engine


def new_session():
    if _SessionLocal is None:
        raise RuntimeError("SQL database not initialized, call init_sql_db() first")
    return _SessionLocal()
