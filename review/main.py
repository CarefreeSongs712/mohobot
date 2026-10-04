#!/usr/bin/env python3
"""审核面板入口 — 独立进程, mohobot 启动时拉起(若未运行), 关闭不影响本进程。

用法:
  python review/main.py
  python review/main.py --config path/to/config.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

REVIEW_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REVIEW_DIR.parent))  # 仓库根, 使 `import review.*` 可用


def _load_admins(repo_root: Path) -> set[str]:
    """管理员免检名单: 读 mohobot 全局配置的 admins(缺失/损坏时为空集)。"""
    try:
        import yaml
        cfg = yaml.safe_load(
            (repo_root / "config" / "global.yaml").read_text(encoding="utf-8")
        ) or {}
        return {str(a) for a in cfg.get("admins") or []}
    except Exception:
        return set()


def main() -> None:
    import uvicorn
    from loguru import logger

    from review.app import create_app
    from review.config import load_config
    from review.loader import MohobotData
    from review.store import ReviewStore

    config_path = REVIEW_DIR / "config.yaml"
    argv = sys.argv[1:]
    if "--config" in argv:
        config_path = Path(argv[argv.index("--config") + 1])
    if not config_path.exists():
        print(f"缺少配置文件: {config_path}")
        print("请复制 config.example.yaml 为 config.yaml, 并用 hash_password.py 生成密码哈希")
        sys.exit(1)
    cfg = load_config(config_path)
    if not cfg.users:
        print("config.yaml 未配置任何用户(users), 无法登录")
        sys.exit(1)

    data_dir = cfg.resolve_data_dir(REVIEW_DIR.parent)
    if not data_dir.exists():
        print(f"mohobot 数据目录不存在: {data_dir} (config.yaml 的 data_dir)")
        sys.exit(1)

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    data = MohobotData(
        data_dir,
        cache_path=REVIEW_DIR / "data" / "loader_cache.json",
        exempt_uids=_load_admins(REVIEW_DIR.parent),
    )
    store = ReviewStore(REVIEW_DIR / "data" / "review.db")
    app = create_app(cfg, data, store, config_path=config_path)

    logger.info(
        f"审核面板启动: http://{cfg.host}:{cfg.port} | 数据目录: {data_dir} | "
        f"用户: {len(cfg.users)} | 免检管理员: {len(data.exempt_uids)}"
    )
    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")
    finally:
        try:
            data.flush_cache()  # history 解析缓存落盘(重启免冷解析)
        except Exception:
            pass
        store.close()


if __name__ == "__main__":
    main()
