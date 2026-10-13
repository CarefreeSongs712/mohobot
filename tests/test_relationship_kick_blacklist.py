"""被踢自动拉黑回归 — 真实 Plugin.on_notice / on_request 入口。

覆盖 Issue #7 的第一项（另两项——审批文本注入、批量任务未纳管——不在本文件范围）。

两个缺陷：

1. ``NoticeHandle.handle`` 调用异步的 ``add_black_group`` / ``add_block_user``
   时漏写 ``await``，协程被丢弃，磁盘名单不写，只产生 never-awaited 警告。
2. 传给 ``add_block_user`` 的是 ``notice.user_id``；在 ``kick_me`` 事件里这个字段是
   **被踢的 bot 自己**，踢人者是 ``notice.operator_id``。只补 ``await`` 会让 bot
   把自己拉黑。

另外覆盖修复过程中发现的第二层缺口：``_persist`` 只写磁盘、未把结果同步回共享的
``plugin_config`` 字典，而下一次入口会用它重建 handler，导致“已落盘但下一申请仍放行”。

只替换远程 I/O（假 WSServer），通知判定、权限、名单存储、插件入口都走真实实现。
"""
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import warnings

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name: str):
    """按插件目录形态加载 plugins/<name>/main.py。"""
    directory = ROOT / "plugins" / name
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location("audit_regression_" + name, directory / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _WS:
    """只记录 OneBot 调用，不做真实网络。"""

    def __init__(self, manager=None):
        self._bot_manager = manager
        self.sent = []
        self.calls = []

    async def send_to_bot(self, bot_id, action, params=None, **kw):
        self.calls.append((action, params))
        data = {"group_name": "fixture", "nickname": "operator"}
        return {"status": "ok", "retcode": 0, "data": data}

    async def send_group_msg(self, *args, **kw):
        self.sent.append(args)

    async def send_private_msg(self, *args, **kw):
        self.sent.append(args)


async def test_kick_blacklist_persists_group_and_operator_not_bot():
    """kick_me 后落盘群与操作者、不落 bot 自身, 且后续申请被真实拒绝。"""
    from mohobot.file_store import json_read, json_write
    from mohobot.models.onebot import NoticeEvent

    module = _load("relationship")
    # empty_config 覆盖真实注入空字典的情况(宿主首次加载尚未落盘 schema 默认值)。
    for group_on, user_on, empty_config in (
        (True, True, True), (True, True, False), (True, False, False),
        (False, True, False), (False, False, False),
    ):
        with tempfile.TemporaryDirectory() as td:
            module.Plugin.inject_data_dir(td)
            module.Plugin.inject_ws_server(_WS())
            module.Plugin.inject_admin_ids([7777])
            plugin = module.Plugin()
            plugin.plugin_config = {} if empty_config else {
                "notice": {"kick_block_group": group_on, "kick_block_user": user_on}}
            # 宿主加载器通常会先把 schema 默认值落盘再进入口钩子; 这里复刻该约定。
            if not empty_config:
                await json_write(Path(td) / "plugins_config/relationship.json", plugin.plugin_config)

            notice = NoticeEvent(time=1, self_id=111, post_type="notice", notice_type="group_decrease",
                                 sub_type="kick_me", group_id=333, user_id=111, operator_id=222)
            raw = {"post_type": "notice", "notice_type": "group_decrease", "sub_type": "kick_me",
                   "self_id": 111, "group_id": 333, "user_id": 111, "operator_id": 222}
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await plugin.on_notice("bot_001", notice, raw)

            saved = await json_read(Path(td) / "plugins_config/relationship.json") or {}
            groups = saved.get("request", {}).get("group_blacklist", [])
            users = saved.get("request", {}).get("user_blacklist", [])
            assert ("333" in groups) == group_on, (group_on, saved)
            assert ("222" in users) == user_on, (user_on, saved)
            assert "111" not in users, "只补 await 会把 bot 自己拉黑"
            assert not any("never awaited" in str(w.message) for w in caught)

            # 不人为构造判定结果: 用真实后续申请验证磁盘+共享快照都已更新。
            if group_on:
                assert await plugin.on_request("bot_001", SimpleNamespace(), {
                    "post_type": "request", "request_type": "group", "sub_type": "invite",
                    "user_id": 222, "group_id": 333, "flag": "group-flag", "comment": "hello"})
                assert ("set_group_add_request",
                        {"flag": "group-flag", "sub_type": "invite", "approve": False}) in module.Plugin._ws_server.calls
            if user_on:
                assert await plugin.on_request("bot_001", SimpleNamespace(), {
                    "post_type": "request", "request_type": "friend", "user_id": 222,
                    "flag": "friend-flag", "comment": "hello"})
                assert ("set_friend_add_request",
                        {"flag": "friend-flag", "approve": False}) in module.Plugin._ws_server.calls

            # 他人被踢的自身通知(user_id != self_id)不得产生任何效果。
            raw["user_id"] = 999
            await plugin.on_notice("bot_001", notice, raw)
            assert (await json_read(Path(td) / "plugins_config/relationship.json") or {}) == saved


def main():
    import asyncio
    asyncio.run(test_kick_blacklist_persists_group_and_operator_not_bot())
    print("\nALL RELATIONSHIP KICK BLACKLIST TESTS PASSED")


if __name__ == "__main__":
    main()
