#!/usr/bin/env python3
"""history 群聊合并布局一次性迁移脚本。

把旧布局 data/history/{bot_id}/group/{群号}.jsonl(每 bot 一份, 互相重复,
含遗留 QQ 号目录) 合并进新布局 data/history/group/{群号}.jsonl:

  - 按 message_id 去重(同一条群消息多 bot 各归档一份只保留一条);
    无 message_id 的行按 (time, user_id, raw_message) 兜底去重
  - 去重按群隔离: 不同群里的相同内容不能互相吞掉(issue #134)
  - 行内补 bot_id 字段(取来源目录名), 与新写入格式一致
  - 合并结果按 time 排序后写入(多来源文件按时间混流)
  - 已处理完的来源文件移入备份目录 data/history_legacy_{时间戳}/{bot_id}/group/
  - 私聊({bot_id}/private/)不动 —— 新布局中私聊仍按 bot 分目录

用法(必须先停止 mohobot 主进程, 避免文件占用/写入竞态):

    python scripts/migrate_history_layout.py            # 交互确认后执行
    python scripts/migrate_history_layout.py --yes      # 非交互确认执行
    python scripts/migrate_history_layout.py --dry-run  # 只预览不落盘(完全不写目录)
    python scripts/migrate_history_layout.py --data-dir /opt/mohobot/data

幂等: 目标文件已有内容也参与去重(重复执行安全); 重复执行时来源已移入
备份目录, 自然无事可做。

安全性(issue #134):
  - dry-run 完全只读, 不会创建/修改/移动任何文件;
  - 实际执行前必须显式确认(--yes 或交互输入), 非交互环境缺 --yes 直接拒绝;
  - 解析到损坏行时默认中止(退出码 2), 不改动任何文件; 确认可跳过后用
    --skip-invalid-lines 显式跳过并计入报告;
  - 任一目标文件写入失败即中止, 不移动来源文件(退出码 1)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

EXIT_OK = 0
EXIT_ABORTED = 1
EXIT_INVALID_INPUT = 2


def dedup_key(event: dict) -> str | None:
    """去重键: message_id 优先, 无 mid 时退回内容指纹。"""
    mid = str(event.get("message_id") or "").strip()
    if mid:
        return f"mid:{mid}"
    raw = event.get("raw_message")
    if raw is None:
        raw = json.dumps(event.get("message"), ensure_ascii=False)
    content = f"{event.get('time')}|{event.get('user_id')}|{raw}"
    return "hash:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _iter_json_lines(path: Path):
    """逐行解析 JSONL, 返回 (event, bad) —— bad 表示空行以外的损坏行。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            yield None, True
            continue
        if not isinstance(event, dict):
            yield None, True
            continue
        yield event, False


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("拒绝执行: 非交互环境必须显式加 --yes", file=sys.stderr)
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="history 群聊合并布局迁移")
    parser.add_argument("--data-dir", default="./data", help="mohobot data 目录")
    parser.add_argument("--dry-run", action="store_true", help="只预览, 不写不改名")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认(非交互环境必需)")
    parser.add_argument(
        "--skip-invalid-lines",
        action="store_true",
        help="跳过无法解析的损坏行(默认遇到损坏行中止, 不动任何文件)",
    )
    args = parser.parse_args(argv)

    history = Path(args.data_dir) / "history"
    if not history.is_dir():
        print(f"未找到 {history}, 无事可做")
        return EXIT_OK

    # 收集来源: {bot_id}/{群号}.jsonl(bot 目录 = 含 group/ 子目录的一级目录)
    sources: dict[str, list[Path]] = {}
    for bot_dir in sorted(history.iterdir()):
        if not bot_dir.is_dir() or bot_dir.name == "group":
            continue
        group_dir = bot_dir / "group"
        if not group_dir.is_dir():
            continue
        files = sorted(group_dir.glob("*.jsonl"))
        if files:
            sources[bot_dir.name] = files

    if not sources:
        print("没有旧布局群归档(data/history/*/group/), 无事可做")
        return EXIT_OK

    total_files = sum(len(v) for v in sources.values())
    print(f"发现 {len(sources)} 个 bot 目录 / {total_files} 个群归档文件")

    target_dir = history / "group"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(args.data_dir) / f"history_legacy_{stamp}"

    # 去重键按群隔离: gid -> set[key]。先载入目标文件已有内容(幂等)。
    seen: dict[str, set[str]] = {}
    merged: dict[str, list[tuple[int, dict, str]]] = {}  # gid -> [(time, event, bot_id)]
    existing_counts: dict[str, int] = {}
    target_bad = 0
    if target_dir.is_dir():
        for target in sorted(target_dir.glob("*.jsonl")):
            gid = target.stem
            count = 0
            for event, bad in _iter_json_lines(target):
                if bad:
                    target_bad += 1
                    continue
                key = dedup_key(event)
                if key:
                    seen.setdefault(gid, set()).add(key)
                count += 1
            existing_counts[gid] = count
            if count:
                print(f"  已有合并文件 {target.name}: {count} 行(参与去重)")
    if target_bad:
        print(f"  ⚠️ 已有合并文件中有 {target_bad} 行损坏, 保留原样且不参与去重")

    stats_kept = stats_dup = stats_bad = 0
    dup_by_gid: dict[str, int] = {}
    for bot_id, files in sources.items():
        for src in files:
            gid = src.stem
            count = dup = bad = 0
            for event, is_bad in _iter_json_lines(src):
                if is_bad:
                    bad += 1
                    continue
                key = dedup_key(event)
                bucket = seen.setdefault(gid, set())
                if key and key in bucket:
                    dup += 1
                    dup_by_gid[gid] = dup_by_gid.get(gid, 0) + 1
                    continue
                if key:
                    bucket.add(key)
                event = dict(event)
                event.setdefault("bot_id", bot_id)
                merged.setdefault(gid, []).append(
                    (int(event.get("time") or 0), event, bot_id)
                )
                count += 1
            stats_kept += count
            stats_dup += dup
            stats_bad += bad
            suffix = f", 损坏行 {bad} 行" if bad else ""
            print(f"  {bot_id}/group/{src.name}: 迁移 {count} 行, 去重丢弃 {dup} 行{suffix}")

    if stats_bad and not args.skip_invalid_lines:
        print(
            f"中止: 发现 {stats_bad} 行损坏数据, 未改动任何文件。"
            "请先修复, 或确认后加 --skip-invalid-lines 跳过。",
            file=sys.stderr,
        )
        return EXIT_INVALID_INPUT

    # 写入计划: 按群汇总, 便于执行前核对保留/去重数量
    print("按群汇总(已有 | 新增 | 去重丢弃):")
    for gid in sorted(set(existing_counts) | set(merged)):
        print(
            f"  群 {gid}: {existing_counts.get(gid, 0)} | "
            f"{len(merged.get(gid, []))} | {dup_by_gid.get(gid, 0)}"
        )

    if not merged:
        print("完成: 没有需要迁移的行" + ("(dry-run)" if args.dry_run else ""))
        return EXIT_OK

    if args.dry_run:
        for gid, rows in sorted(merged.items()):
            rows.sort(key=lambda x: x[0])
            print(f"  → [dry-run] {target_dir / f'{gid}.jsonl'}: 将追加 {len(rows)} 行")
        print(
            f"完成(dry-run 未落盘): 可迁移 {stats_kept} 行, 去重丢弃 {stats_dup} 行"
        )
        return EXIT_OK

    if not _confirm(
        f"将向 {target_dir} 追加 {stats_kept} 行, 并把 "
        f"{sum(len(v) for v in sources.values())} 个来源文件移动到 {backup_dir}。继续?",
        args.yes,
    ):
        print("已取消, 未改动任何文件")
        return EXIT_ABORTED

    for gid, rows in sorted(merged.items()):
        rows.sort(key=lambda x: x[0])
        target = target_dir / f"{gid}.jsonl"
        print(f"  → {target}: 追加 {len(rows)} 行(按时间排序)")
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as fh:
                for _, event, _bot in rows:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"写入失败, 已停止且未移动来源文件: {target}: {exc}", file=sys.stderr)
            return EXIT_ABORTED

    # 备份: 来源文件移入 data/history_legacy_{stamp}/{bot_id}/group/
    for bot_id, files in sources.items():
        for src in files:
            dst = backup_dir / bot_id / "group" / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        group_dir = history / bot_id / "group"
        try:
            group_dir.rmdir()  # 空了才删得掉
        except OSError:
            pass
    print(f"旧文件已备份到 {backup_dir}")
    print(f"完成: 迁移 {stats_kept} 行, 去重丢弃 {stats_dup} 行")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
