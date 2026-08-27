"""SQLite 歌曲知识库 — 全局歌曲识别与事实库(重写)。

一张 songs 表, 存歌曲名/UP主/演唱/词曲混调等创作人员/介绍/完整歌词;
新增 song_stats 表记录库状态(供挂载判断)。查询全部走 knowledge_service.py。
与旧版(mohobot/agent/music_knowledge)的区别:
- 不再依赖 res/song_knowledge/ 内置库文件, 也不复制任何默认文件到 data 目录;
- 启动时若库路径不存在则按新 schema 建空库;
- 歌词保留完整换行(供 LLM 前注入), 不生成 song_lyric_keywords.txt 等关键词文件。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Dict, Generator, Optional

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

Base = declarative_base()


class Song(Base):
    __tablename__ = "songs"

    uuid = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)             # 歌曲原名
    safe_name = Column(String, nullable=False)        # 规范化名(过滤特殊字符, 防重复)
    uploader = Column(String, nullable=True)          # UP主
    singers = Column(String, nullable=True)           # 演唱(多个以逗号分隔)
    lyricist = Column(String, nullable=True)          # 作词
    composer = Column(String, nullable=True)          # 作曲
    arranger = Column(String, nullable=True)          # 编曲
    mixer = Column(String, nullable=True)             # 混音
    tuner = Column(String, nullable=True)             # 调教/调校
    mastering = Column(String, nullable=True)         # 母带
    pv = Column(String, nullable=True)                # PV
    illustrator = Column(String, nullable=True)       # 曲绘
    year = Column(Integer, nullable=True)             # 投稿年份
    introduction = Column(Text, nullable=False, default="")  # 歌曲介绍
    lyrics = Column(Text, nullable=False, default="")        # 完整歌词(保留换行)


class SongStats(Base):
    __tablename__ = "song_stats"

    id = Column(String, primary_key=True, default="default")
    total_songs = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=True)


SessionLocal = None
engine = None
_engine_url: str | None = None  # 已初始化的 URL(防重复重建)


def init_song_db(config: Dict):
    """初始化歌曲库(幂等: 相同路径重复调用不重建 engine)。

    只建目录与表, 不复制任何默认数据文件; 新库即为空库,
    数据通过 VCPedia 同步(/sync-songs 或 scripts/sync_vcpedia.py)或迁移脚本写入。
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


def update_song_stats(db: Session) -> None:
    """刷新 song_stats(歌曲总数/更新时间)。"""
    total = db.query(Song).count()
    stats = db.query(SongStats).filter(SongStats.id == "default").first()
    if stats is None:
        db.add(SongStats(id="default", total_songs=total, updated_at=datetime.now()))
    else:
        stats.total_songs = total
        stats.updated_at = datetime.now()
    db.commit()