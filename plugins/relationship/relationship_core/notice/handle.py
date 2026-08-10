"""通知处理 — 移植自 astrbot_plugin_relationship (core/notice/handle.py)。

根据决策结果: 发管理员通知/操作者回复、自动退群、拉黑、抽查新群。
新增: 新好友/新入群欢迎消息(可配置开关与模板, 随机延迟 3~5s 发送)。
"""

from __future__ import annotations

import asyncio
import random
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

        # 新好友欢迎(friend_add 的 user_id 是新好友 QQ, 不走 is_self_notice)
        if notice.post_type == "notice" and notice.notice_type == "friend_add":
            await self._send_welcome(
                bot_id, target_type="friend", target_id=notice.user_id,
            )
            return

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

        # 新入群欢迎(仅不退群的入群; 黑名单/小群/互斥群等自动退群的场景不发)
        if notice.notice_type == "group_increase" and not result.leave_group:
            await self._send_welcome(
                bot_id, target_type="group", target_id=notice.group_id,
            )

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

    async def _send_welcome(
        self, bot_id: str, *, target_type: str, target_id: str,
    ) -> None:
        """发送欢迎消息(开关关闭/模板为空则跳过)。

        target_type: "friend"=私聊新好友, "group"=群聊新入群。
        发送延迟随机 3~5s; 模板占位符【此处替换为 bot 的昵称】替换为 bot 昵称
        (取不到昵称回退 bot_id)。
        """
        if self.cfg.ws_server is None:
            return
        if target_type == "friend":
            enabled_key, msg_key = "welcome_friend_enabled", "welcome_friend_msg"
        else:
            enabled_key, msg_key = "welcome_group_enabled", "welcome_group_msg"
        data = getattr(self.cfg, "_data", {}) or {}
        if not data.get(enabled_key, True):
            return
        template = str(data.get(msg_key, "") or "").strip()
        if not template:
            return
        nickname = await self._get_bot_nickname(bot_id)
        text = template.replace("【此处替换为 bot 的昵称】", nickname)
        # 随机延迟 3~5s, 模拟真人
        await asyncio.sleep(random.uniform(3.0, 5.0))
        try:
            if target_type == "friend":
                await self.cfg.ws_server.send_private_msg(bot_id, int(target_id), text)
            else:
                await self.cfg.ws_server.send_group_msg(bot_id, int(target_id), text)
            logger.info(
                f"欢迎消息已发送: {target_type}={target_id} (bot {bot_id}, {len(text)} 字)"
            )
        except Exception as e:
            logger.warning(f"欢迎消息发送失败({target_type}={target_id}): {e}")

    async def _get_bot_nickname(self, bot_id: str) -> str:
        """取 bot 配置的昵称; 无 bot_manager 引用时回退 bot_id。"""
        ws = self.cfg.ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        if bm is None:
            return bot_id
        try:
            instance = bm.get(bot_id)
            if instance is not None and getattr(instance, "nickname", ""):
                return instance.nickname
        except Exception:
            pass
        return bot_id

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
