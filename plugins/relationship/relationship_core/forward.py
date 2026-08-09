"""转发工具 — 移植自 astrbot_plugin_relationship (core/forward.py)。

抽查聊天记录: get_group_msg_history/get_friend_msg_history → 构造转发节点 →
send_group_forward_msg/send_private_forward_msg 分批发到当前会话。
"""

from __future__ import annotations

import random
from typing import Any

from loguru import logger

from relationship_core.utils import api_call, get_ats, parse_multi_input


class ForwardTool:
    @staticmethod
    def _make_nodes(messages: list[dict]) -> list[dict[str, Any]]:
        """消息 → 转发节点(OneBot node 段)。"""
        nodes = []
        for message in messages:
            sender = message.get("sender") or {}
            nodes.append({
                "type": "node",
                "data": {
                    "name": sender.get("nickname") or "未知",
                    "uin": sender.get("user_id") or 0,
                    "content": message.get("message") or "",
                },
            })
        return nodes

    @staticmethod
    async def _get_msg_history(
        ws_server, bot_id: str, count: int,
        group_id: int | None = None, user_id: int | None = None,
    ) -> list[dict] | None:
        """获取消息历史, 群消息优先。"""
        result = None
        if group_id:
            result = await api_call(
                ws_server, bot_id, "get_group_msg_history",
                {"group_id": group_id, "count": count},
            )
        elif user_id:
            result = await api_call(
                ws_server, bot_id, "get_friend_msg_history",
                {"user_id": user_id, "count": count},
            )
        if isinstance(result, dict) and "messages" in result:
            return result["messages"]
        return result if isinstance(result, list) else None

    @staticmethod
    async def _forward_messages(
        ws_server, bot_id: str, messages: list[dict],
        group_id: int | None = None, user_id: int | None = None, batch_size: int = 0,
    ) -> None:
        """转发消息(支持分批)。"""
        if batch_size <= 0:
            batch_size = len(messages)

        for i in range(0, len(messages), batch_size):
            batch = messages[i:i + batch_size]
            try:
                if group_id:
                    await api_call(
                        ws_server, bot_id, "send_group_forward_msg",
                        {"group_id": group_id, "messages": batch}, timeout=30,
                    )
                elif user_id:
                    await api_call(
                        ws_server, bot_id, "send_private_forward_msg",
                        {"user_id": user_id, "messages": batch}, timeout=30,
                    )
                logger.debug(f"转发消息成功（第{i // batch_size + 1}批）")
            except Exception:
                logger.exception(f"转发消息失败（第{i // batch_size + 1}批）")

    @staticmethod
    async def source_forward(
        ws_server, bot_id: str, *, count: int,
        source_group_id: int | None = None, source_user_id: int | None = None,
        forward_group_id: int | None = None, forward_user_id: int | None = None,
        batch_size: int = 0,
    ) -> bool:
        """把源会话最近 count 条消息转发到目标会话。"""
        try:
            messages = await ForwardTool._get_msg_history(
                ws_server, bot_id,
                group_id=source_group_id, user_id=source_user_id, count=count,
            )
            if not messages:
                return False
            nodes = ForwardTool._make_nodes(messages)
            await ForwardTool._forward_messages(
                ws_server, bot_id, nodes,
                group_id=forward_group_id, user_id=forward_user_id,
                batch_size=batch_size,
            )
            return True
        except Exception:
            return False

    @staticmethod
    async def check_messages(
        ws_server, bot_id: str, *, at_ids: list[str], target_arg: str, count: int,
        reply_group_id: int, reply_user_id: int, batch_size: int = 0,
    ) -> None:
        """抽查指定群/用户最近 count 条消息, 转发到当前会话。"""
        sgid: int | None = None
        suid: int | None = None

        # 1. @ 用户优先
        if at_ids:
            suid = int(at_ids[0])

        # 2. 文本解析(序号/群号)
        if not suid and target_arg:
            group_list = await api_call(ws_server, bot_id, "get_group_list")
            if not isinstance(group_list, list):
                group_list = []
            indexes, ids = parse_multi_input(target_arg, total=len(group_list))
            if indexes:
                sgid = int(group_list[min(indexes)].get("group_id"))
            elif ids:
                value = next(iter(ids))
                if value.isdigit():
                    sgid = int(value)

        # 3. 兜底: 随机群
        if not sgid and not suid:
            group_list = await api_call(ws_server, bot_id, "get_group_list")
            if not isinstance(group_list, list) or not group_list:
                raise RuntimeError("未找到可用的群聊或用户，无法进行抽查")
            sgid = int(random.choice(group_list).get("group_id"))

        logger.debug(f"正在抽查{f'群({sgid})' if sgid else f'用户({suid})'}的 {count} 条聊天记录...")

        ok = await ForwardTool.source_forward(
            ws_server=ws_server,
            bot_id=bot_id,
            count=count,
            source_group_id=sgid,
            source_user_id=suid,
            forward_group_id=int(reply_group_id) if reply_group_id else None,
            forward_user_id=int(reply_user_id) if reply_user_id else None,
            batch_size=batch_size,
        )
        if not ok:
            raise RuntimeError("抽查失败: 获取消息历史或转发失败")
