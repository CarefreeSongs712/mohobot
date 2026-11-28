"""多来源同步歌曲知识库(命令行)。

用法:
  python scripts/sync_songs.py                      # 按配置同步全部来源
  python scripts/sync_songs.py --mode new_only      # 只抓新歌
  python scripts/sync_songs.py --source moegirl     # 只同步一个来源
  python scripts/sync_songs.py --limit 200          # 每来源最多抓 200 首
  python scripts/sync_songs.py --status             # 查看上次结果

说明: 该脚本直接写歌曲库, 建议在主进程运行时只做只读的 --status;
抓取任务请优先用 /sync-songs 或定时同步, 避免两个进程同时写 SQLite。

退出码(issue #134, 不再把失败吞成 0):
  0 = 所有来源成功
  1 = 部分失败(有成功来源, 也有失败/中止来源)
  2 = 全部失败 / 没有可用来源 / 配置加载失败
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FAILED = 2


def _report_exit_code(report) -> int:
    """按来源统计决定退出码: 全成功 0, 部分失败 1, 全失败 2。"""
    if not report.sources:
        return EXIT_FAILED
    failed = [s for s in report.sources if s.aborted or s.failed > 0]
    if not failed:
        return EXIT_OK
    if len(failed) == len(report.sources):
        return EXIT_FAILED
    return EXIT_PARTIAL


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多来源同步歌曲知识库")
    parser.add_argument("--mode", default=None,
                        choices=["new_only", "fill_missing", "full_refresh"])
    parser.add_argument("--source", default="", help="只同步该来源(vcpedia/vocawiki/moegirl)")
    parser.add_argument("--limit", type=int, default=None, help="每来源最多抓取数量")
    parser.add_argument("--status", action="store_true", help="只打印同步状态")
    args = parser.parse_args(argv)

    music_cfg = load_music_config()
    if args.status:
        print_status(music_cfg)
        return 0

    try:
        from mohobot.music_knowledge.sync import (
            build_adapters,
            set_db_location,
            sync_all,
        )

        db_cfg = music_cfg.get("song_database") or {}
        if db_cfg.get("db_folder") and db_cfg.get("db_file"):
            set_db_location(str(db_cfg["db_folder"]), str(db_cfg["db_file"]))

        adapters = build_adapters(music_cfg)
        if args.source:
            adapters = [a for a in adapters if a.name == args.source]
        if not adapters:
            print(
                "没有启用的来源, 请检查 music_knowledge.sync.sources",
                file=sys.stderr,
            )
            return EXIT_FAILED

        sync_cfg = music_cfg.get("sync") or {}
        mode = args.mode or sync_cfg.get("mode") or "fill_missing"
        report = sync_all(
            adapters, mode=mode, limit=args.limit,
            max_fail=int(sync_cfg.get("max_fail", 30) or 30),
        )
    except Exception as e:
        print(f"同步失败: {e}", file=sys.stderr)
        return EXIT_FAILED

    print("\n===== 同步结果 =====")
    print(report.summary_text())
    code = _report_exit_code(report)
    if code == EXIT_PARTIAL:
        print("部分来源失败, 详见上方结果", file=sys.stderr)
    elif code == EXIT_FAILED:
        print("全部来源失败, 详见上方结果", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
