"""普通命令处理 — 移植自 astrbot_plugin_relationship (core/normal.py)。

群列表/好友列表/退群/删好友/审批员管理/抽查。
抽查的消息取自本地归档(data/history), 其余动作走 send_to_bot。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from relationship_core.config import PluginConfig
from relationship_core.utils import api_call, get_ats, get_nickname, parse_multi_input


class NormalHandle:
    def __init__(self, config: PluginConfig):
        self.cfg = config

    async def get_group_list(self, bot_id: str) -> str:
        """查看 bot 加入的所有群聊。"""
        group_list = await api_call(self.cfg.ws_server, bot_id, "get_group_list")
        if not isinstance(group_list, list) or not group_list:
            return "❌ 获取群列表失败(或还没有加任何群)"

        info = "\n\n".join(
            f"{i + 1}. {g.get('group_id')}: {g.get('group_name')}"
            for i, g in enumerate(group_list)
        )
        return f"【群列表】共加入 {len(group_list)} 个群：\n\n{info}"

    async def get_friend_list(self, bot_id: str) -> str:
        """查看 bot 的所有好友。"""
        friend_list = await api_call(self.cfg.ws_server, bot_id, "get_friend_list")
        if not isinstance(friend_list, list) or not friend_list:
            return "❌ 获取好友列表失败(或还没有好友)"

        info = "\n".join(
            f"{i + 1}. {f.get('user_id')}: {f.get('nickname')}"
            for i, f in enumerate(friend_list)
        )
        return f"【好友列表】共 {len(friend_list)} 位好友：\n{info}"

    async def set_group_leave(self, bot_id: str, raw: str) -> str:
        """退群 <序号|群号|区间> [可批量]"""
        group_list = await api_call(self.cfg.ws_server, bot_id, "get_group_list")
        if not isinstance(group_list, list):
            return "❌ 获取群列表失败"
        if not group_list:
            return "我还没加任何群"

        indexes, ids = parse_multi_input(raw, total=len(group_list))
        if not indexes and not ids:
            return "请输入群序号或群号，可空格分隔"

        group_map = {str(g.get("group_id")): g for g in group_list}
        msgs = []

        for idx in sorted(indexes):
            g = group_list[idx]
            await api_call(self.cfg.ws_server, bot_id, "set_group_leave",
                           {"group_id": int(g.get("group_id"))})
            msgs.append(f"已退出群聊：{g.get('group_name')}({g.get('group_id')})")

        for gid in ids:
            g = group_map.get(gid)
            if not g:
                msgs.append(f"不存在群聊：{gid}")
                continue
            await api_call(self.cfg.ws_server, bot_id, "set_group_leave",
                           {"group_id": int(gid)})
            msgs.append(f"已退出群聊：{g.get('group_name')}({gid})")

        return "\n".join(msgs) if msgs else "未执行任何操作"

    async def delete_friend(self, bot_id: str, event: Any, raw: str) -> str:
        """删好友 <@昵称|QQ|序号|区间> [可批量]"""
        friend_list = await api_call(self.cfg.ws_server, bot_id, "get_friend_list")
        if not isinstance(friend_list, list):
            return "❌ 获取好友列表失败"
        if not friend_list:
            return "我还没有好友"

        user_ids: set[str] = set(get_ats(event))
        indexes, ids = parse_multi_input(raw, total=len(friend_list))
        for idx in indexes:
            user_ids.add(str(friend_list[idx].get("user_id")))
        user_ids |= ids

        if not user_ids:
            return "请 @好友、输入 QQ 号或好友序号"

        friend_map = {str(f.get("user_id")): f for f in friend_list}
        msgs = []
        for uid in sorted(user_ids):
            f = friend_map.get(uid)
            if not f:
                msgs.append(f"不存在好友：{uid}")
                continue
            await api_call(self.cfg.ws_server, bot_id, "delete_friend", {"user_id": int(uid)})
            msgs.append(f"已删除好友：{f.get('nickname')}({uid})")

        return "\n".join(msgs) if msgs else "未执行任何操作"

    async def append_manage_user(self, bot_id: str, event: Any) -> str:
        """添加审批员(全局管理员已是审批员, 这里加额外审批员)。"""
        at_ids = get_ats(event)
        if not at_ids:
            return "需@要添加的审批员"
        msgs = []
        for at_id in at_ids:
            nickname = await get_nickname(
                self.cfg.ws_server, bot_id,
                group_id=getattr(event, "group_id", 0), user_id=at_id,
            )
            if self.cfg.is_manage_user(at_id):
                msgs.append(f"{nickname}已在审批员列表中")
                continue
            await self.cfg.add_manage_user(at_id)
            msgs.append(f"已添加审批员: {nickname}")
        return "\n".join(msgs)

    async def remove_manage_user(self, bot_id: str, event: Any) -> str:
        """移除审批员。"""
        at_ids = get_ats(event)
        if not at_ids:
            return "需@要移除的审批员"
        msgs = []
        for at_id in at_ids:
            nickname = await get_nickname(
                self.cfg.ws_server, bot_id,
                group_id=getattr(event, "group_id", 0), user_id=at_id,
            )
            if not self.cfg.is_manage_user(at_id):
                msgs.append(f"{nickname}不在审批员列表中")
                continue
            await self.cfg.remove_manage_user(at_id)
            msgs.append(f"已移除审批员: {nickname}")
        return "\n".join(msgs)

    async def check_messages(self, bot_id: str, event: Any, raw: str) -> str:
        """抽查 <群号|@群友|@QQ> <数量>, 转发最近聊天记录到当前会话。

        消息取自本地归档 data/history(不调用历史查询 API); 无记录时给出提示。
        """
        from relationship_core.forward import ForwardTool

        parts = (raw or "").split()
        count = self.cfg.check.count
        target_arg = ""
        for p in parts:
            if p.isdigit() and count == self.cfg.check.count and target_arg == "":
                # 第一个数字可能是群号/QQ, 第二个是数量
                target_arg = p
            elif p.isdigit():
                count = int(p)
        # @ 优先
        at_ids = get_ats(event, noself=True)

        try:
            group_id = getattr(event, "group_id", 0)
            await ForwardTool.check_messages(
                ws_server=self.cfg.ws_server,
                bot_id=bot_id,
                at_ids=at_ids,
                target_arg=target_arg,
                count=count,
                reply_group_id=group_id,
                reply_user_id=getattr(event, "user_id", 0),
                batch_size=self.cfg.check.batch_size,
                data_dir=self.cfg.data_dir,
            )
            return None  # 转发已发送, 无需文本回复
        except Exception as e:
            logger.error(f"抽查失败: {e}")
            return f"❌ 抽查失败: {e}"
