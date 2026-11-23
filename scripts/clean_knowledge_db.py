# -*- coding: utf-8 -*-
"""一次性清理 knowledge_db.db 中的 wiki 冗余。

清洗规则统一在 mohobot/music_knowledge/text_clean.py 维护(爬虫入库前
也使用同一套规则), 本脚本只负责: 备份原库 → 就地清洗 → 校验。

- introduction: 去除 ''' 粗体引号、{{...}} 模板(计数模板整删、ruby/内容
  模板保留内文)、infobox/wikitable 残片、链接残留(目标|显示 → 显示)
- lyrics: 去除 HTML 标签壳(标签内正文保留)、<ref> 注释、弹幕画布残行
- credits/singers: 去除 [[ 壳与跨wiki前缀

安全性(issue #134):
- 备份使用 SQLite backup API 生成一致快照(不会因为未 checkpoint 的 WAL
  丢失已提交数据), 并对备份做 integrity_check + 行数校验;
- 每次备份文件名带时间戳且不覆盖已有备份(同日重复运行各自留档);
- --dry-run 完全只读, 只打印计划, 不备份不写入;
- 实际写入前必须确认(--yes 或交互输入), 非交互环境缺 --yes 直接拒绝。

用法:
  python scripts/clean_knowledge_db.py --dry-run
  python scripts/clean_knowledge_db.py --yes
  python scripts/clean_knowledge_db.py --db /opt/mohobot/data/song_knowledge/knowledge_db.db --yes
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.music_knowledge.text_clean import clean_credit, clean_introduction, clean_lyrics

DEFAULT_DB = (
    Path(__file__).resolve().parent.parent / "data" / "song_knowledge" / "knowledge_db.db"
)

CREDIT_FIELDS = ("uploader", "singers", "lyricist", "composer", "arranger",
                 "mixer", "tuner", "mastering", "pv", "illustrator")

EXIT_OK = 0
EXIT_ABORTED = 1
EXIT_INVALID_INPUT = 2


def _unique_backup_path(db_path: Path) -> Path:
    """生成不与现有备份冲突的备份路径(同日重复运行也不覆盖)。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    index = 1
    while candidate.exists():
        candidate = db_path.with_name(f"{db_path.name}.bak-{stamp}-{index}")
        index += 1
    return candidate


def _backup_database(db_path: Path, backup_path: Path) -> int:
    """SQLite 在线备份 API 生成一致快照, 校验完整性与行数后返回总行数。"""
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(backup_path))
        try:
            source.backup(dest)
            check = dest.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RuntimeError(f"备份完整性校验失败: {check}")
            backup_rows = dest.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        finally:
            dest.close()
        source_rows = source.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    finally:
        source.close()

    if backup_rows != source_rows:
        raise RuntimeError(
            f"备份行数不一致: 源 {source_rows} / 备份 {backup_rows}"
        )
    return backup_rows


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("拒绝执行: 非交互环境必须显式加 --yes", file=sys.stderr)
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _compute_updates(db_path: Path, *, readonly: bool):
    """读取全表并计算清洗结果。readonly=True 时用只读连接(dry-run 不落盘)。"""
    if readonly:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT uuid, introduction, lyrics, " + ", ".join(CREDIT_FIELDS) + " FROM songs"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    updates = []
    lost_content = []
    for row in rows:
        uuid, intro, lyrics = row[0], row[1], row[2]
        credits = row[3:]
        new_intro = clean_introduction(intro or "")
        new_lyrics = clean_lyrics(lyrics or "")
        new_credits = [clean_credit(x) if x else x for x in credits]
        # 校验: 清洗不得把非空字段清空
        if (intro or "").strip() and not new_intro:
            lost_content.append((uuid, "introduction"))
        if (lyrics or "").strip() and not new_lyrics:
            lost_content.append((uuid, "lyrics"))
        if new_intro != (intro or "") or new_lyrics != (lyrics or "") or new_credits != list(credits):
            updates.append([new_intro, new_lyrics, *new_credits, uuid])
    return len(rows), updates, lost_content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 knowledge_db.db 中的 wiki 冗余")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="knowledge_db.db 路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划, 不备份不写入")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认(非交互环境必需)")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"未找到数据库: {db_path}", file=sys.stderr)
        return EXIT_INVALID_INPUT

    total, updates, lost_content = _compute_updates(db_path, readonly=args.dry_run)
    print(f"需更新 {len(updates)} / {total} 行")
    if lost_content:
        print("以下行清洗后非空字段变空(请抽查):")
        for uuid, field in lost_content[:20]:
            print("  ", uuid, field)

    if args.dry_run:
        print("dry-run: 未备份、未写入任何数据")
        return EXIT_OK
    if not updates:
        print("没有需要更新的行, 无需备份")
        return EXIT_OK

    if not _confirm(
        f"将备份 {db_path} 并就地更新 {len(updates)} 行, 继续?", args.yes
    ):
        print("已取消, 未改动任何文件")
        return EXIT_ABORTED

    backup_path = _unique_backup_path(db_path)
    try:
        backup_rows = _backup_database(db_path, backup_path)
    except Exception as exc:
        print(f"备份失败, 已中止且未写入: {exc}", file=sys.stderr)
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        return EXIT_ABORTED
    print(f"已备份 {backup_rows} 行到: {backup_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.executemany(
            "UPDATE songs SET introduction=?, lyrics=?, "
            + ", ".join(f + "=?" for f in CREDIT_FIELDS)
            + " WHERE uuid=?",
            updates,
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"写入失败, 已回滚: {exc}", file=sys.stderr)
        return EXIT_ABORTED
    finally:
        conn.close()
    print("完成")
    return EXIT_OK


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
