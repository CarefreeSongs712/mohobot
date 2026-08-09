"""手动同步 VCPedia 新歌到歌曲知识库。

用法:
  python scripts/sync_vcpedia.py        # 同步当年洛天依模板页新歌
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.agent.music_knowledge.vcpedia import sync_vcpedia_new_songs


def load_music_knowledge_config() -> dict:
    """从 config/global.yaml 读 agent.music_knowledge 配置。"""
    from mohobot.models.config import GlobalConfig

    cfg = GlobalConfig.load("./config/global.yaml")
    return cfg.agent.music_knowledge or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 VCPedia 新歌到歌曲知识库")
    parser.parse_args()

    config = load_music_knowledge_config()
    result = sync_vcpedia_new_songs(config)
    added = result.get("added", [])
    failed = result.get("failed", [])

    print("\n===== 本次同步结果 =====")
    print(f"新增歌曲数: {len(added)}")
    for name in added:
        print(f"  + {name}")
    print(f"抓取/入库失败数: {len(failed)}")
    for item in failed:
        print(f"  - {item}")


if __name__ == "__main__":
    main()
