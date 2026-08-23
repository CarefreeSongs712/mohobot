"""关系管理器插件 — 移植自 astrbot_plugin_relationship (v3.0.5, Zhalslar)。

帮助管理 QQ 好友与群聊: 群列表/好友列表/退群/删好友/审批员管理、
好友申请与群邀请审批(完整审批流: 审批群/审批员转发 + 引用回复审批)、
抽查聊天记录、通知类事件自动处理(管理员变动/禁言/被踢/被拉群)。

适配 mohobot:
- 命令带 / 前缀; 权限使用全局 admins(配置顶层 admins, 与封禁系统共用)
- OneBot API 通过 ws_server.send_to_bot 调用
- 配置来自插件配置系统(data/plugins_config/relationship.json, 面板可编辑)
"""

from __future__ import annotations

import os
import sys
from typing import Any

from loguru import logger

# 目录插件: 把插件目录加入 sys.path, 用绝对导入加载 core 包
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from relationship_core import config as _cfg
from relationship_core.contact import ContactHandle
from relationship_core.normal import NormalHandle
from relationship_core.notice.handle import NoticeHandle
from relationship_core.request.handle import RequestHandle

# 命令表: 名称 → 处理器方法名(带 / 前缀, 与 mohobot 风格一致)
COMMANDS = {
    "群列表": "cmd_group_list",
    "好友列表": "cmd_friend_list",
    "退群": "cmd_leave_group",
    "删好友": "cmd_delete_friend",
    "删除好友": "cmd_delete_friend",
    "加审批员": "cmd_add_manage_user",
    "减审批员": "cmd_remove_manage_user",
    "同意": "cmd_agree",
    "拒绝": "cmd_refuse",
    "拉黑": "cmd_block",
    "抽查": "cmd_check",
    "推荐": "cmd_contact",
    "批量加群": "cmd_batch_join_group",
    "批量加好友": "cmd_batch_add_friend",
}


