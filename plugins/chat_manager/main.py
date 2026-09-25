"""聊天记录管理插件 — 查看指定会话的聊天记录、以 bot 身份代发消息。

命令(均仅全局管理员可用, 带 / 前缀; 群内多 bot 时只由一个 bot 响应):
- /查看聊天 <QQ号|群号> [条数]  — 从 data/history 归档读最近条数(默认 20,
  不足则全部; 含用户发言与 bot 自己发出的 message_sent), 合并转发到**当前会话**。
  转发前剥离 reply 引用段(目标会话无法解析, 会导致整批被拒); 图片/语音等
  URL 过期导致整批失败时, 自动降级为纯文本占位重试一次
- /发送消息 <QQ号|群号> <内容>  — 把纯文本发送到指定会话

目标自动判定(群号与 QQ 号都是纯数字, 字面无法区分):
- /查看聊天 按本地归档判定, 群归档优先
- /发送消息 按 get_group_list 判定, 在群列表里即群, 否则当私聊

不调用任何历史查询接口, 只读本地归档; 无归档时明确报错。
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

from chat_manager_core.records import forward_messages, read_recent
from chat_manager_core.sender import send_plain_text
from chat_manager_core.target import (
    Target,
    parse_chat_id,
    resolve_by_group_list,
    resolve_by_history,
)

# 命令表: 名称 → 处理器方法名(带 / 前缀)
COMMANDS = {
    "查看聊天": "cmd_view_chat",
    "发送消息": "cmd_send_message",
}

_USAGE_VIEW = "用法: /查看聊天 <QQ号|群号> [条数]"
_USAGE_SEND = "用法: /发送消息 <QQ号|群号> <内容>"
_BAD_TARGET = "❌ 目标必须是纯数字的 QQ 号或群号(不支持 @)。"
_NO_PERMISSION = "❌ 你没有权限执行此操作。"


class Plugin:
    """聊天记录查看与代发(/查看聊天、/发送消息, 仅管理员)。"""

    # 群内多 bot 时只由一个 bot 响应(与 /help、/status 等全局指令同机制)。
    # 注意必须带 / 前缀 —— 框架用这些词去匹配整条消息文本(如 "/查看聊天 群号")
    global_triggers = {"/查看聊天", "/发送消息"}

    info = {
        "commands": [
            {"name": "查看聊天", "desc": "查看聊天 <QQ号|群号> [条数] — 合并转发最近聊天记录(默认 20)(管理员)"},
            {"name": "发送消息", "desc": "发送消息 <QQ号|群号> <内容> — 以 bot 身份发送一条纯文本(管理员)"},
        ],
    }

    # 注入引用(PluginSystem.apply_injections), 必须是 classmethod
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

    # 配置(schema 默认值; 面板保存后由框架注入 plugin_config)
    _DEFAULTS = {"default_count": 20, "batch_size": 40}

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)

    def on_config_update(self, config: dict) -> None:
        """配置热更新回调(同步调用)。"""
        self.plugin_config = config or {}

    # ── 消息入口 ──────────────────────────────────────────────

    async def on_message(
        self, bot_id: str, event: Any, raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        text = self._extract_text(event)
        if not text.startswith("/"):
            return (False, None)

        parts = text[1:].strip().split(maxsplit=1)
        if not parts:
            return (False, None)
        handler_name = COMMANDS.get(parts[0])
        if handler_name is None:
            return (False, None)

        if not self._is_admin(event):
            return (True, _NO_PERMISSION)

        rest = parts[1] if len(parts) > 1 else ""
        try:
            reply = await getattr(self, handler_name)(bot_id, event, rest)
            return (True, reply or None)
        except Exception as e:
            logger.exception(f"chat_manager 命令 {parts[0]} 执行异常")
            return (True, f"❌ 命令执行出错: {e}")

    # ── /查看聊天 ─────────────────────────────────────────────

    async def cmd_view_chat(self, bot_id: str, event: Any, rest: str) -> str | None:
        """/查看聊天 <QQ号|群号> [条数] — 合并转发最近聊天记录到当前会话。

        转发本身即回复, 成功时不再补文字(返回 None)。
        """
        parts = (rest or "").split()
        if not parts:
            return _USAGE_VIEW
        if len(parts) > 2:
            return f"❌ 参数过多。{_USAGE_VIEW}"

        chat_id = parse_chat_id(parts[0])
        if chat_id is None:
            return _BAD_TARGET

        count = self._cfg("default_count", 20)
        if len(parts) == 2:
            if not parts[1].isdigit() or int(parts[1]) <= 0:
                return "❌ 条数必须是正整数。"
            count = int(parts[1])

        target = resolve_by_history(self._data_dir, bot_id, chat_id)
        if target is None:
            return (
                f"❌ 本地没有 {chat_id} 的聊天记录"
                "(只能查看 data/history 里已归档的会话)。"
            )

        messages = await read_recent(self._data_dir, bot_id, target, count)
        if not messages:
            return f"❌ {target.describe()} 的本地归档为空。"

        dest = self._current_session(event)
        if dest is None:
            return "❌ 无法确定当前会话, 已取消转发。"

        ok, failed = await forward_messages(
            self._ws_server, bot_id, messages, dest, self._cfg("batch_size", 40),
        )
        logger.info(
            f"/查看聊天 {target.describe()} 最近 {len(messages)} 条 → "
            f"{dest.describe()}: {ok} 批成功, {failed} 批失败"
        )
        if failed and not ok:
            return f"❌ 转发失败(共 {failed} 批), 请检查协议端连接。"
        if failed:
            return f"⚠️ 已转发 {len(nodes)} 条, 其中 {failed} 批失败(成功 {ok} 批)。"
        return None

    # ── /发送消息 ─────────────────────────────────────────────

    async def cmd_send_message(self, bot_id: str, event: Any, rest: str) -> str:
        """/发送消息 <QQ号|群号> <内容> — 以 bot 身份发送一条纯文本。"""
        parts = (rest or "").split(maxsplit=1)
        if len(parts) < 2:
            return _USAGE_SEND

        chat_id = parse_chat_id(parts[0])
        if chat_id is None:
            return _BAD_TARGET

        content = parts[1].strip()
        if not content:
            return "❌ 消息内容不能为空。"

        target = await resolve_by_group_list(self._ws_server, bot_id, chat_id)
        if target is None:
            return "❌ 获取群列表失败, 无法判定目标是群还是好友, 已取消发送。"

        if await send_plain_text(self._ws_server, bot_id, target, content):
            logger.info(f"/发送消息 → {target.describe()} ({len(content)} 字)")
            return f"✅ 已发送到{target.describe()}({len(content)} 字)"
        return (
            f"❌ 发送失败: 请确认 bot 已在该群 / 已添加该好友"
            f"({target.describe()})。"
        )

    # ── 工具 ─────────────────────────────────────────────────

    def _cfg(self, key: str, default: int) -> int:
        """读插件配置项并转 int; 缺失/空/非法时回退默认值。"""
        value = (getattr(self, "plugin_config", None) or {}).get(key, default)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _is_admin(self, event: Any) -> bool:
        """全局管理员判定(全局配置 admins, 与封禁系统共用)。"""
        return str(getattr(event, "user_id", "") or "") in set(self._admin_ids)

    @staticmethod
    def _current_session(event: Any) -> Target | None:
        """命令所在会话(群聊→该群; 私聊→该用户), 作为查看聊天的转发目标。"""
        from mohobot.models.onebot import GroupMessageEvent

        if isinstance(event, GroupMessageEvent):
            return Target("group", str(event.group_id))
        user_id = str(getattr(event, "user_id", "") or "")
        return Target("private", user_id) if user_id else None

    @staticmethod
    def _extract_text(event: Any) -> str:
        """取消息纯文本(str 或 OneBot 段数组)。"""
        message = getattr(event, "message", "")
        if isinstance(message, str):
            return message.strip()
        text = ""
        for seg in message or []:
            if isinstance(seg, dict) and seg.get("type") == "text":
                text += seg.get("data", {}).get("text", "")
        return text.strip()
