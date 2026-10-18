"""SQLite 歌曲知识库 — 全局歌曲识别与事实库(重写)。

一张 songs 表, 存歌曲名/UP主/演唱/词曲混调等创作人员/介绍/完整歌词;
新增 song_stats 表记录库状态(供挂载判断)。查询全部走 knowledge_service.py。
与旧版(mohobot/agent/music_knowledge)的区别:
- 不再依赖 res/song_knowledge/ 内置库文件, 也不复制任何默认文件到 data 目录;
- 启动时若库路径不存在则按新 schema 建空库;
- 歌词保留完整换行(供 LLM 前注入), 不生成 song_lyric_keywords.txt 等关键词文件。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Dict, Generator, Optional

from sqlalchemy import (
    Column, DateTime, Index, Integer, String, Text, UniqueConstraint,
    create_engine, event, inspect,
)
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
    pv = Column(String, nullable=True)                # PV 作者名(不是 BV 号)
    illustrator = Column(String, nullable=True)       # 曲绘
    year = Column(Integer, nullable=True)             # 投稿年份
    introduction = Column(Text, nullable=False, default="")  # 歌曲介绍
    lyrics = Column(Text, nullable=False, default="")        # 完整歌词(保留换行)
    # ── 多来源合并(增量列; 旧库由 migrate_song_schema 补齐) ──
    aliases = Column(Text, nullable=True)             # JSON: 其它写法/来源标题
    bvid = Column(String, nullable=True)              # B站 BV 号(身份区分力最高)
    lang = Column(String, nullable=True)              # zh/ja/ko/en/unknown
    normalized_name = Column(String, nullable=True)   # 索引键, 不作唯一约束
    primary_source = Column(String, nullable=True)    # 当前主要字段来源
    updated_at = Column(DateTime, nullable=True)      # 最后一次合并时间


class SongSource(Base):
    """每首歌在每个来源上的原始记录(溯源 + 增量刷新 + 字段补全依据)。

    song_uuid 为空表示抓到了但未能对齐到 songs 表, 保留供人工复核;
    (source, source_title) 唯一, 用于判断是否重复抓取。
    """

    __tablename__ = "song_sources"
    __table_args__ = (
        UniqueConstraint("source", "source_title", name="uq_song_source_title"),
        Index("ix_song_sources_song_uuid", "song_uuid"),
        Index("ix_song_sources_status", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    song_uuid = Column(String, nullable=True)
    source = Column(String, nullable=False)           # vcpedia / vocawiki / moegirl
    source_title = Column(String, nullable=False)     # 来源站页面标题
    source_url = Column(String, nullable=True)        # 可回溯地址
    license = Column(String, nullable=True)           # 来源许可快照
    content_hash = Column(String, nullable=True)      # 规范化内容哈希
    fields_json = Column(Text, nullable=False, default="{}")
    fetched_at = Column(DateTime, nullable=True)
    status = Column(String, nullable=True)            # matched / unmatched / failed
    error = Column(Text, nullable=True)


class SyncState(Base):
    """同步断点/游标/统计, 按 key 存 JSON(支持中断续跑)。"""

    __tablename__ = "sync_state"

    key = Column(String, primary_key=True)
    value_json = Column(Text, nullable=False, default="{}")
    updated_at = Column(DateTime, nullable=True)


class SongStats(Base):
    __tablename__ = "song_stats"

    id = Column(String, primary_key=True, default="default")
    total_songs = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=True)


_SONG_MIGRATION_COLUMNS = {
    "lyricist": "TEXT",
    "composer": "TEXT",
    "arranger": "TEXT",
    "mixer": "TEXT",
    "tuner": "TEXT",
    "mastering": "TEXT",
    "pv": "TEXT",
    "illustrator": "TEXT",
    "year": "INTEGER",
    "aliases": "TEXT",
    "bvid": "VARCHAR",
    "lang": "VARCHAR",
    "normalized_name": "VARCHAR",
    "primary_source": "VARCHAR",
    "updated_at": "DATETIME",
}

_SONG_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_songs_normalized_name ON songs (normalized_name)",
    "CREATE INDEX IF NOT EXISTS ix_songs_bvid ON songs (bvid)",
    "CREATE INDEX IF NOT EXISTS ix_songs_safe_name ON songs (safe_name)",
)


def migrate_song_schema(db_engine) -> None:
    """补齐旧 songs 表新增列与索引; 幂等且不覆盖已有数据。

    只做加列/加索引, 不重建表、不改已有值; 索引用 IF NOT EXISTS。
    """
    inspector = inspect(db_engine)
    if "songs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("songs")}
    missing = [(name, sql_type) for name, sql_type in _SONG_MIGRATION_COLUMNS.items()
               if name not in existing]
    with db_engine.begin() as connection:
        for name, sql_type in missing:
            connection.exec_driver_sql(
                f"ALTER TABLE songs ADD COLUMN {name} {sql_type}"
            )
        for statement in _SONG_MIGRATION_INDEXES:
            connection.exec_driver_sql(statement)


def backfill_normalized_columns(db: Session, *, batch: int = 500) -> int:
    """为旧数据补 normalized_name / lang(只填空值, 幂等)。

    返回实际更新的行数。同步后可反复调用。
    """
    from mohobot.music_knowledge.normalize import detect_language, normalize_name

    updated = 0
    query = (
        db.query(Song)
        .filter((Song.normalized_name.is_(None)) | (Song.normalized_name == ""))
        .limit(batch)
    )
    while True:
        rows = query.all()
        if not rows:
            break
        for song in rows:
            song.normalized_name = normalize_name(song.name)
            if not (song.lang or "").strip():
                song.lang = detect_language(song.lyrics or song.name)
            updated += 1
        db.commit()
    return updated


def get_sync_state(db: Session, key: str) -> Dict:
    """读取同步状态(不存在返回空 dict)。"""
    row = db.query(SyncState).filter(SyncState.key == key).first()
    if row is None:
        return {}
    try:
        value = json.loads(row.value_json or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def set_sync_state(db: Session, key: str, value: Dict) -> None:
    """写入同步状态(整体覆盖后提交)。

    用 SQLite 原生 upsert 而不是"先查再 add": 多个来源/进程同时第一次写同一个
    key 时, 先查再插会双双看到"没有"并撞 `sync_state.key` 唯一约束。
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    payload = json.dumps(value or {}, ensure_ascii=False, default=str)
    now = datetime.now()
    stmt = sqlite_insert(SyncState).values(key=key, value_json=payload, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SyncState.key],
        set_={"value_json": payload, "updated_at": now},
    )
    db.execute(stmt)
    db.commit()


