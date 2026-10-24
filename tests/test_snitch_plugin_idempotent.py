"""snitch 既是内置工具(由 llm_tools 导入)又是插件文件, 重复加载不得报错。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_snitch_registration_is_idempotent():
    from mohobot.services.llm_tools import registry

    import plugins.snitch as snitch_mod  # 第一次: 正常注册或已由内置路径注册

    assert registry.contains("snitch")
    # 模拟插件系统再次加载同一文件(模块重新执行)
    snitch_mod._register()
    assert registry.contains("snitch")
    importlib.reload(snitch_mod)   # 模块级 _register() 再跑一次, 不能抛 duplicate
    assert registry.contains("snitch")
