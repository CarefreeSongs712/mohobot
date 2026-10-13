"""被踢自动拉黑回归 — 真实 Plugin.on_notice / on_request 入口。

覆盖 Issue #7 的第一项（另两项——审批文本注入、批量任务未纳管——不在本文件范围）。

修复的缺陷：

1. ``NoticeHandle.handle`` 调用异步的 ``add_black_group`` / ``add_block_user``
   时漏写 ``await``，协程被丢弃，磁盘名单不写，只产生 never-awaited 警告。
2. 传给 ``add_block_user`` 的是 ``notice.user_id``；在 ``kick_me`` 事件里这个字段是
   **被踢的 bot 自己**，踢人者是 ``notice.operator_id``。只补 ``await`` 会让 bot
   把自己拉黑。
3. ``_persist`` 只写磁盘、未把结果同步回共享的 ``plugin_config`` 字典，而下一次
   入口会用它重建 handler，导致"已落盘但下一申请仍放行"。
4. 名单目标未规范化：``NoticeMessage.from_raw`` 只做 ``str()``，``operator_id="0222"``
   会被原样存档，后续 ``is_block_user("222")`` 匹配不到 —— 管理员收到"已拉黑"提示
   却不生效；``"  222  "`` 又会被旧的 ``isdigit()`` 守卫直接拒掉。
5. ``_persist`` 曾用磁盘快照整体替换 ``_data``：磁盘尚无存档时会把宿主注入但
   未落盘的配置（如 ``notice.max_ban_days``）抹成默认值。

注意 ``NoticeHandle.handle`` 会忽略传入的 event 对象、改用 ``raw`` 重建
``NoticeMessage``，因此本文件一律通过 ``raw`` 驱动事件内容。
只替换远程 WSServer，通知判定、权限、名单存储、插件入口都走真实实现。
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


def _prepare(td, plugin_config):
    """建一个注入真实 WSServer 的插件实例。"""
    module = _load("relationship")
    module.Plugin.inject_data_dir(str(td))
    module.Plugin.inject_ws_server(_WS())
    module.Plugin.inject_admin_ids([7777])
    plugin = module.Plugin()
    plugin.plugin_config = plugin_config
    return module, plugin


def _kick_raw(*, group_id=333, user_id=111, operator_id=222, self_id=111):
    return {"post_type": "notice", "notice_type": "group_decrease", "sub_type": "kick_me",
            "self_id": self_id, "group_id": group_id, "user_id": user_id,
            "operator_id": operator_id, "time": 1}


def _kick_event(raw):
    from mohobot.models.onebot import NoticeEvent
    return NoticeEvent.from_dict(raw)


async def _read_saved(td):
    from mohobot.file_store import json_read
    return await json_read(Path(td) / "plugins_config/relationship.json") or {}


def _lists(saved):
    request = saved.get("request") or {}
    return request.get("group_blacklist", []), request.get("user_blacklist", [])


async def _friend_request(plugin, user_id):
    """真实好友申请入口；返回 WSServer 是否被要求拒绝。"""
    await plugin.on_request("bot_001", SimpleNamespace(), {
        "post_type": "request", "request_type": "friend", "user_id": user_id,
        "flag": "friend-flag", "comment": "hello"})
    return any(action == "set_friend_add_request" for action, _ in plugin._ws_server.calls)


async def test_kick_blacklist_persists_group_and_operator_not_bot():
    """kick_me 后落盘群与操作者、不落 bot 自身, 且后续申请被真实拒绝。"""
    from mohobot.file_store import json_write

    # empty_config 覆盖真实注入空字典的情况(宿主首次加载尚未落盘 schema 默认值)。
    for group_on, user_on, empty_config in (
        (True, True, True), (True, True, False), (True, False, False),
        (False, True, False), (False, False, False),
    ):
        with tempfile.TemporaryDirectory() as td:
            config = {} if empty_config else {
                "notice": {"kick_block_group": group_on, "kick_block_user": user_on}}
            module, plugin = _prepare(td, config)
            # 宿主加载器通常会先把 schema 默认值落盘再进入口钩子; 这里复刻该约定。
            if not empty_config:
                await json_write(Path(td) / "plugins_config/relationship.json", plugin.plugin_config)

            raw = _kick_raw()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await plugin.on_notice("bot_001", _kick_event(raw), raw)

            saved = await _read_saved(td)
            groups, users = _lists(saved)
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
                assert await _friend_request(plugin, 222)

            # 他人被踢的自身通知: 换一个未被拉黑的群号, 若门禁失效就会新增 444,
            # 断言因此具备鉴别力(不再是幂等写入的空断言)。
            other = _kick_raw(group_id=444, user_id=999, operator_id=222)
            await plugin.on_notice("bot_001", _kick_event(other), other)
            after_groups, after_users = _lists(await _read_saved(td))
            assert "444" not in after_groups, "非自身通知不得拉黑群"
            assert after_groups == groups and after_users == users


async def test_kick_blacklist_normalizes_operator_and_group_ids():
    """非规范号码必须归一化后落盘并真正生效, 垃圾号码不得写入名单。"""
    cases = [
        ("0222", "222"),          # 前导零
        ("  222  ", "222"),       # 前后空白(旧的 isdigit 守卫会误拒)
        ("２２２", "222"),         # 全角数字
        (222, "222"),             # 正常整数
    ]
    for operator_id, expected in cases:
        with tempfile.TemporaryDirectory() as td:
            _, plugin = _prepare(td, {"notice": {"kick_block_group": True, "kick_block_user": True}})
            raw = _kick_raw(operator_id=operator_id)
            await plugin.on_notice("bot_001", _kick_event(raw), raw)

            groups, users = _lists(await _read_saved(td))
            assert users == [expected], (operator_id, users)
            assert groups == ["333"], (operator_id, groups)
            # 归一化必须让"已拉黑"真的生效: 规范形式与非规范形式都要被拒。
            assert await _friend_request(plugin, 222), operator_id
            assert await _friend_request(plugin, "0222"), operator_id

    # 非法号码: 不写入任何垃圾项, 也不得抛异常中断整个通知处理。
    for operator_id in ("", "abc", "0", "-1", None):
        with tempfile.TemporaryDirectory() as td:
            _, plugin = _prepare(td, {"notice": {"kick_block_group": False, "kick_block_user": True}})
            raw = _kick_raw(operator_id=operator_id)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await plugin.on_notice("bot_001", _kick_event(raw), raw)
            saved = await _read_saved(td)
            groups, users = _lists(saved)
            assert users == [], (operator_id, users)
            assert not any("never awaited" in str(w.message) for w in caught)

    # 群号非法时不写入 "0"/"None" 之类的垃圾项。
    with tempfile.TemporaryDirectory() as td:
        _, plugin = _prepare(td, {"notice": {"kick_block_group": True, "kick_block_user": False}})
        raw = _kick_raw(group_id=0)
        await plugin.on_notice("bot_001", _kick_event(raw), raw)
        groups, _ = _lists(await _read_saved(td))
        assert groups == [], groups


async def test_blacklist_write_preserves_in_memory_only_config():
    """磁盘尚无存档时, 一次拉黑回写不得把宿主注入的其它配置抹成默认值。"""
    with tempfile.TemporaryDirectory() as td:
        injected = {"notice": {"kick_block_group": True, "kick_block_user": False,
                               "max_ban_days": 9, "block_small_group": True}}
        _, plugin = _prepare(td, injected)
        raw = _kick_raw()
        await plugin.on_notice("bot_001", _kick_event(raw), raw)

        # 名单写成功。
        groups, _ = _lists(await _read_saved(td))
        assert groups == ["333"], groups

        # 但内存配置(以及随后重建 handler 用的 plugin_config)必须完整保留。
        saved_config = plugin.plugin_config
        assert "notice" in saved_config, saved_config
        assert saved_config["notice"].get("max_ban_days") == 9, saved_config
        assert saved_config["notice"].get("block_small_group") is True, saved_config
        assert saved_config["notice"].get("kick_block_user") is False, saved_config


def main():
    import asyncio
    asyncio.run(test_kick_blacklist_persists_group_and_operator_not_bot())
    asyncio.run(test_kick_blacklist_normalizes_operator_and_group_ids())
    asyncio.run(test_blacklist_write_preserves_in_memory_only_config())
    print("\nALL RELATIONSHIP KICK BLACKLIST TESTS PASSED")


if __name__ == "__main__":
    main()
