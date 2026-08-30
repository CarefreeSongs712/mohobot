"""Startup/shutdown smoke test for the full MohobotApplication.

Boots the real app (WS server + web panel), then shuts down.
No OneBot client connects; we only verify wiring doesn't crash.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import MohobotApplication


async def main() -> None:
    # 使用隔离的测试配置(不同端口 + 独立数据库),避免影响正在运行的实例
    config_path = os.environ.get(
        "MOHOBOT_TEST_CONFIG",
        str(Path(__file__).resolve().parent / "test_config.yaml"),
    )
    app = MohobotApplication(config_path=config_path)
    try:
        await app.startup()
        assert app._database_manager is not None, "DatabaseManager not initialized"
        assert app._message_handler is not None
        print("startup OK")
        print(f"  db: {app._config.database.folder}/{app._config.database.file}")
    finally:
        await app.shutdown()
    print("shutdown OK")


if __name__ == "__main__":
    asyncio.run(main())
