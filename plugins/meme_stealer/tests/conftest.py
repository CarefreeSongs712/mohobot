"""pytest 配置：把插件目录加入 sys.path，提供隔离的插件数据目录。

插件核心只依赖 ``meme_stealer_core.host`` 适配层，测试不需要 mohobot 本体；
需要 mohobot 的入口测试（main.py）在缺少 mohobot 时自动跳过。
"""

import sys
from itertools import count
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

sys.modules.setdefault("tests.conftest", sys.modules[__name__])

_sequence = count()


@pytest.fixture
def plugin_data_dir(tmp_path):
    """每次调用返回一个新的插件数据目录。"""

    def _make() -> Path:
        path = tmp_path / f"meme_stealer-{next(_sequence)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    return _make


@pytest.fixture
def make_config(plugin_data_dir):
    """``make_config({...})`` → 使用独立数据目录的 PluginConfig。"""
    from meme_stealer_core.core.config.config import PluginConfig

    def _make(data: dict | None = None, data_dir: Path | None = None):
        return PluginConfig(dict(data or {}), data_dir or plugin_data_dir())

    return _make
