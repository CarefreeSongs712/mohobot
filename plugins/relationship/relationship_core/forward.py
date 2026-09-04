"""转发工具 — 移植自 astrbot_plugin_relationship (core/forward.py)。

抽查聊天记录: 从本地归档 data/history/{bot_id}/ 读取最近消息
(不再调用 get_group_msg_history / get_friend_msg_history API) →
构造转发节点 → send_group_forward_msg/send_private_forward_msg
分批发到当前会话。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from loguru import logger

from relationship_core.utils import api_call, parse_multi_input


class ForwardTool:
    @staticmethod
    def _make_nodes(messages: list[dict]) -> list[dict[str, Any]]:
        """消息 → 转发节点(OneBot node 段)。"""
        nodes = []
        for message in messages:
            sender = message.get("sender") or {}
            name = (
                sender.get("card")
                or sender.get("nickname")
                or str(sender.get("user_id") or "未知")
            )
            nodes.append({
                "type": "node",
                "data": {
                    "name": name,
                    "uin": sender.get("user_id") or 0,
                    "content": message.get("message") or "",
                },
            })
        return nodes

    @staticmethod
    def _history_dir(data_dir: str, bot_id: str) -> Path:
        """本地消息归档目录 data/history/{bot_id}/。"""
        return Path(data_dir) / "history" / bot_id

    @staticmethod
    async def _get_msg_history(
        data_dir: str, bot_id: str, count: int,
        group_id: int | None = None, user_id: int | None = None,
    ) -> list[dict]:
        """读取本地归档的最近 count 条消息事件(群聊/私聊), 无记录返回 []。

        数据来自框架逐条写入的 data/history/{bot_id}/group|private/{id}.jsonl,
        不调用 get_group_msg_history / get_friend_msg_history 等 API。
        """
        base = ForwardTool._history_dir(data_dir, bot_id)
        if group_id:
            path = base / "group" / f"{int(group_id)}.jsonl"
        elif user_id:
            path = base / "private" / f"{int(user_id)}.jsonl"
        else:
            return []
        if not path.is_file():
            logger.debug(f"抽查: 本地无聊天记录 {path}")
            return []
        from mohobot.file_store import jsonl_read_tail

        lines = await jsonl_read_tail(path, n=max(count, 1))
        messages = [
            line for line in lines
            if isinstance(line, dict)
            and line.get("post_type") == "message"
            and line.get("message") not in (None, "")
        ]
        return messages[-count:]

    @staticmethod
    async def _local_group_list(data_dir: str, bot_id: str) -> list[dict]:
        """本地有聊天记录的群列表(扫描归档目录, 作为抽查候选)。"""
        group_dir = ForwardTool._history_dir(data_dir, bot_id) / "group"
        result = []
        if group_dir.is_dir():
            for p in sorted(group_dir.glob("*.jsonl")):
                result.append({"group_id": p.stem})
        return result

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
        ws_server, bot_id: str, *, count: int, data_dir: str,
        source_group_id: int | None = None, source_user_id: int | None = None,
        forward_group_id: int | None = None, forward_user_id: int | None = None,
        batch_size: int = 0,
    ) -> bool:
        """把源会话最近 count 条本地消息转发到目标会话。"""
        try:
            messages = await ForwardTool._get_msg_history(
                data_dir, bot_id,
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
        reply_group_id: int, reply_user_id: int, data_dir: str, batch_size: int = 0,
    ) -> None:
        """抽查指定群/用户最近 count 条消息(取自本地归档), 转发到当前会话。"""
        sgid: int | None = None
        suid: int | None = None

        # 1. @ 用户优先(私聊抽查)
        if at_ids:
            suid = int(at_ids[0])

        # 2. 文本解析(序号/群号) — 候选为本地有聊天记录的群
        if not suid and target_arg:
            group_list = await ForwardTool._local_group_list(data_dir, bot_id)
            indexes, ids = parse_multi_input(target_arg, total=len(group_list))
            if indexes:
                sgid = int(group_list[min(indexes)].get("group_id"))
            elif ids:
                value = next(iter(ids))
                if value.isdigit():
                    sgid = int(value)

        # 3. 兜底: 随机本地有记录的群
        if not sgid and not suid:
            group_list = await ForwardTool._local_group_list(data_dir, bot_id)
            if not group_list:
                raise RuntimeError("本地没有可抽查的聊天记录(抽查依赖 data/history 归档)")
            sgid = int(random.choice(group_list).get("group_id"))

        logger.debug(
            f"正在抽查{f'群({sgid})' if sgid else f'用户({suid})'}"
            f"的最近 {count} 条本地聊天记录..."
        )

        ok = await ForwardTool.source_forward(
            ws_server=ws_server,
            bot_id=bot_id,
            count=count,
            data_dir=data_dir,
            source_group_id=sgid,
            source_user_id=suid,
            forward_group_id=int(reply_group_id) if reply_group_id else None,
            forward_user_id=int(reply_user_id) if reply_user_id else None,
            batch_size=batch_size,
        )
        if not ok:
            target_desc = f"群 {sgid}" if sgid else f"用户 {suid}"
            raise RuntimeError(
                f"抽查失败: 本地没有 {target_desc} 的聊天记录, 或转发失败"
            )
