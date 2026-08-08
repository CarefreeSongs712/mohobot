"""插件热加载测试: 新增/修改/启停无需重启即生效。"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PLUGIN_A = '''class Plugin:
    """插件 A"""
    _ws = None
    @classmethod
    def inject_ws_server(cls, ws): cls._ws = ws
    async def on_message(self, bot_id, event, raw):
        return (False, None)
'''

PLUGIN_A_V2 = '''class Plugin:
    """插件 A v2"""
    _ws = None
    @classmethod
    def inject_ws_server(cls, ws): cls._ws = ws
    async def on_message(self, bot_id, event, raw):
        return (False, None)
'''


async def main() -> None:
    from mohobot.interceptors.plugin_system import PluginSystem

    tmp = Path(tempfile.mkdtemp(prefix="plug_"))
    data_dir = tmp / "data"
    data_dir.mkdir()
    ps = PluginSystem(plugins_dir=str(tmp / "plugins"), data_dir=str(data_dir))
    (tmp / "plugins").mkdir()

    # 1. 初始加载
    count = await ps.load_plugins()
    assert count == 0
    print("[1] 空目录加载 OK")

    # 2. 新增插件文件 → 热重载生效
    (tmp / "plugins" / "plugin_a.py").write_text(PLUGIN_A, encoding="utf-8")
    count = await ps.reload_plugins()
    assert count == 1, count
    assert ps.list_plugins()[0]["name"] == "plugin_a"
    print("[2] 新增插件热加载 OK")

    # 3. 注入引用: 热重载后自动重新注入
    injected = {"ws": None}
    ps.set_runtime_refs(ws_server="FAKE_WS")
    await ps.reload_plugins()
    inst = ps._plugins[0]["instance"]
    assert inst._ws == "FAKE_WS", "热重载后应重新注入 ws_server"
    print("[3] 热重载后自动注入 OK")

    # 4. 修改插件文件 → 重载后是新实例
    old_inst = inst
    (tmp / "plugins" / "plugin_a.py").write_text(PLUGIN_A_V2, encoding="utf-8")
    await ps.reload_plugins()
    new_inst = ps._plugins[0]["instance"]
    assert new_inst is not old_inst, "修改后应重新实例化"
    assert new_inst._ws == "FAKE_WS"
    print("[4] 修改插件热重载 OK")

    # 5. 禁用 → 立即生效(dispatch 跳过); 启用 → 立即恢复
    await ps.set_enabled("plugin_a", False)
    assert ps.list_plugins()[0]["enabled"] is False
    await ps.set_enabled("plugin_a", True)
    assert ps.list_plugins()[0]["enabled"] is True
    print("[5] 启停热生效 OK")

    # 6. 删除插件文件 → 重载后移除
    (tmp / "plugins" / "plugin_a.py").unlink()
    await ps.reload_plugins()
    assert len(ps._plugins) == 0
    print("[6] 删除插件热生效 OK")

    print("\nALL PLUGIN HOT-RELOAD TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
