"""多来源同步歌曲知识库(命令行)。

用法:
  python scripts/sync_songs.py                      # 按配置同步全部来源
  python scripts/sync_songs.py --mode new_only      # 只抓新歌
  python scripts/sync_songs.py --source moegirl     # 只同步一个来源
  python scripts/sync_songs.py --limit 200          # 每来源最多抓 200 首
  python scripts/sync_songs.py --status             # 查看上次结果

说明: 该脚本直接写歌曲库, 建议在主进程运行时只做只读的 --status;
抓取任务请优先用 /sync-songs 或定时同步, 避免两个进程同时写 SQLite。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_music_config() -> dict:
    from mohobot.models.config import GlobalConfig

    cfg = GlobalConfig.load("./config/global.yaml")
    return cfg.music_knowledge or {}


def print_status(music_cfg: dict) -> None:
    from sqlalchemy import func

    from mohobot.music_knowledge.pool import ensure_init, get_session
    from mohobot.music_knowledge.song_database import Song, SongSource, get_sync_state

    db_cfg = music_cfg.get("song_database") or {}
    folder = db_cfg.get("db_folder", "./data/song_knowledge")
    filename = db_cfg.get("db_file", "knowledge_db.db")
    ensure_init(folder, filename)
    db = get_session()
    try:
        total = db.query(Song).count()
        state = get_sync_state(db, "song_sync")
        rows = (
            db.query(SongSource.source, func.count(SongSource.id))
            .group_by(SongSource.source)
            .all()
        )
    finally:
        db.close()

    print(f"歌曲库: {folder}/{filename}")
    print(f"总数: {total}")
    sources = state.get("sources") or {}
    for source, count in rows:
        item = sources.get(source) or {}
        print(f"  {source}: 记录 {count}, 上次 {item.get('last_run') or '从未'}")
    for source, item in sources.items():
        if not any(source == row[0] for row in rows):
            print(f"  {source}: 无记录, 上次 {item.get('last_run') or '从未'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="多来源同步歌曲知识库")
    parser.add_argument("--mode", default=None,
                        choices=["new_only", "fill_missing", "full_refresh"])
    parser.add_argument("--source", default="", help="只同步该来源(vcpedia/vocawiki/moegirl)")
    parser.add_argument("--limit", type=int, default=None, help="每来源最多抓取数量")
    parser.add_argument("--status", action="store_true", help="只打印同步状态")
    args = parser.parse_args()

    music_cfg = load_music_config()
    if args.status:
        print_status(music_cfg)
        return 0

    from mohobot.music_knowledge.sync import build_adapters, set_db_location, sync_all

    db_cfg = music_cfg.get("song_database") or {}
    if db_cfg.get("db_folder") and db_cfg.get("db_file"):
        set_db_location(str(db_cfg["db_folder"]), str(db_cfg["db_file"]))

    adapters = build_adapters(music_cfg)
    if args.source:
        adapters = [a for a in adapters if a.name == args.source]
    if not adapters:
        print("没有启用的来源, 请检查 music_knowledge.sync.sources")
        return 1

    sync_cfg = music_cfg.get("sync") or {}
    mode = args.mode or sync_cfg.get("mode") or "fill_missing"
    report = sync_all(
        adapters, mode=mode, limit=args.limit,
        max_fail=int(sync_cfg.get("max_fail", 30) or 30),
    )
    print("\n===== 同步结果 =====")
    print(report.summary_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
