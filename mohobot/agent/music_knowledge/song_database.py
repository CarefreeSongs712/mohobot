"""SQLite 事实库 — 移植自 Agent-LuoTianyi (song_database.py)。

一张 songs 表, 存歌曲名/UP主/演唱/介绍/歌词, 查询全部走 knowledge_service.py。
数据库文件独立于 mohobot.db(默认 data/song_knowledge/knowledge_db.db)。
"""

from __future__ import annotations

import os
import uuid
from typing import Dict, Generator, Optional

from sqlalchemy import Column, String, Text, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

Base = declarative_base()


class Song(Base):
    __tablename__ = "songs"

    uuid = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    safe_name = Column(String, nullable=False)  # 过滤非字母数字的规范化名, 防重复
    uploader = Column(String, nullable=True)    # UP主
    singers = Column(String, nullable=True)     # 演唱
    introduction = Column(Text, nullable=False)  # short_summary
    lyrics = Column(Text, nullable=False)        # lyrics (cleaned)


SessionLocal = None
engine = None
_engine_url: str | None = None  # 已初始化的 URL(防重复重建)


def init_song_db(config: Dict):
    """Initialize database tables.

    幂等: 相同 db 路径重复调用不重建(多 bot 共享同一知识库时避免
    全局 engine 被反复替换导致旧连接泄漏)。
    """
    global engine, SessionLocal, _engine_url

    db_folder: str = config.get("db_folder", None)
    db_file: str = config.get("db_file", None)

    if not db_folder:
        raise ValueError("song_database 配置缺少 db_folder")
    if not db_file:
        raise ValueError("song_database 配置缺少 db_file")

    # Ensure directory exists
    os.makedirs(db_folder, exist_ok=True)

    DATABASE_URL = f"sqlite:///{os.path.join(db_folder, db_file)}"

    if _engine_url == DATABASE_URL and engine is not None and SessionLocal is not None:
        return  # 已初始化同一库, 直接复用

    if engine is not None:
        engine.dispose()  # 换库前释放旧连接

    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    _engine_url = DATABASE_URL
    Base.metadata.create_all(bind=engine)


def get_song_db() -> Generator[Session, None, None]:
    """Generator for database session"""
    global SessionLocal
    if SessionLocal is None:
        raise RuntimeError("song database 未初始化, 请先调用 init_song_db()")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_song_session() -> Session:
    """Direct session"""
    global SessionLocal
    if SessionLocal is None:
        raise RuntimeError("song database 未初始化, 请先调用 init_song_db()")
    return SessionLocal()