SessionLocal = None
engine = None
_engine_url: str | None = None  # 已初始化的 URL(防重复重建)


def _configure_sqlite(dbapi_connection, _record) -> None:
    """每个新连接统一设置 WAL 与忙等待: 同步写库时匹配器仍可读。

    旧库是 rollback-journal 模式, 长事务会让读端报 database is locked;
    WAL 是持久属性, 切换一次后对后续连接生效。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    except Exception:  # pragma: no cover - 只读介质/不支持 WAL 时保持默认
        pass
    finally:
        cursor.close()


def init_song_db(config: Dict):
    """初始化歌曲库(幂等: 相同路径重复调用不重建 engine)。

    只建目录与表, 不复制任何默认数据文件; 新库即为空库,
    数据通过多来源同步(/sync-songs 或 scripts/sync_songs.py)写入。
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

    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    event.listen(engine, "connect", _configure_sqlite)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    _engine_url = DATABASE_URL
    Base.metadata.create_all(bind=engine)
    migrate_song_schema(engine)


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
    """刷新 song_stats(歌曲总数/更新时间)。

    原子 upsert: 三个来源并行同步结束时会同时调用本函数, "先查再 add" 会让
    它们同时看到"没有 default 行"并撞唯一约束, 异常发生在 finally 之前,
    还会让整个来源的收尾(进度状态、断点)丢失。
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    total = db.query(Song).count()
    now = datetime.now()
    stmt = sqlite_insert(SongStats).values(id="default", total_songs=total, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SongStats.id],
        set_={"total_songs": total, "updated_at": now},
    )
    db.execute(stmt)
    db.commit()