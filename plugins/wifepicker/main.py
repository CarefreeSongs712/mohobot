"""抽老婆插件 — 移植自 astrbot-plugin-wifepicker v3.2.6 (活跃成员抽老婆)。

功能: 今日老婆抽取(活跃池筛选+头像)、我的老婆、强娶(冷却)、关系图(HTML渲染)、
rbq排行、求婚(同意/拒绝 30 秒交互)、管理员重置命令、无前缀关键词触发。

适配 mohobot:
- 命令带 / 前缀(中文指令 + 英文缩写别名); 关键词触发为可配置开关(默认关)
- 数据合并为 data/plugins_data/wifepicker/data.json(WifeStore 原子读写)
- 群消息观察钩子 on_message_observed: 活跃记录/求婚回复/关键词触发
  (框架 gate 前分发所有消息, 无需 @bot)
- 关系图/rbq排行用 Playwright 渲染(生产需安装), 失败降级文本
- 管理员命令使用全局 admins(与封禁/关系插件共用)
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

# 目录插件: 把插件目录加入 sys.path, 用绝对导入加载 core 包
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from wifepicker_core.constants import _DEFAULT_KEYWORD_ROUTES
from wifepicker_core.core import (
    clean_rbq_stats,
    cleanup_inactive,
    draw_excluded_users,
    force_marry_excluded_users,
    format_remaining_seconds,
    get_daily_limit,
    get_active_user_days,
    get_force_marry_cooldown_status,
    get_group_records,
    get_propose_cooldown_status,
    maybe_add_other_half_record,
    trim_active_users,
    upsert_user_wife_record,
)
from wifepicker_core.keyword_trigger import KeywordRouter, MatchMode
from wifepicker_core.store import TRIM_INTERVAL_SECONDS, WifeStore
from wifepicker_core.utils import (
    api_call,
    at_segment,
    extract_target_id,
    get_group_id,
    get_sender_id,
    get_self_id,
    get_text,
    image_url_segment,
    is_allowed_group,
    resolve_member_name,
    text_segment,
)

# 命令表: 命令首词 → 处理方法(带 / 前缀, 别名多键映射)
COMMANDS = {
    "今日老婆": "cmd_draw_wife", "抽老婆": "cmd_draw_wife", "jrlp": "cmd_draw_wife",
    "我的老婆": "cmd_show_history", "wdlp": "cmd_show_history", "抽取历史": "cmd_show_history",
    "强娶": "cmd_force_marry", "qiangqu": "cmd_force_marry",
    "关系图": "cmd_show_graph", "gxt": "cmd_show_graph", "羁绊图谱": "cmd_show_graph",
    "rbq排行": "cmd_rbq_ranking", "rbqph": "cmd_rbq_ranking",
    "抽老婆帮助": "cmd_show_help", "老婆插件帮助": "cmd_show_help", "clpbz": "cmd_show_help",
    "重置记录": "cmd_reset_records", "czjl": "cmd_reset_records",
    "重置强娶时间": "cmd_reset_force_cd", "czqqsj": "cmd_reset_force_cd",
    "重置求婚时间": "cmd_reset_propose_cd", "czqhsj": "cmd_reset_propose_cd",
    "求婚": "cmd_propose", "qh": "cmd_propose",
}

# 后台维护周期(秒): 活跃池清理 + 数据落盘
MAINTENANCE_INTERVAL = 300

# 关键词路由 action → 处理方法(源插件 action 名 → 本插件 cmd_xxx)
_KEYWORD_HANDLERS = {
    "draw_wife": "cmd_draw_wife",
    "show_history": "cmd_show_history",
    "force_marry": "cmd_force_marry",
    "show_graph": "cmd_show_graph",
    "rbq_ranking": "cmd_rbq_ranking",
    "show_help": "cmd_show_help",
    "propose_command": "cmd_propose",
}


class Plugin:
    """活跃成员抽老婆 — 群聊互动插件。"""

    info = {
        "commands": [
            {"name": "今日老婆", "desc": "随机抽取一名今日老婆(别名: 抽老婆/jrlp)"},
            {"name": "我的老婆", "desc": "查看今天抽到的记录及剩余次数(wdlp)"},
            {"name": "强娶", "desc": "强娶 @某人, 有冷却期(qiangqu)"},
            {"name": "关系图", "desc": "生成并发送本群今日老婆关系图谱(gxt)"},
            {"name": "rbq排行", "desc": "近30天被强娶次数排行(rbqph)"},
            {"name": "求婚", "desc": "向群友求婚 @某人, 30秒内回复同意/拒绝(qh)"},
            {"name": "抽老婆帮助", "desc": "查看详细指令说明(clpbz)"},
            {"name": "重置记录", "desc": "清空所有今日抽取记录(管理员)"},
            {"name": "重置强娶时间", "desc": "清空本群强娶冷却 CD(管理员)"},
            {"name": "重置求婚时间", "desc": "清空本群求婚冷却 CD(管理员)"},
        ],
    }

    # 注入引用(PluginSystem.apply_injections)
    _ws_server = None
    _data_dir = "./data"
    _admin_ids: list[str] = []

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    @classmethod
    def inject_admin_ids(cls, admin_ids: list[str]) -> None:
        cls._admin_ids = [str(a) for a in (admin_ids or [])]

    def __init__(self):
        self.plugin_config = {}
        self._plugin_dir = _PLUGIN_DIR
        self._store = None  # 惰性创建(注入 data_dir 之后)
        # 求婚交互内存状态(单实例, 进程内)
        self._propose_requests: dict[str, dict] = {}
        self._force_confirm_requests: dict[str, dict] = {}
        self._keyword_router = KeywordRouter(routes=_DEFAULT_KEYWORD_ROUTES)
        self._maintenance_task: asyncio.Task | None = None
        self._last_maintenance_at = 0.0

    @property
    def store(self) -> WifeStore:
        """数据存储(惰性创建, 确保使用注入后的 data_dir)。"""
        if self._store is None:
            self._store = WifeStore(self._data_dir)
        return self._store

    def on_config_update(self, config: dict) -> None:
        """插件配置热更新回调(面板保存后调用)。"""
        self.plugin_config = config or {}
        # 关键词路由表固定, 无需重建; 开关实时读取
        logger.info("wifepicker 插件配置已热更新")

    # ── 消息观察钩子(所有消息, gate 前) ──────────────────────

    async def on_message_observed(
        self, bot_id: str, event: Any, raw: dict,
    ) -> tuple[bool, str | list[dict] | None]:
        """所有消息先过这里: 活跃记录 → 求婚回复 → 关键词触发。"""
        group_id = get_group_id(event)
        if group_id:
            # 1. 活跃记录(白名单群内发言即入池)
            if is_allowed_group(group_id, self.plugin_config):
                self.store.active_users.setdefault(group_id, {})
                record_active_light(self.store, group_id, get_sender_id(event), get_self_id(event))
                # 周期性落盘(活跃记录高频, 不每条都写盘)
                now = time.time()
                if now - self.store._last_save_at > 120:
                    await self.store.flush(force=True)
            # 2. 求婚回复(同意/拒绝/是/否)
            reply = await self._handle_propose_response(bot_id, event)
            if reply:
                return (True, reply)

        # 3. 无前缀关键词触发(仅群消息, 开关开启时; / 前缀消息走 on_message)
        text = get_text(event)
        if group_id and text and not text.startswith(("/", "!", "！")):
            if self.plugin_config.get("keyword_trigger_enabled", False):
                mode = self._get_keyword_mode()
                route = self._keyword_router.match_route(text, mode=mode)
                if route is None:
                    route = self._keyword_router.match_command_route(text)
                if route:
                    handler_name = _KEYWORD_HANDLERS.get(route.action)
                    handler = getattr(self, handler_name, None) if handler_name else None
                    if handler:
                        result = await handler(bot_id, event, None)
                        return (True, result or None)
        return (False, None)

    def _get_keyword_mode(self) -> MatchMode:
        raw = self.plugin_config.get("keyword_trigger_mode", "exact")
        try:
            return MatchMode(str(raw))
        except ValueError:
            return MatchMode.EXACT

    # ── / 前缀命令 ───────────────────────────────────────────

    async def on_message(
        self, bot_id: str, event: Any, raw: dict,
    ) -> tuple[bool, str | list[dict] | None]:
        text = get_text(event)
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

        # 求婚回复在观察钩子处理, / 命令不走这里; 正常执行命令
        await self._maybe_maintenance()
        handler = getattr(self, handler_name, None)
        if handler is None:
            return (False, None)
        try:
            reply = await handler(bot_id, event, rest)
            return (True, reply or None)
        except Exception as e:
            logger.error(f"wifepicker command {cmd} error: {e}")
            return (True, f"❌ 命令执行出错: {e}")

    # ── 后台维护(活跃池清理 + 落盘) ─────────────────────────

    async def _maybe_maintenance(self) -> None:
        """命令入口触发: 定期清理活跃池 + 落盘(避免常驻任务循环)。"""
        now = time.time()
        if now - self._last_maintenance_at < MAINTENANCE_INTERVAL:
            return
        self._last_maintenance_at = now
        try:
            cleanup_inactive(self.store, self.plugin_config)
            trim_active_users(self.store, self.plugin_config)
            await self.store.flush(force=True)
        except Exception as e:
            logger.warning(f"wifepicker 维护失败: {e}")

    # ── 命令实现 ─────────────────────────────────────────────

    def _check_admin(self, event: Any) -> bool:
        return str(event.user_id) in set(self._admin_ids)

    def _avatar_url(self, qq: str) -> str:
        return f"https://q4.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640"

    async def cmd_draw_wife(self, bot_id: str, event: Any, rest: str | None) -> str | list[dict] | None:
        """今日老婆: 从活跃池(近 N 天发言且在群)随机抽取。"""
        group_id = get_group_id(event)
        if not group_id:
            return "此功能仅在群聊中可用哦~"
        if not is_allowed_group(group_id, self.plugin_config):
            return None

        user_id = get_sender_id(event)
        bot_self_id = get_self_id(event)
        cleanup_inactive(self.store, self.plugin_config, group_id)

        daily_limit = get_daily_limit(self.plugin_config)
        group_records = get_group_records(self.store, group_id)
        user_recs = [r for r in group_records if str(r.get("user_id")) == user_id]
        today_count = len(user_recs)

        if today_count >= daily_limit:
            if daily_limit == 1:
                wife = user_recs[0]
                wife_name, wife_id = wife["wife_name"], wife["wife_id"]
                return [
                    at_segment(user_id),
                    text_segment(f" 你今天已经有老婆了哦❤️~\n她是：【{wife_name}】\n"),
                    image_url_segment(self._avatar_url(wife_id)),
                ]
            return f"你今天已经抽了{today_count}次老婆了，明天再来吧！"

        # 获取当前群成员(过滤退群者)
        members: list[dict] = []
        try:
            members = await api_call(
                self._ws_server, bot_id, "get_group_member_list",
                {"group_id": int(group_id)},
            ) or []
            if not isinstance(members, list):
                members = []
        except Exception as e:
            logger.warning(f"获取群成员列表失败, 使用缓存池: {e}")

        current_member_ids = {str(m.get("user_id")) for m in members}
        active_pool = self.store.active_users.get(group_id, {})
        if not isinstance(active_pool, dict):
            active_pool = {}

        excluded = draw_excluded_users(self.plugin_config)
        if not self.plugin_config.get("allow_marry_bot", False):
            excluded.add(bot_self_id)
        excluded.update([user_id, "0"])

        # 只在"当前还在群里"的活跃用户中抽
        if current_member_ids:
            pool = [
                uid for uid in active_pool.keys()
                if uid not in excluded and uid in current_member_ids
            ]
            # 顺手清理退群者
            removed = [uid for uid in active_pool.keys() if uid not in current_member_ids]
            if removed:
                for uid in removed:
                    active_pool.pop(uid, None)
                self.store.mark_dirty()
        else:
            pool = [uid for uid in active_pool.keys() if uid not in excluded]

        if not pool:
            days = get_active_user_days(self.plugin_config)
            return f"老婆池为空（需有人在{days}天内发言）。"

        wife_id = random.choice(pool)
        wife_name = f"用户({wife_id})"
        user_name = f"用户({user_id})"
        if members:
            wife_name = resolve_member_name(members, wife_id, wife_name)
            user_name = resolve_member_name(members, user_id, user_name)
        else:
            # 群成员列表获取失败 → 框架 get_nickname 兜底(群名片→QQ昵称→数字)
            from wifepicker_core.utils import resolve_name
            wife_name = await resolve_name(self._ws_server, bot_id, group_id, wife_id, wife_name)
            user_name = await resolve_name(self._ws_server, bot_id, group_id, user_id, user_name)

        timestamp = datetime.now().isoformat()
        group_records.append({
            "user_id": user_id,
            "wife_id": wife_id,
            "wife_name": wife_name,
            "timestamp": timestamp,
        })
        maybe_add_other_half_record(
            records=group_records,
            user_id=user_id, user_name=user_name,
            wife_id=wife_id, wife_name=wife_name,
            enabled=bool(self.plugin_config.get("auto_set_other_half", False)),
            timestamp=timestamp,
        )
        await self.store.flush(force=True)

        suffix = "\n请好好对待她哦❤️~ \n" + f"剩余抽取次数：{max(0, daily_limit - today_count - 1)}次"
        msg: list[dict] = [
            at_segment(user_id),
            text_segment(f" 你的今日老婆是：\n\n【{wife_name}】\n"),
        ]
        if self.plugin_config.get("at_waifu", False):
            msg.append(at_segment(wife_id))
            msg.append(text_segment(" "))
        msg.extend([
            image_url_segment(self._avatar_url(wife_id)),
            text_segment(suffix),
        ])
        return msg

    async def cmd_show_history(self, bot_id: str, event: Any, rest: str | None):
        from wifepicker_core.command.my_wife import cmd_show_history
        return await cmd_show_history(self, bot_id, event, rest)

    async def cmd_force_marry(
        self, bot_id: str, event: Any, rest: str | None,
        target_id_override: str | None = None,
    ) -> str | list[dict] | None:
        """强娶 @某人(有冷却)。target_id_override 供求婚拒绝后确认强娶使用。"""
        group_id = get_group_id(event)
        if not group_id:
            return "此功能仅在群聊中可用哦~"
        if not is_allowed_group(group_id, self.plugin_config):
            return None

        user_id = get_sender_id(event)
        bot_self_id = get_self_id(event)
        config = self.plugin_config

        # 求婚冷却(强娶也受求婚冷却约束)
        user_propose_cd = get_propose_cooldown_status(self.store, group_id, user_id)
        if user_propose_cd:
            return f"你还在求婚冷却期内，请等待 {format_remaining_seconds(user_propose_cd['remaining'])} 后再强娶。"
        user_force_cd = get_force_marry_cooldown_status(self.store, group_id, user_id, config)
        if user_force_cd:
            reset_text = user_force_cd["reset_dt"].strftime("%m-%d %H:%M")
            return (f"你已经强娶过啦！\n请等待：{format_remaining_seconds(user_force_cd['remaining'])}后再试。\n"
                    f"(重置时间：{reset_text})")

        target_id = target_id_override or extract_target_id(event)
        if not target_id or target_id == "all":
            return "请 @ 一个你想强娶的人。"
        if target_id == user_id:
            return "不能娶自己！"

        target_propose_cd = get_propose_cooldown_status(self.store, group_id, target_id)
        if target_propose_cd:
            return f"对方还在求婚冷却期内，请等待 {format_remaining_seconds(target_propose_cd['remaining'])} 后再强娶。"

        force_excluded = force_marry_excluded_users(config)
        if not config.get("allow_marry_bot", False):
            force_excluded.add(bot_self_id)
        force_excluded.add("0")
        if target_id in force_excluded:
            return "该用户在强娶排除列表中，无法被强娶。"

        # 名字
        target_name = f"用户({target_id})"
        user_name = f"用户({user_id})"
        members: list[dict] = []
        try:
            members = await api_call(
                self._ws_server, bot_id, "get_group_member_list",
                {"group_id": int(group_id)},
            ) or []
            if isinstance(members, list):
                target_name = resolve_member_name(members, target_id, target_name)
                user_name = resolve_member_name(members, user_id, user_name)
        except Exception:
            pass
        if not members:
            # 群成员列表获取失败 → 框架 get_nickname 兜底(群名片→QQ昵称→数字)
            from wifepicker_core.utils import resolve_name
            target_name = await resolve_name(self._ws_server, bot_id, group_id, target_id, target_name)
            user_name = await resolve_name(self._ws_server, bot_id, group_id, user_id, user_name)

        group_records = get_group_records(self.store, group_id)

        # rbq 统计
        stats_group = self.store.rbq_stats.setdefault(group_id, {})
        stats_group.setdefault(target_id, []).append(time.time())
        clean_rbq_stats(self.store)

        timestamp = datetime.now().isoformat()
        upsert_user_wife_record(
            group_records,
            user_id=user_id, wife_id=target_id, wife_name=target_name,
            timestamp=timestamp,
            daily_limit=get_daily_limit(config),
        )
        maybe_add_other_half_record(
            records=group_records,
            user_id=user_id, user_name=user_name,
            wife_id=target_id, wife_name=target_name,
            enabled=bool(config.get("auto_set_other_half", False)),
            timestamp=timestamp,
        )

        # 强娶冷却
        self.store.forced_marriage.setdefault(group_id, {})[user_id] = time.time()
        await self.store.flush(force=True)

        return [
            at_segment(user_id),
            text_segment(f" 你今天强娶了【{target_name}】哦❤️~\n请对她好一点哦~。\n"),
            image_url_segment(self._avatar_url(target_id)),
        ]

    async def cmd_show_graph(self, bot_id: str, event: Any, rest: str | None):
        from wifepicker_core.command.relationdiagram import cmd_show_graph
        return await cmd_show_graph(self, bot_id, event, rest)

    async def cmd_rbq_ranking(self, bot_id: str, event: Any, rest: str | None):
        from wifepicker_core.command.rbqrank import cmd_rbq_ranking
        return await cmd_rbq_ranking(self, bot_id, event, rest)

    async def cmd_show_help(self, bot_id: str, event: Any, rest: str | None):
        from wifepicker_core.command.help import cmd_show_help
        return await cmd_show_help(self, bot_id, event, rest)

    async def cmd_propose(self, bot_id: str, event: Any, rest: str | None):
        from wifepicker_core.command.propose import cmd_propose
        return await cmd_propose(self, bot_id, event, rest)

    async def _handle_propose_response(self, bot_id: str, event: Any):
        from wifepicker_core.command.propose import handle_propose_response
        return await handle_propose_response(self, bot_id, event)

    # ── 管理员命令 ───────────────────────────────────────────

    async def cmd_reset_records(self, bot_id: str, event: Any, rest: str | None) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        self.store.records["date"] = datetime.now().strftime("%Y-%m-%d")
        self.store.records["groups"] = {}
        await self.store.flush(force=True)
        return "今日抽取记录已重置！"

    async def cmd_reset_force_cd(self, bot_id: str, event: Any, rest: str | None) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        group_id = get_group_id(event)
        if group_id and group_id in self.store.forced_marriage:
            self.store.forced_marriage[group_id] = {}
            await self.store.flush(force=True)
            logger.info(f"[Wife] 已重置群 {group_id} 的强娶冷却时间")
            return "✅ 本群强娶冷却时间已重置！现在大家可以再次强娶了。"
        return "💡 本群目前没有人在冷却期内。"

    async def cmd_reset_propose_cd(self, bot_id: str, event: Any, rest: str | None) -> str:
        if not self._check_admin(event):
            return "❌ 你没有权限执行此操作。"
        from wifepicker_core.command.reset_propose_cd import cmd_reset_propose_cd
        return await cmd_reset_propose_cd(self, bot_id, event, rest)


def record_active_light(store: WifeStore, group_id: str, user_id: str, bot_id: str) -> None:
    """活跃记录轻量版(高频, 仅内存 + dirty)。"""
    uid, bot = str(user_id), str(bot_id)
    if uid == bot or uid == "0":
        return
    group = store.active_users.setdefault(group_id, {})
    if not isinstance(group, dict):
        group = {}
        store.active_users[group_id] = group
    group[uid] = time.time()
    store.mark_dirty()
