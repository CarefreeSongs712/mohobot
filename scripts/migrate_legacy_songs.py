"""把旧版歌曲知识库(res/song_knowledge/knowledge_db.db)迁移到新 schema。

新库字段: uuid/name/safe_name/uploader/singers/lyricist/composer/arranger/
mixer/tuner/mastering/pv/illustrator/year/introduction/lyrics。
旧库只有 name/uploader/singers/introduction/lyrics —— 迁移后 credits 留空,
歌词保留换行。可选参数恢复来源(旧 knowledge_db.db 路径)。

用法:
  python scripts/migrate_legacy_songs.py [旧库路径]
默认从 res/song_knowledge/knowledge_db.db 读取(若存在)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.music_knowledge import pool, Song, update_song_stats
from mohobot.music_knowledge.song_database import Base


def migrate(legacy_db_path: str, new_db_folder: str, new_db_file: str) -> int:
    """迁移旧库歌曲到新库; 返回迁移条数(幂等: 已存在歌名跳过)。"""
    import sqlite3

    if not Path(legacy_db_path).exists():
        raise FileNotFoundError(f"旧库不存在: {legacy_db_path}")

    conn = sqlite3.connect(legacy_db_path)
    cur = conn.cursor()
    # 旧表列名(可能缺列)
    cols = [r[1] for r in cur.execute("PRAGMA table_info(songs)").fetchall()]
    sel = ["name"]
    for c in ("uploader", "singers", "introduction", "lyrics"):
        if c in cols:
            sel.append(c)
    rows = cur.execute(
        f"SELECT {', '.join(sel)} FROM songs"
    ).fetchall()
    conn.close()

    pool.ensure_init(new_db_folder, new_db_file)
    migrated = 0
    existing = 0
    with pool.get_session() as db:
        for row in rows:
            name = row[0]
            if not name:
                continue
            exists = db.query(Song).filter(
                (Song.name == name) | (Song.safe_name == _safe(name))
            ).first()
            if exists:
                existing += 1
                continue
            rec = Song(
                name=name,
                safe_name=_safe(name),
                uploader=(row[1] if len(row) > 1 else "") or "",
                singers=(row[2] if len(row) > 2 else "") or "",
                introduction=(row[3] if len(row) > 3 else "") or "",
                lyrics=(row[4] if len(row) > 4 else "") or "",
            )
            db.add(rec)
            migrated += 1
        update_song_stats(db)
    return migrated, existing


def _safe(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="迁移旧歌曲知识库到新 schema")
    parser.add_argument("legacy_db", nargs="?", default=None,
                        help="旧 knowledge_db.db 路径(默认 res/song_knowledge/knowledge_db.db)")
    args = parser.parse_args()

    legacy = args.legacy_db or str(
        Path(__file__).resolve().parent.parent / "res" / "song_knowledge" / "knowledge_db.db"
    )
    new_folder = "./data/song_knowledge"
    new_file = "knowledge_db.db"
    try:
        migrated, existing = migrate(legacy, new_folder, new_file)
    except Exception as e:
        print(f"❌ 迁移失败: {e}")
        sys.exit(1)
    print(f"✅ 迁移完成: 新增 {migrated} 首, 跳过(已存在) {existing} 首")
    print(f"新库: {new_folder}/{new_file}")


if __name__ == "__main__":
    main()