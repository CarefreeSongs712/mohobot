"""手动同步 VCPedia 新歌到歌曲知识库。

用法:
  python scripts/sync_vcpedia.py        # 同步当年洛天依模板页新歌

退出码(issue #134):
  0 = 全部成功(或没有新歌)
  1 = 部分失败(有新增也有失败)
  2 = 全部失败 / 配置或运行前置条件不满足
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.music_knowledge.vcpedia import sync_vcpedia_new_songs

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FAILED = 2


def load_music_knowledge_config() -> dict:
    """从 config/global.yaml 读顶层 music_knowledge 配置。"""
    from mohobot.models.config import GlobalConfig

    cfg = GlobalConfig.load("./config/global.yaml")
    return cfg.music_knowledge or {}


def _exit_code(added: int, failed: int) -> int:
    """按新增/失败数量决定退出码, 不把失败吞成 0。"""
    if failed <= 0:
        return EXIT_OK
    if added > 0:
        return EXIT_PARTIAL
    return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步 VCPedia 新歌到歌曲知识库")
    parser.parse_args(argv)

    try:
        config = load_music_knowledge_config()
        result = sync_vcpedia_new_songs(config)
    except Exception as exc:  # 配置缺失/网络前置失败: 明确非零
        print(f"同步失败: {exc}", file=sys.stderr)
        return EXIT_FAILED

    added = result.get("added", [])
    failed = result.get("failed", [])
    skipped = result.get("skipped", 0)

    print("\n===== 本次同步结果 =====")
    print(f"新增歌曲数: {len(added)}")
    for name in added:
        print(f"  + {name}")
    if skipped:
        print(f"跳过(已存在)数: {skipped}")
    print(f"抓取/入库失败数: {len(failed)}")
    for item in failed:
        print(f"  - {item}")

    code = _exit_code(len(added), len(failed))
    if code == EXIT_PARTIAL:
        print("结果: 部分失败", file=sys.stderr)
    elif code == EXIT_FAILED:
        print("结果: 失败(无新增)", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
