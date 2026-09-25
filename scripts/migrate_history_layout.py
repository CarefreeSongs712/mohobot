#!/usr/bin/env python3
"""history 群聊合并布局一次性迁移脚本。

把旧布局 data/history/{bot_id}/group/{群号}.jsonl(每 bot 一份, 互相重复,
含遗留 QQ 号目录) 合并进新布局 data/history/group/{群号}.jsonl:

  - 按 message_id 去重(同一条群消息多 bot 各归档一份只保留一条);
    无 message_id 的行按 (time, user_id, raw_message) 兜底去重
  - 行内补 bot_id 字段(取来源目录名), 与新写入格式一致
  - 合并结果按 time 排序后写入(多来源文件按时间混流)
  - 已处理完的来源文件移入备份目录 data/history_legacy_{时间戳}/{bot_id}/group/
  - 私聊({bot_id}/private/)不动 —— 新布局中私聊仍按 bot 分目录

用法(必须先停止 mohobot 主进程, 避免文件占用/写入竞态):

    python scripts/migrate_history_layout.py            # 实际执行
    python scripts/migrate_history_layout.py --dry-run  # 只预览不落盘
    python scripts/migrate_history_layout.py --data-dir /opt/mohobot/data

幂等: 目标文件已有内容也参与去重(重复执行安全); 重复执行时来源已移入
备份目录, 自然无事可做。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path


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


def main() -> int:
    parser = argparse.ArgumentParser(description="history 群聊合并布局迁移")
    parser.add_argument("--data-dir", default="./data", help="mohobot data 目录")
    parser.add_argument("--dry-run", action="store_true", help="只预览, 不写不改名")
    args = parser.parse_args()

    history = Path(args.data_dir) / "history"
    if not history.is_dir():
        print(f"未找到 {history}, 无事可做")
        return 0

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
        return 0

    total_files = sum(len(v) for v in sources.values())
    print(f"发现 {len(sources)} 个 bot 目录 / {total_files} 个群归档文件")
    if not args.dry_run:
        print("⚠️ 请确认 mohobot 主进程已停止")

    target_dir = history / "group"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(args.data_dir) / f"history_legacy_{stamp}"

    # 全局去重键集合: 先载入目标文件已有内容(幂等), 再逐来源吸收
    seen: set[str] = set()
    merged: dict[str, list[tuple[int, dict, str]]] = {}  # gid -> [(time, event, bot_id)]
    target_dir.mkdir(parents=True, exist_ok=True)
    for target in target_dir.glob("*.jsonl"):
        count = 0
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # 损坏行原样保留在目标文件, 不参与去重
            key = dedup_key(event)
            if key:
                seen.add(key)
            count += 1
        if count:
            print(f"  已有合并文件 {target.name}: {count} 行(参与去重)")

    stats_kept = stats_dup = 0
    for bot_id, files in sources.items():
        for src in files:
            gid = src.stem
            count = dup = 0
            for line in src.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 损坏行不迁移(旧代码也读不了)
                if not isinstance(event, dict):
                    continue
                key = dedup_key(event)
                if key and key in seen:
                    dup += 1
                    continue
                if key:
                    seen.add(key)
                event = dict(event)
                event.setdefault("bot_id", bot_id)
                merged.setdefault(gid, []).append(
                    (int(event.get("time") or 0), event, bot_id)
                )
                count += 1
            stats_kept += count
            stats_dup += dup
            print(f"  {bot_id}/group/{src.name}: 迁移 {count} 行, 去重丢弃 {dup} 行")

    for gid, rows in sorted(merged.items()):
        rows.sort(key=lambda x: x[0])
        target = target_dir / f"{gid}.jsonl"
        print(f"  → {target}: 追加 {len(rows)} 行(按时间排序)")
        if not args.dry_run:
            with open(target, "a", encoding="utf-8") as fh:
                for _, event, _bot in rows:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    # 备份: 来源文件移入 data/history_legacy_{stamp}/{bot_id}/group/
    if not args.dry_run:
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
    print(f"完成: 迁移 {stats_kept} 行, 去重丢弃 {stats_dup} 行"
          + ("(dry-run 未落盘)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
