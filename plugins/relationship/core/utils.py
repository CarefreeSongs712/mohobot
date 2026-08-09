"""工具 — 移植自 astrbot_plugin_relationship (core/utils.py), 适配 mohobot。

OneBot API 通过 ws_server.send_to_bot(bot_id, action, params, wait_response=True) 调用。
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger


def convert_duration_advanced(duration: int) -> str:
    """将秒数转换为友好的时长字符串, 如"1天2小时3分钟4秒"。"""
    if duration < 0:
        return "未知时长"
    if duration == 0:
        return "0秒"

    days, rem = divmod(duration, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    units = [
        (days, "天"), (hours, "小时"), (minutes, "分钟"), (seconds, "秒"),
    ]
    non_zero = [(value, label) for value, label in units if value > 0]
    if len(non_zero) == 1:
        return f"{non_zero[0][0]}{non_zero[0][1]}"
    return "".join(f"{value}{label}" for value, label in non_zero)


async def api_call(ws_server, bot_id: str, action: str, params: dict | None = None,
                   timeout: float = 10.0) -> Any:
    """调用 OneBot API 并返回响应 data(任意类型; 失败/超时返回 None)。"""
    if ws_server is None:
        return None
    try:
        resp = await ws_server.send_to_bot(
            bot_id, action, params or {}, wait_response=True, timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"API {action} 调用失败: {e}")
        return None
    if not isinstance(resp, dict):
        return None
    if resp.get("status") != "ok" or resp.get("retcode") != 0:
        logger.warning(
            f"API {action} 返回错误: {resp.get('wording') or resp.get('message')}"
        )
        return None
    return resp.get("data")


async def get_nickname(ws_server, bot_id: str, group_id: str | int, user_id: str | int) -> str:
    """获取指定群友的群昵称或 Q 名, 群接口失败自动降级到陌生人资料。"""
    user_id = int(user_id)
    info: dict = {}

    if str(group_id).isdigit():
        data = await api_call(
            ws_server, bot_id, "get_group_member_info",
            {"group_id": int(group_id), "user_id": user_id},
        )
        if data:
            info = data

    if not info:
        data = await api_call(ws_server, bot_id, "get_stranger_info", {"user_id": user_id})
        if data:
            info = data

    return info.get("card") or info.get("nickname") or info.get("nick") or str(user_id)


def get_ats(event: Any, noself: bool = False) -> list[str]:
    """获取消息中 @ 的用户 id 列表(mohobot message 段格式)。"""
    ats: set[str] = set()
    message = getattr(event, "message", None)
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = str(seg.get("data", {}).get("qq", ""))
                if qq and qq != "all":
                    ats.add(qq)
    if noself:
        ats.discard(str(getattr(event, "self_id", "")))
    return list(ats)


def get_reply_id(event: Any) -> str | None:
    """获取引用(reply)段的消息 id。"""
    message = getattr(event, "message", None)
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                return str(seg.get("data", {}).get("id", ""))
    return None


async def get_reply_text(ws_server, bot_id: str, event: Any) -> str:
    """获取被引用消息的纯文本(通过 get_msg 接口)。"""
    reply_id = get_reply_id(event)
    if not reply_id:
        return ""
    data = await api_call(ws_server, bot_id, "get_msg", {"message_id": int(reply_id)})
    if not data:
        return ""
    message = data.get("message") or []
    text = ""
    if isinstance(message, str):
        return message
    for seg in message:
        if isinstance(seg, dict) and seg.get("type") == "text":
            text += seg.get("data", {}).get("text", "")
    return text


def get_message_text(event: Any) -> str:
    """提取事件消息的纯文本。"""
    message = getattr(event, "message", None)
    if isinstance(message, str):
        return message.strip()
    text = ""
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "text":
                text += seg.get("data", {}).get("text", "")
    return text.strip()


def parse_multi_input(raw: str, total: int) -> tuple[set[int], set[str]]:
    """
    解析文本参数, 支持:
    - 空格分隔 / 序号 / 区间(1~5 / 1-5) / 直接 ID(QQ/群号)
    返回: (indexes: 0-based 索引集合, ids: 明确 ID 集合)
    """
    indexes: set[int] = set()
    ids: set[str] = set()

    for token in (raw or "").strip().split():
        token = token.strip()
        if not token:
            continue

        m = re.fullmatch(r"(\d+)\s*[~-]\s*(\d+)", token)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                if 1 <= i <= total:
                    indexes.add(i - 1)
            continue

        if token.isdigit():
            num = int(token)
            if 1 <= num <= total:
                indexes.add(num - 1)
            else:
                ids.add(token)

    return indexes, ids
