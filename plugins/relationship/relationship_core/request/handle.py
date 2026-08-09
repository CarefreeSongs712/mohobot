"""请求处理 — 移植自 astrbot_plugin_relationship (core/request/handle.py)。

handle_raw: 事件触发(自动规则 + 转发审批消息到审批群/审批员)。
handle_cmd: 审批命令(同意/拒绝/拉黑, 引用审批消息)。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from relationship_core.config import PluginConfig
from relationship_core.request.decision import RequestDecision
from relationship_core.request.model import BaseRequest, FriendRequest, GroupRequest
from relationship_core.utils import api_call, get_reply_text


class RequestHandle:
    def __init__(self, config: PluginConfig):
        self.cfg = config

    async def handle_raw(self, bot_id: str, event: Any, raw: dict) -> bool:
        """事件触发: 好友申请/群邀请。返回 True=已接管(不再自动同意)。"""
        req = await BaseRequest.from_raw(self.cfg.ws_server, bot_id, raw)
        if not req:
            return False
        await self._handle_req(bot_id, event, req)
        return True

    async def handle_cmd(
        self, bot_id: str, event: Any, *, approve: bool, extra: str = "", block: bool = False,
    ) -> str:
        """审批命令: 引用审批消息 → 同意/拒绝/拉黑。"""
        sender_id = str(getattr(event, "user_id", ""))
        if not self.cfg.is_manage_user(sender_id):
            return "❌ 你没有审批权限"

        text = await get_reply_text(self.cfg.ws_server, bot_id, event)
        req = BaseRequest.from_display_text(text)
        if not req:
            return "❌ 无法解析申请信息，请确保引用的是正确的审批消息"

        result = await RequestDecision(
            self.cfg.ws_server, bot_id, req, self.cfg,
        ).decide(approve=approve, extra=extra, block=block)

        if result.approve is not None:
            await self._do_approve(bot_id, req, result.approve)

        # 黑名单状态同步(必须在 event_reply 返回前执行, 否则 /拉黑 不会生效)
        if isinstance(req, GroupRequest):
            if result.block_group is False:
                self.cfg.remove_black_group(req.group_id)
            elif result.block_group:
                self.cfg.add_black_group(req.group_id)
        if isinstance(req, FriendRequest):
            if result.block_user is False:
                self.cfg.remove_block_user(req.user_id)
            elif result.block_user:
                self.cfg.add_block_user(req.user_id)

        return result.event_reply or "已处理"

    async def _handle_req(self, bot_id: str, event: Any, req: BaseRequest) -> None:
        result = await RequestDecision(
            self.cfg.ws_server, bot_id, req, self.cfg,
        ).decide()

        # 执行自动同意/拒绝
        if result.approve is not None:
            await self._do_approve(bot_id, req, result.approve)

        # 回复申请人(好友申请/群邀请方)
        if result.user_reply:
            await self._send_user_reply(bot_id, req, result.user_reply)

        # 转发审批消息到审批群/审批员(未自动处理时)
        if result.approve is None and result.admin_reply:
            await self._send_admin(bot_id, result.admin_reply)

        # 黑名单状态同步
        if isinstance(req, GroupRequest):
            if result.block_group is False:
                self.cfg.remove_black_group(req.group_id)
            elif result.block_group:
                self.cfg.add_black_group(req.group_id)
        if isinstance(req, FriendRequest):
            if result.block_user is False:
                self.cfg.remove_block_user(req.user_id)
            elif result.block_user:
                self.cfg.add_block_user(req.user_id)

    async def _do_approve(self, bot_id: str, req: BaseRequest, approve: bool) -> None:
        try:
            if isinstance(req, FriendRequest):
                await api_call(
                    self.cfg.ws_server, bot_id, "set_friend_add_request",
                    {"flag": req.flag, "approve": approve},
                )
            elif isinstance(req, GroupRequest):
                await api_call(
                    self.cfg.ws_server, bot_id, "set_group_add_request",
                    {"flag": req.flag, "sub_type": "invite", "approve": approve},
                )
        except Exception as e:
            logger.error(f"审批失败: {e}")

    async def _send_user_reply(self, bot_id: str, req: BaseRequest, text: str) -> None:
        """给申请人发私聊(失败则放弃, 避免暴露错误)。"""
        if self.cfg.ws_server is None:
            return
        try:
            if isinstance(req, FriendRequest):
                await self.cfg.ws_server.send_private_msg(bot_id, int(req.user_id), text)
            elif isinstance(req, GroupRequest):
                await self.cfg.ws_server.send_private_msg(bot_id, int(req.inviter_id), text)
        except Exception as e:
            logger.warning(f"给申请人发消息失败: {e}")

    async def _send_admin(self, bot_id: str, text: str) -> None:
        """转发审批消息: 审批群优先, 否则私发各审批员。"""
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
            logger.warning(f"审批消息发送失败: {e}")
