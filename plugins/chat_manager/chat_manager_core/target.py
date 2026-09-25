"""目标会话解析 —— 群号与 QQ 号都是纯数字, 字面无法区分, 需按上下文判定。

两个命令的判定基准不同:
- /查看聊天: 按本地归档(data/history)判定, 群归档优先
- /发送消息: 按 get_group_list 判定, 在群列表里即群, 否则当私聊
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chat_manager_core.onebot import api_call
from mohobot.history import group_history_files, history_path as archive_path


@dataclass(frozen=True)
class Target:
    """一个目标会话。chat_type 与 data/history 的目录名一致。"""

    chat_type: str  # "group" | "private"
    chat_id: str

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    def describe(self) -> str:
        return f"群 {self.chat_id}" if self.is_group else f"用户 {self.chat_id}"


def parse_chat_id(raw: str) -> str | None:
    """参数 → 纯数字会话 ID; 非数字/空返回 None(不支持 @)。"""
    value = (raw or "").strip()
    return value if value.isdigit() else None


def history_path(data_dir: str, bot_id: str, target: Target) -> Path:
    """共享群归档或按 Bot 隔离的私聊归档路径。"""
    return archive_path(data_dir, bot_id, target.chat_type, target.chat_id)


def resolve_by_history(data_dir: str, bot_id: str, chat_id: str) -> Target | None:
    """按本地归档判定目标, 群归档优先(群号与 QQ 号理论上可能撞号)。

    两边都无归档返回 None —— 调用方据此提示"本地无记录"。
    """
    for chat_type in ("group", "private"):
        candidate = Target(chat_type, chat_id)
        if (group_history_files(data_dir, chat_id) if candidate.is_group
                else history_path(data_dir, bot_id, candidate).is_file()):
            return candidate
    return None


async def resolve_by_group_list(ws_server, bot_id: str, chat_id: str) -> Target | None:
    """按 get_group_list 判定目标: 在群列表里即群, 否则当私聊。

    群列表接口失败返回 None, 调用方必须中止 ——
    不能退化为私聊, 否则会把消息私发给一个恰好等于群号的 QQ 号。
    """
    groups = await api_call(ws_server, bot_id, "get_group_list")
    if not isinstance(groups, list):
        return None
    if any(str((g or {}).get("group_id", "")) == chat_id for g in groups):
        return Target("group", chat_id)
    return Target("private", chat_id)
