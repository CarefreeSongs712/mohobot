"""查看聊天 —— 从 data/history 归档读最近消息, 合并转发到目标会话。

归档文件每行一个消息事件, 取尾部 N 行即最近 N 条。两类都算聊天记录:
- post_type="message"       收到的消息(MessageHandler 归档)
- post_type="message_sent"  bot 自己发出的消息(WSServer 出站层归档)

这样转发出来是一段完整对话(用户说了什么 + bot 怎么答), 而不是单方独白。
不调用任何历史查询接口(get_group_msg_history 等), 本地无归档时由调用方
提示错误。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from chat_manager_core.onebot import api_ok
from chat_manager_core.target import Target, history_path

# 计入聊天记录的事件类型(收到的消息 + bot 自己发出的消息)
_MESSAGE_POST_TYPES = ("message", "message_sent")

# 批间隔(秒): 转发走 control 优先级(不经框架出站限速队列), 手动间隔防协议端风控
_BATCH_DELAY = 0.5


async def read_recent(
    data_dir: str, bot_id: str, target: Target, count: int,
) -> list[dict]:
    """读归档最近 count 条消息事件; 归档不足 count 条时返回全部(可能为空)。"""
    from mohobot.file_store import jsonl_read_tail

    path = history_path(data_dir, bot_id, target)
    if not path.is_file():
        return []
    lines = await jsonl_read_tail(path, n=max(int(count), 1))
    return [
        event for event in lines
        if isinstance(event, dict)
        and event.get("post_type") in _MESSAGE_POST_TYPES
        and event.get("message") not in (None, "")
    ]


def build_nodes(messages: list[dict]) -> list[dict[str, Any]]:
    """消息事件 → 合并转发节点, 署名取群名片 > 昵称 > QQ。

    同时给出 uin/name 与 user_id/nickname 两套字段名: OneBot 的合并转发
    节点格式属扩展, 不同协议端读的字段名不一致(NapCat 两套都接受)。
    content 直接透传原始 message 段数组, 图片/表情等段型原样保留。
    bot 自己发言(message_sent)的 sender.card 为空, 自然会落到昵称。
    """
    nodes: list[dict[str, Any]] = []
    for event in messages:
        sender = event.get("sender") or {}
        uid = sender.get("user_id") or event.get("user_id") or 0
        name = str(sender.get("card") or sender.get("nickname") or uid)
        nodes.append({
            "type": "node",
            "data": {
                "uin": uid,
                "name": name,
                "user_id": str(uid),
                "nickname": name,
                "content": event.get("message") or "",
            },
        })
    return nodes


async def forward_nodes(
    ws_server, bot_id: str, nodes: list[dict], dest: Target, batch_size: int,
) -> tuple[int, int]:
    """分批把节点转发到 dest, 返回 (成功批数, 失败批数)。

    单批失败只记数不中断 —— 多批场景下前面的批次已经发出去了,
    中断反而让用户不知道发到哪了。
    """
    if not nodes:
        return (0, 0)
    size = int(batch_size) if int(batch_size) > 0 else len(nodes)
    action = "send_group_forward_msg" if dest.is_group else "send_private_forward_msg"
    id_key = "group_id" if dest.is_group else "user_id"
    batches = [nodes[i:i + size] for i in range(0, len(nodes), size)]

    ok = failed = 0
    for index, batch in enumerate(batches, 1):
        if index > 1:
            await asyncio.sleep(_BATCH_DELAY)
        params = {id_key: int(dest.chat_id), "messages": batch}
        if await api_ok(ws_server, bot_id, action, params, timeout=30.0):
            ok += 1
        else:
            failed += 1
            logger.warning(
                f"合并转发失败: {dest.describe()} 第 {index}/{len(batches)} 批"
            )
    return ok, failed
