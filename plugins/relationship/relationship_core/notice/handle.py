"""通知处理 — 移植自 astrbot_plugin_relationship (core/notice/handle.py)。

根据决策结果: 发管理员通知/操作者回复、自动退群、拉黑、抽查新群。
(欢迎消息已拆分为独立插件 plugins/welcome)
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from relationship_core.config import PluginConfig
from relationship_core.forward import ForwardTool
from relationship_core.notice.decision import NoticeDecision
from relationship_core.notice.model import NoticeMessage


class NoticeHandle:
    def __init__(self, config: PluginConfig):
        self.cfg = config

    async def handle(self, bot_id: str, event: Any, raw: dict) -> None:
        raw_dict = raw if isinstance(raw, dict) else {}
        notice = NoticeMessage.from_raw(raw_dict)

        if not notice.is_self_notice():
            return

        decision = NoticeDecision(self.cfg.ws_server, bot_id, notice, self.cfg)
        result = await decision.decide()

        # 操作者提示(发到当前会话)
        if result.operator_reply:
            await self._send_to_chat(bot_id, event, result.operator_reply)

        # 管理者提示(审批群/审批员)
        if result.admin_reply:
            await self._send_admin(bot_id, result.admin_reply)

        # 查群(被拉入新群时自动抽查聊天记录)
        if (
            self.cfg.check.check_new_group
            and result.check_group
            and (self.cfg.manage_group or self.cfg.admin_id)
        ):
            if self.cfg.check.delay > 0:
                await asyncio.sleep(self.cfg.check.delay)
            await ForwardTool.source_forward(
                ws_server=self.cfg.ws_server,
                bot_id=bot_id,
                count=self.cfg.check.count,
                source_group_id=int(notice.group_id),
                forward_group_id=int(self.cfg.manage_group) if self.cfg.manage_group else None,
                forward_user_id=int(self.cfg.admin_id) if self.cfg.admin_id else None,
                batch_size=self.cfg.check.batch_size,
            )

        # 拉黑群聊/用户
        if result.black_group:
            self.cfg.add_black_group(notice.group_id)
        if result.black_user:
            self.cfg.add_block_user(notice.user_id)

        # 退群
        if result.leave_group:
            await asyncio.sleep(5)
            try:
                gid = int(notice.group_id)
            except (TypeError, ValueError):
                gid = 0
            if gid:
                await self.cfg.ws_server.send_to_bot(
                    bot_id, "set_group_leave", {"group_id": gid},
                )

    async def _send_to_chat(self, bot_id: str, event: Any, text: str) -> None:
        """把提示发到事件所在会话(群聊/私聊)。"""
        if self.cfg.ws_server is None:
            return
        try:
            group_id = getattr(event, "group_id", 0)
            if group_id:
                await self.cfg.ws_server.send_group_msg(bot_id, group_id, text)
            else:
                await self.cfg.ws_server.send_private_msg(
                    bot_id, getattr(event, "user_id", 0), text
                )
        except Exception as e:
            logger.warning(f"发送通知失败: {e}")

    async def _send_admin(self, bot_id: str, text: str) -> None:
        if self.cfg.ws_server is None:
            return
        try:
            if self.cfg.manage_group:
                await self.cfg.ws_server.send_group_msg(
                    bot_id, int(self.cfg.manage_group), text
                )
            elif self.cfg.manage_users:
                for user_id in self.cfg.manage_users:
                    try:
                        await self.cfg.ws_server.send_private_msg(
                            bot_id, int(user_id), text
                        )
                    except Exception as e:
                        logger.warning(f"向审批员 {user_id} 发送消息失败: {e}")
            elif self.cfg.admin_id:
                await self.cfg.ws_server.send_private_msg(
                    bot_id, int(self.cfg.admin_id), text
                )
        except Exception as e:
            logger.warning(f"通知发送失败: {e}")
