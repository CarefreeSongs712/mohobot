"""Issue #155 回归: 求婚计时器接入任务监管, 禁用/卸载后不再发提醒。

缺陷: ``cmd_propose`` 用裸 ``asyncio.create_task`` 起 30 秒超时提醒, 没有登记
TaskSupervisor, ``on_shutdown`` 也不取消。计时期间禁用插件后, 旧实例仍会通过
持有的 ws_server 往群里发超时提醒。

修复: 计时任务经 ``Plugin._spawn_task`` 优先登记到注入的 TaskSupervisor
(框架会归一为 ``plugin:wifepicker:*`` owner, 禁用/卸载按前缀取消); 无
supervisor 时本地持有引用并在 ``on_shutdown`` 取消, 同时清理待处理请求。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins" / "wifepicker"))

from mohobot.interceptors.plugin_system import _PluginTaskSupervisor  # noqa: E402
from mohobot.models.onebot import GroupMessageEvent, Sender  # noqa: E402
from mohobot.services.task_supervisor import TaskSupervisor  # noqa: E402
from wifepicker_core.command import propose as propose_module  # noqa: E402

BOT = "bot_001"
GROUP = "888888"

BASE_CONFIG = {
    "daily_limit": 1,
    "force_marry_cd": 3,
    "propose_cooldown_minutes": 60,
    "max_records": 500,
    "active_user_days": 30,
    "excluded_users": [],
    "force_marry_excluded_users": [],
    "keyword_trigger_enabled": False,
    "keyword_trigger_mode": "exact",
    "allow_marry_bot": False,
    "at_waifu": False,
    "auto_set_other_half": False,
    "whitelist_groups": [],
    "blacklist_groups": [],
    "iterations": 140,
}


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=10.0):
        if action == "get_group_member_list":
            return {"status": "ok", "retcode": 0, "data": []}
        if action == "get_group_info":
            return {"status": "ok", "retcode": 0, "data": {"group_name": "测试群"}}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send_group_msg(self, bot_id, group_id, message, source="auto"):
        self.sent.append(("group", group_id, message))

    async def send_private_msg(self, bot_id, user_id, message, source="auto"):
        self.sent.append(("private", user_id, message))

    async def send_image(self, *args, **kwargs):
        return None


def _load_plugin_main():
    path = ROOT / "plugins" / "wifepicker" / "main.py"
    spec = importlib.util.spec_from_file_location("wifepicker_plugin_main_sup", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_plugin(tmp, supervisor=None):
    module = _load_plugin_main()
    module.Plugin.inject_ws_server(FakeWS())
    module.Plugin.inject_data_dir(tmp)
    module.Plugin.inject_admin_ids(["1001"])
    module.Plugin.inject_task_supervisor(supervisor)
    plugin = module.Plugin()
    plugin.plugin_config = dict(BASE_CONFIG)
    return plugin


def _propose_event(user_id=2001, target_id=1002):
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=int(GROUP),
        sender=Sender(user_id=user_id),
        message=[
            {"type": "text", "data": {"text": "/求婚"}},
            {"type": "at", "data": {"qq": str(target_id)}},
        ],
    )


def _group_texts(ws):
    return [m for kind, _, m in ws.sent if kind == "group"]


async def test_propose_timer_registered_with_supervisor(monkeypatch):
    monkeypatch.setattr(propose_module, "PROPOSE_RESPONSE_SECONDS", 0.05)
    supervisor = TaskSupervisor()
    with tempfile.TemporaryDirectory() as td:
        plugin = _make_plugin(td, _PluginTaskSupervisor(supervisor, "wifepicker"))
        handled, reply = await plugin.on_message(BOT, _propose_event(), {})
        assert handled and "发起了求婚" in reply, reply

        owners = list(supervisor._owners)
        assert any(o.startswith("plugin:wifepicker") for o in owners), owners
        assert any(
            task.get_name().startswith("wifepicker:propose-timeout:")
            for tasks in supervisor._owners.values() for task in tasks
        )

        await asyncio.sleep(0.2)
        assert any("求婚超时" in str(m) for m in _group_texts(plugin._ws_server)), \
            plugin._ws_server.sent
        assert not plugin._propose_requests.get(GROUP)
    await supervisor.shutdown()


async def test_disable_cancels_timer_and_does_not_send(monkeypatch):
    """PluginSystem 禁用/卸载时按 owner 前缀取消, 计时器不得再发提醒。"""
    monkeypatch.setattr(propose_module, "PROPOSE_RESPONSE_SECONDS", 0.2)
    supervisor = TaskSupervisor()
    wrapped = _PluginTaskSupervisor(supervisor, "wifepicker")
    with tempfile.TemporaryDirectory() as td:
        plugin = _make_plugin(td, wrapped)
        await plugin.on_message(BOT, _propose_event(), {})

        # 禁用插件时 PluginSystem 的行为: cancel_owner_prefix("plugin:wifepicker")
        await supervisor.cancel_owner_prefix("plugin:wifepicker")

        await asyncio.sleep(0.35)
        assert _group_texts(plugin._ws_server) == [], plugin._ws_server.sent
    await supervisor.shutdown()


async def test_on_shutdown_cancels_fallback_timer(monkeypatch):
    """未注入 supervisor 时也必须由 on_shutdown 取消本地计时任务。"""
    monkeypatch.setattr(propose_module, "PROPOSE_RESPONSE_SECONDS", 0.2)
    with tempfile.TemporaryDirectory() as td:
        plugin = _make_plugin(td)  # no supervisor
        await plugin.on_message(BOT, _propose_event(), {})
        assert plugin._background_tasks, "计时任务应被实例持有"

        await plugin.on_shutdown()

        assert not plugin._background_tasks
        assert plugin._propose_requests == {}
        assert plugin._force_confirm_requests == {}
        await asyncio.sleep(0.35)
        assert _group_texts(plugin._ws_server) == [], plugin._ws_server.sent


async def test_spawn_task_when_supervisor_stopping_is_safe(monkeypatch):
    """宿主正在关闭时登记任务不应抛异常, 也不留下会发送的计时器。"""
    monkeypatch.setattr(propose_module, "PROPOSE_RESPONSE_SECONDS", 0.1)
    supervisor = TaskSupervisor()
    await supervisor.shutdown()  # stopping=True
    with tempfile.TemporaryDirectory() as td:
        plugin = _make_plugin(td, _PluginTaskSupervisor(supervisor, "wifepicker"))
        handled, reply = await plugin.on_message(BOT, _propose_event(), {})
        assert handled, reply
        # 任务无法登记时不能告诉用户"求婚已发起", 也不能留下待处理请求。
        assert "发起了求婚" not in reply, reply
        assert "未生效" in reply, reply
        assert plugin._propose_requests == {}
        assert not plugin._background_tasks

        await asyncio.sleep(0.25)
        assert _group_texts(plugin._ws_server) == [], plugin._ws_server.sent


async def test_propose_agree_clears_pending_before_timer_fires(monkeypatch):
    """同意后计时器到点不得再发超时提醒(请求已清除)。"""
    monkeypatch.setattr(propose_module, "PROPOSE_RESPONSE_SECONDS", 0.05)
    supervisor = TaskSupervisor()
    with tempfile.TemporaryDirectory() as td:
        plugin = _make_plugin(td, _PluginTaskSupervisor(supervisor, "wifepicker"))
        await plugin.on_message(BOT, _propose_event(), {})
        assert plugin._propose_requests.get(GROUP)

        agree = GroupMessageEvent(
            time=0, self_id=1000, post_type="message", message_type="group",
            message_id=2, user_id=1002, group_id=int(GROUP),
            sender=Sender(user_id=1002),
            message=[{"type": "text", "data": {"text": "同意"}}],
        )
        handled, reply = await plugin.on_message_observed(BOT, agree, {})
        assert handled and "结为夫妻" in reply, reply

        await asyncio.sleep(0.2)
        assert not any("求婚超时" in str(m) for m in _group_texts(plugin._ws_server))
    await supervisor.shutdown()
