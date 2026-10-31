#!/usr/bin/env python3
"""LLM prompt cache 命中率聚合(审查提案 C-1 的度量半边).

读 data/stats/llm_usage.jsonl(或 --file 指定), 输出按维度聚合的命中率:
prompt_tokens 总量 / cached_tokens 总量 / 命中率 / 请求数。

用法:
  python scripts/usage_hit_rate.py                      # 最近 7 天, 按日
  python scripts/usage_hit_rate.py --days 30 --by chat_type
  python scripts/usage_hit_rate.py --by module --by day

只读, 不写任何文件; 记录缺失字段按 0 计。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

DEFAULT_FILE = Path("./data/stats/llm_usage.jsonl")


def _records(path: Path, days: int):
    cutoff = time.time() - days * 86400
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = float(rec.get("time", 0) or 0)
            if days > 0 and ts and ts < cutoff:
                continue
            yield rec


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM prompt cache 命中率聚合")
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--days", type=int, default=7, help="只统计最近 N 天(0=全部)")
    ap.add_argument("--by", action="append", default=["day"],
                    choices=["day", "chat_type", "module", "kind", "bot_id"],
                    help="聚合维度, 可多次给(默认 day)")
    args = ap.parse_args()

    groups: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])  # prompt, cached, calls
    for rec in _records(args.file, args.days):
        prompt = int(rec.get("prompt_tokens", 0) or 0)
        cached = int(rec.get("cached_tokens", 0) or 0)
        key = []
        for dim in args.by:
            if dim == "day":
                ts = float(rec.get("time", 0) or 0)
                key.append(time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "unknown")
            else:
                key.append(str(rec.get(dim, "") or "unknown"))
        g = groups[tuple(key)]
        g[0] += prompt
        g[1] += cached
        g[2] += 1

    if not groups:
        print(f"no records in {args.file} (last {args.days or 'all'} days)")
        return 0

    header = " | ".join(f"{d:<12}" for d in args.by)
    print(f"{header} | {'requests':>8} | {'prompt':>12} | {'cached':>12} | {'hit_rate':>8}")
    print("-" * (len(header) + 60))
    total = [0, 0, 0]
    for key in sorted(groups):
        p, c, n = groups[key]
        total[0] += p; total[1] += c; total[2] += n
        rate = f"{(c * 100 / p):.1f}%" if p else "n/a"
        print(f"{' | '.join(f'{k:<12}' for k in key)} | {n:>8} | {p:>12} | {c:>12} | {rate:>8}")
    rate = f"{(total[1] * 100 / total[0]):.1f}%" if total[0] else "n/a"
    print("-" * (len(header) + 60))
    print(f"{'TOTAL':<{12 * len(args.by)}} | {total[2]:>8} | {total[0]:>12} | {total[1]:>12} | {rate:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
