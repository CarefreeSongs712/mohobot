"""websockets 依赖下限回归测试(issue #134)。

mohobot/ws_server.py 在导入期使用 websockets.asyncio.server,
该模块是 websockets>=13.0 才提供的; requirements.txt 曾写 >=12.0,
下游按 <13 解析时会启动即 ImportError。这里锁定最低可用版本,
并让 CI 的 minimum-dependencies job 在固定旧版本上实际导入。
"""

from __future__ import annotations

import importlib

from packaging.version import Version


def _websockets_version() -> Version:
    ws = importlib.import_module("websockets")
    return Version(ws.__version__)


def test_websockets_meets_declared_floor():
    assert _websockets_version() >= Version("13.0"), (
        "requirements.txt 声明的 websockets 下限是 13.0, 当前 "
        f"{_websockets_version()} 不满足"
    )


def test_asyncio_server_module_available():
    server = importlib.import_module("websockets.asyncio.server")
    assert hasattr(server, "serve")
    assert hasattr(server, "Server")
    assert hasattr(server, "ServerConnection")


def test_ws_server_imports_and_constructs(tmp_path):
    module = importlib.import_module("mohobot.ws_server")
    from mohobot.bot_manager import BotManager

    server = module.WSServer(BotManager(), host="127.0.0.1", port=0, data_dir=str(tmp_path))
    assert server._port == 0
