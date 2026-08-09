"""请求决策 — 移植自 astrbot_plugin_relationship (core/request/decision.py)。

自动规则(黑名单/auto_agree/reject) + 指令审批(同意/拒绝/拉黑)。
去掉 afdian 校验(无外部服务依赖)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.config import PluginConfig
from core.request.model import BaseRequest, FriendRequest, GroupRequest
from core.utils import api_call


@dataclass
class RequestResult:
    admin_reply: str = ""
    user_reply: str = ""
    event_reply: str = ""
    approve: Optional[bool] = None
    block_group: Optional[bool] = None
    block_user: Optional[bool] = None


class RequestDecision:
    """请求决策层。"""

    def __init__(self, ws_server, bot_id: str, request: BaseRequest, config: PluginConfig):
        self.ws_server = ws_server
        self.bot_id = bot_id
        self.req = request
        self.cfg = config

    async def decide(
        self,
        approve: Optional[bool] = None,
        extra: str = "",
        block: bool = False,
    ) -> RequestResult:
        result = RequestResult(approve=approve)
        result.admin_reply = self.req.to_display_text()

        # 自动规则(最高优先级, 仅事件触发时)
        if approve is None:
            if self._auto_decide(result):
                return result

        # 事件触发(无 approve) → 未自动处理则转人工审批
        if approve is None:
            if isinstance(self.req, FriendRequest):
                self._decide_friend(result)
            elif isinstance(self.req, GroupRequest):
                self._decide_group(self.req, result)

        # 指令决策(审批员引用审批消息回复)
        else:
            if isinstance(self.req, FriendRequest):
                await self._decide_friend_cmd(self.req, approve, result, extra, block)
            elif isinstance(self.req, GroupRequest):
                await self._decide_group_cmd(self.req, approve, result, extra, block)

        return result

    # ── 自动决策(配置驱动) ───────────────────────────────────

    def _auto_decide(self, result: RequestResult) -> bool:
        cfg = self.cfg.request

        if isinstance(self.req, FriendRequest):
            uid = str(self.req.user_id)
            if cfg.auto_reject_friend:
                result.approve = False
                result.user_reply = "已自动拒绝好友请求"
                result.admin_reply += "\n自动处理：已自动拒绝"
                return True
            if uid in cfg.user_blacklist:
                result.approve = False
                result.user_reply = "你已被加入黑名单，无法添加好友"
                result.block_user = True
                result.admin_reply += "\n自动处理：该用户在黑名单中"
                return True
            if cfg.auto_agree_friend:
                result.approve = True
                result.user_reply = "已自动同意好友请求"
                result.admin_reply += "\n自动处理：已自动同意"
                return True

        if isinstance(self.req, GroupRequest):
            gid = str(self.req.group_id)
            if cfg.auto_reject_group:
                result.approve = False
                result.user_reply = "已自动拒绝群邀请"
                result.admin_reply += "\n自动处理：已自动拒绝"
                return True
            if gid in cfg.group_blacklist:
                result.approve = False
                result.user_reply = "该群已被列入黑名单，自动拒绝"
                result.block_group = True
                result.admin_reply += "\n自动处理：该群在黑名单中"
                return True
            if cfg.auto_agree_group:
                result.approve = True
                result.user_reply = "已自动同意群邀请"
                result.admin_reply += "\n自动处理：已自动同意"
                return True

        return False

    # ── 未自动处理 → 转人工审批 ───────────────────────────────

    def _decide_friend(self, result: RequestResult):
        result.user_reply = "好友申请已收到，正在审核中，请耐心等待"

    def _decide_group(self, req: GroupRequest, result: RequestResult):
        if self.cfg.manage_group:
            result.user_reply = (
                f"群邀请已收到，需要在审核群 {self.cfg.manage_group} 审批后才能加入"
            )
        else:
            result.user_reply = "群邀请已收到，需要审核通过后才能加入"

        if self.cfg.is_black_group(req.group_id):
            result.admin_reply += "\n警告: 该群为黑名单群聊，请谨慎通过"
            result.user_reply += "\n该群已被列入黑名单，可能不会通过审核"

    # ── 指令处理(审批员) ─────────────────────────────────────

    async def _decide_friend_cmd(
        self, req: FriendRequest, approve: bool, result: RequestResult,
        extra: str = "", block: bool = False,
    ):
        friend_list = await api_call(self.ws_server, self.bot_id, "get_friend_list")
        uids = {str(f.get("user_id")) for f in friend_list} if isinstance(friend_list, list) else set()

        if req.user_id in uids:
            result.event_reply = f"【{req.nickname}】已经是我的好友啦"
            result.approve = None
            return

        if approve:
            result.approve = True
            result.event_reply = f"已同意好友：{req.nickname}"
            if extra:
                result.event_reply += f"\n备注：{extra}"
        else:
            result.approve = False
            result.event_reply = f"已拒绝好友：{req.nickname}"
            if block:
                result.block_user = True
                result.event_reply = f"已拉黑好友申请人：{req.nickname}"
            if extra:
                result.event_reply += f"\n理由：{extra}"

    async def _decide_group_cmd(
        self, req: GroupRequest, approve: bool, result: RequestResult,
        extra: str = "", block: bool = False,
    ):
        group_list = await api_call(self.ws_server, self.bot_id, "get_group_list")
        gids = {str(g.get("group_id")) for g in group_list} if isinstance(group_list, list) else set()

        if str(req.group_id) in gids:
            result.event_reply = f"我已经在【{req.group_name}】里啦"
            result.approve = None
            return

        if approve:
            result.approve = True
            result.block_group = False
            result.event_reply = f"已同意群邀请：{req.group_name}"
        else:
            result.approve = False
            result.event_reply = f"已拒绝群邀请：{req.group_name}"
            if block:
                result.block_group = True
                result.event_reply = f"已拉黑群聊：{req.group_name}"
            if extra:
                result.event_reply += f"\n理由：{extra}"
