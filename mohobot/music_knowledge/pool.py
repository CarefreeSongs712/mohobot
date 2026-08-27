"""线程安全 SQLite 会话池 — 供全局歌曲知识库(matcher/vcpedia)与迁移脚本使用。

pool.py 提供 get_session() / close_all()。与 mohobot/db/sql_database.py 独立的
轻量设施: 线程池 + 每线程独立连接, 避免一块 SQLAlchemy engine 被多线程
并发读取(歌曲库读写量小, 但同步脚本跑在 worker 线程, 匹配器可能被
多个事件循环并发调用)。

用法:
    from mohobot.music_knowledge import pool
    pool.ensure_init(db_folder, db_file)   # 首次调用(幂等)
    with pool.get_session() as db:
        rows = db.query(Song).all()
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mohobot.music_knowledge.song_database import Base

_lock = threading.Lock()
_engine = None
_SessionLocal: Optional[sessionmaker] = None
_engine_url: str = ""


def ensure_init(db_folder: str, db_file: str) -> None:
    """初始化歌曲库连接池(幂等; 换路径时重连)。"""
    global _engine, _SessionLocal, _engine_url
    os.makedirs(db_folder, exist_ok=True)
    url = f"sqlite:///{os.path.join(db_folder, db_file)}"
    with _lock:
        if _engine is not None and _engine_url == url:
            return
        if _engine is not None:
            _engine.dispose()
        # 每线程独立连接: 线程池内各线程各持一个, 天然线程安全
        _engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
        Base.metadata.create_all(bind=_engine)
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        _engine_url = url


def get_session() -> Session:
    """获取一个 SQLAlchemy Session(线程内安全; 调用方负责 close/with)。"""
    if _SessionLocal is None:
        raise RuntimeError("music_knowledge pool 未初始化, 请先调用 pool.ensure_init()")
    return _SessionLocal()


def close_all() -> None:
    """释放连接池(进程退出/换库时调用)。"""
    global _engine, _SessionLocal, _engine_url
    with _lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None
        _SessionLocal = None
        _engine_url = ""