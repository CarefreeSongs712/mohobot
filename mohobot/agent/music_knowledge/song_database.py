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


def init_song_db(config: Dict):
    """Initialize database tables"""
    global engine, SessionLocal

    db_folder: str = config.get("db_folder", None)
    db_file: str = config.get("db_file", None)

    if not db_folder:
        raise ValueError("song_database 配置缺少 db_folder")
    if not db_file:
        raise ValueError("song_database 配置缺少 db_file")

    # Ensure directory exists
    os.makedirs(db_folder, exist_ok=True)

    DATABASE_URL = f"sqlite:///{os.path.join(db_folder, db_file)}"

    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
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
