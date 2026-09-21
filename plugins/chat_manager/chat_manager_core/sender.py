"""发送消息 —— 把纯文本发到指定会话。"""

from __future__ import annotations

from loguru import logger

from chat_manager_core.target import Target


async def send_plain_text(
    ws_server, bot_id: str, dest: Target, text: str,
) -> bool:
    """把 text 作为单个文本段发到 dest, 返回是否成功。

    用段数组而不是字符串发送: 字符串形式会被协议端当 CQ 码解析,
    而命令语义是"仅纯文本"(不能让调用者借 [CQ:...] 构造任意消息)。
    走 ws_server 的 send_*_msg 助手, 从而享受出站队列的限速与发送追踪。
    """
    if ws_server is None:
        return False
    message = [{"type": "text", "data": {"text": text}}]
    try:
        if dest.is_group:
            await ws_server.send_group_msg(bot_id, int(dest.chat_id), message)
        else:
            await ws_server.send_private_msg(bot_id, int(dest.chat_id), message)
        return True
    except Exception as e:
        logger.warning(f"发送消息到 {dest.describe()} 失败: {e}")
        return False
