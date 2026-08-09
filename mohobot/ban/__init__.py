"""封禁系统 — 移植自 astrbot_plugin_reneban(去掉 server/云同步)。

mohobot 多 bot 聚合: 名单全局统一(所有 bot 共享), 会话维度为
"chat_type:chat_id"(群=group:群号, 私聊=private:QQ号)。
封禁语义: bot 静默忽略被禁用户的消息(不是 QQ 群管理封禁)。
"""

from mohobot.ban.ban_filter import BanInterceptor
from mohobot.ban.store import BanStore

__all__ = ["BanInterceptor", "BanStore"]