class Plugin:
    """Relationship manager — 群聊/好友/申请审批管理。"""

    info = {
        "commands": [
            {"name": "群列表", "desc": "查看 bot 加入的所有群聊(管理员)"},
            {"name": "好友列表", "desc": "查看 bot 的所有好友(管理员)"},
            {"name": "退群", "desc": "退群 <序号|群号|区间> [可批量](管理员)"},
            {"name": "删好友", "desc": "删好友 <@|QQ|序号|区间> [可批量](管理员)"},
            {"name": "加审批员", "desc": "加审批员 @某人(管理员)"},
            {"name": "减审批员", "desc": "减审批员 @某人(管理员)"},
            {"name": "同意", "desc": "同意好友申请或群邀请(引用审批消息, 审批员)"},
            {"name": "拒绝", "desc": "拒绝好友申请或群邀请(引用审批消息, 审批员)"},
            {"name": "拉黑", "desc": "拒绝并拉黑好友申请人或邀请群(引用审批消息, 审批员)"},
            {"name": "抽查", "desc": "抽查 <群号|@群友|@QQ> <数量>(管理员)"},
            {"name": "推荐", "desc": "推荐 <群号/@群友/@qq>"},
            {"name": "批量加群", "desc": "批量加群 <群号1,群号2,...> — 逐个申请加群, 随机延迟(管理员)"},
            {"name": "批量加好友", "desc": "批量加好友 <QQ1,QQ2,...> — 逐个申请加好友, 随机延迟(管理员)"},
        ],
    }

    # 注入引用(PluginSystem.apply_injections)
    _ws_server = None
    _bot_manager = None
    _data_dir = "./data"
    _admin_ids: list[str] = []

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_bot_manager(cls, bot_manager) -> None:
        cls._bot_manager = bot_manager

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    @classmethod
    def inject_admin_ids(cls, admin_ids: list[str]) -> None:
        cls._admin_ids = [str(a) for a in (admin_ids or [])]

    def __init__(self):
        self.plugin_config = {}
        self._cfg = None
        self._normal = None
        self._request = None
        self._notice = None
        self._contact = None
        self._batch_running = False  # 批量加群/加好友任务锁
        # 配置修改串行化锁(跨 _ensure_handlers 重建的 cfg 实例共享,
        # 6 bot 并发 /加审批员 /拉黑 时防止丢更新)
        import asyncio

        self._config_lock = asyncio.Lock()

    def _ensure_handlers(self) -> None:
        """用当前配置 + 当前管理员重建处理器。

        每次入口调用(admin 列表由 inject_admin_ids 类级热更新,
        配置由面板热更新), 保证权限判断始终使用最新值。
        """
        self._cfg = _cfg.PluginConfig(
            self.plugin_config,
            admins=self._admin_ids,
            ws_server=self._ws_server,
            data_dir=self._data_dir,
            lock=self._config_lock,
        )
        self._normal = NormalHandle(self._cfg)
        self._request = RequestHandle(self._cfg)
        self._notice = NoticeHandle(self._cfg)
        self._contact = ContactHandle(self._cfg)

    def on_config_update(self, config: dict) -> None:
        """插件配置热更新回调(面板保存后调用)。"""
        self.plugin_config = config or {}
        self._cfg = None
        logger.info("relationship 插件配置已热更新")

    # ── 消息命令 ──────────────────────────────────────────────

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        text = self._extract_text(event)
        if not text.startswith("/"):
            return (False, None)

        parts = text[1:].strip().split(maxsplit=1)
        if not parts:
            return (False, None)
        cmd = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        handler_name = COMMANDS.get(cmd)
        if handler_name is None:
            return (False, None)

        self._ensure_handlers()

        handler = getattr(self, handler_name, None)
        if handler is None:
            return (False, None)
        try:
            reply = await handler(bot_id, event, rest)
            return (True, reply or None)
        except Exception as e:
            logger.error(f"relationship command {cmd} error: {e}")
            return (True, f"❌ 命令执行出错: {e}")

    # ── 请求事件(好友申请/群邀请) ────────────────────────────

    async def on_request(self, bot_id: str, event: Any, raw: dict) -> bool:
        """接管好友申请/群邀请: 自动规则 + 转发审批。返回 True=已处理。"""
        self._ensure_handlers()
        return await self._request.handle_raw(bot_id, event, raw)

    # ── 通知事件(管理员变动/禁言/被踢/被拉群) ────────────────

    async def on_notice(self, bot_id: str, event: Any, raw: dict) -> None:
        if event.notice_type == "notify" and event.sub_type == "poke":
            return  # 戳一戳交给反射/框架
        self._ensure_handlers()
        try:
            await self._notice.handle(bot_id, event, raw)
        except Exception as e:
            logger.error(f"relationship notice error: {e}")

    # ── 工具 ─────────────────────────────────────────────────

    @staticmethod
    def _extract_text(event: Any) -> str:
        if isinstance(event.message, str):
            return event.message.strip()
        text = ""
        for seg in event.message or []:
            if isinstance(seg, dict) and seg.get("type") == "text":
                text += seg.get("data", {}).get("text", "")
        return text.strip()

    def _check_admin(self, event: Any) -> bool:
        """全局管理员判定(与封禁系统共用)。"""
        return str(event.user_id) in set(self._admin_ids)

    async def _send_reply(self, bot_id: str, event: Any, text: str) -> None:
        """回复到当前会话。"""
        from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent

        if self._ws_server is None:
            return
        if isinstance(event, GroupMessageEvent):
            await self._ws_server.send_group_msg(bot_id, event.group_id, text)
        elif isinstance(event, PrivateMessageEvent):
            await self._ws_server.send_private_msg(bot_id, event.user_id, text)

    # ── 命令实现 ──────────────────────────────────────────────

    async def cmd_group_list(self, bot_id: str, event: Any, rest: str) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        return await self._normal.get_group_list(bot_id)

    async def cmd_friend_list(self, bot_id: str, event: Any, rest: str) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        return await self._normal.get_friend_list(bot_id)

    async def cmd_leave_group(self, bot_id: str, event: Any, rest: str) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        return await self._normal.set_group_leave(bot_id, rest)

    async def cmd_delete_friend(self, bot_id: str, event: Any, rest: str) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        return await self._normal.delete_friend(bot_id, event, rest)

    async def cmd_add_manage_user(self, bot_id: str, event: Any, rest: str) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        return await self._normal.append_manage_user(bot_id, event)

    async def cmd_remove_manage_user(self, bot_id: str, event: Any, rest: str) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        return await self._normal.remove_manage_user(bot_id, event)

    async def cmd_agree(self, bot_id: str, event: Any, rest: str) -> str:
        return await self._request.handle_cmd(bot_id, event, approve=True, extra=rest)

    async def cmd_refuse(self, bot_id: str, event: Any, rest: str) -> str:
        return await self._request.handle_cmd(bot_id, event, approve=False, extra=rest)

    async def cmd_block(self, bot_id: str, event: Any, rest: str) -> str:
        return await self._request.handle_cmd(bot_id, event, approve=False, extra=rest, block=True)

    async def cmd_check(self, bot_id: str, event: Any, rest: str) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        return await self._normal.check_messages(bot_id, event, rest)

    async def cmd_contact(self, bot_id: str, event: Any, rest: str) -> str:
        return await self._contact.contact(bot_id, event, rest)

    # ── 批量加群 / 批量加好友 ────────────────────────────────

    async def cmd_batch_join_group(self, bot_id: str, event: Any, rest: str) -> str:
        """批量加群 <群号1,群号2,...> — 逐个申请, 随机延迟 10~30s(可配置)。"""
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        ids = self._parse_batch_ids(rest)
        if not ids:
            return "用法: /批量加群 <群号1,群号2,...>(逗号分隔)"
        return self._start_batch(bot_id, event, "group", ids)

    async def cmd_batch_add_friend(self, bot_id: str, event: Any, rest: str) -> str:
        """批量加好友 <QQ1,QQ2,...> — 逐个申请, 随机延迟 10~30s(可配置)。"""
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        ids = self._parse_batch_ids(rest)
        if not ids:
            return "用法: /批量加好友 <QQ1,QQ2,...>(逗号分隔)"
        return self._start_batch(bot_id, event, "friend", ids)

    @staticmethod
    def _parse_batch_ids(rest: str) -> list[str]:
        parts = [p.strip() for p in (rest or "").replace("，", ",").split(",")]
        return [p for p in parts if p.isdigit()]

    def _start_batch(
        self, bot_id: str, event: Any, kind: str, ids: list[str],
    ) -> str:
        """启动后台批量任务(同 bot 同时只允许一个批量任务)。"""
        if getattr(self, "_batch_running", False):
            return "⚠️ 已有批量任务在进行中, 请等待完成后再试。"
        self._batch_running = True
        import asyncio as _aio
        _aio.create_task(self._run_batch(bot_id, event, kind, ids))
        total_sec = len(ids) * 20  # 按默认延迟区间估算
        return (
            f"🚀 开始批量{'加群' if kind == 'group' else '加好友'} {len(ids)} 个目标,"
            f"每个间隔随机 10~30 秒, 预计约 {total_sec // 60 + 1} 分钟完成。\n"
            f"完成后会在这里汇报成功/失败统计。"
        )

    async def _run_batch(
        self, bot_id: str, event: Any, kind: str, ids: list[str],
    ) -> None:
        """后台执行: 逐个调用 NapCat 申请接口, 中间随机延迟, 失败跳过继续。"""
        import asyncio
        import random

        ws = self._ws_server
        try:
            if ws is None:
                return
            # 延迟区间(秒), 配置可调
            data = getattr(self, "plugin_config", {}) or {}
            delay_min = max(0, int(data.get("batch_delay_min", 10)))
            delay_max = max(delay_min, int(data.get("batch_delay_max", 30)))

            ok, failed = 0, 0
            failed_list: list[str] = []
            for target in ids:
                try:
                    if kind == "group":
                        resp = await ws.send_to_bot(
                            bot_id, "set_group_add", {"group_id": int(target)},
                            wait_response=True, timeout=10.0,
                        )
                    else:
                        resp = await ws.send_to_bot(
                            bot_id, "set_friend_add", {"user_id": int(target)},
                            wait_response=True, timeout=10.0,
                        )
                    if resp is None or resp.get("status") != "ok" or resp.get("retcode") != 0:
                        failed += 1
                        failed_list.append(target)
                    else:
                        ok += 1
                except Exception as e:
                    logger.warning(f"批量{'加群' if kind == 'group' else '加好友'} {target} 失败: {e}")
                    failed += 1
                    failed_list.append(target)
                await asyncio.sleep(random.uniform(delay_min, delay_max))

            label = "加群" if kind == "group" else "加好友"
            summary = (
                f"📋 批量{label}完成: 成功 {ok} 个, 失败 {failed} 个\n"
                + (f"失败列表: {', '.join(failed_list)}" if failed_list else "全部成功 ✅")
            )
            await self._send_reply(bot_id, event, summary)
        except Exception as e:
            logger.error(f"批量任务异常: {e}")
            try:
                await self._send_reply(bot_id, event, f"❌ 批量任务异常: {e}")
            except Exception:
                pass
        finally:
            self._batch_running = False
