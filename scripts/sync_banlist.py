"""把用户提供的全局封禁名单写入生产 banall_list.json:
28 个用户全部 time=0(永久) + reason="bot"。

用法: python scripts/sync_banlist.py <生产 data 目录>
"""
import json
import sys
from pathlib import Path

# 用户提供的全局禁用用户列表(全部改为永久 + 理由 bot)
UIDS = [
    3905442817, 3894436213, 3889503222, 3937755853, 3602423951,
    3443571731, 3699852885, 3314023814, 1722415589, 1447796110,
    2984183938, 3663048599, 3807738952, 3758311466, 2307845006,
    1489506663, 2132658176, 2831510346, 1955956228, 2137074986,
    3125606254, 3135911351, 3839186911, 3616174427, 2557594386,
    1607476270, 3889237221, 3889006601,
]

RECORDS = [
    {"uid": str(u), "time": 0, "reason": "bot"}
    for u in UIDS
]


def main() -> None:
    data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "./data")
    path = data_dir / "ban" / "banall_list.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(RECORDS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已写入 {path}: {len(RECORDS)} 条全局永久封禁(理由=bot)")
    for r in RECORDS[:5]:
        print(" ", r["uid"], "永久", r["reason"])


if __name__ == "__main__":
    main()
