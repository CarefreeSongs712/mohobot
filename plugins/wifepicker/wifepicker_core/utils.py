"""事件与 OneBot API 工具 — 适配 mohobot 事件模型。

原插件依赖 AstrBot 的 AstrMessageEvent, 这里改为直接操作 mohobot 的
GroupMessageEvent / PrivateMessageEvent(消息段数组)。
"""

from __future__ import annotations

from typing import Any

from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent
from mohobot.utils.cq_code import extract_plain_text


def is_private_chat(event) -> bool:
    return isinstance(event, PrivateMessageEvent)


def get_group_id(event) -> str | None:
    if isinstance(event, GroupMessageEvent):
        return str(event.group_id)
    return None


def get_sender_id(event) -> str:
    return str(event.user_id)


def get_self_id(event) -> str:
    return str(getattr(event, "self_id", 0) or 0)


def get_text(event) -> str:
    return extract_plain_text(event.message) or ""


def extract_at_ids(event) -> list[str]:
    """从消息段中解析 @ 目标 QQ(不含 bot 自身)。"""
    self_id = get_self_id(event)
    result: list[str] = []
    for seg in event.message or []:
        if isinstance(seg, dict) and seg.get("type") == "at":
            qq = str(seg.get("data", {}).get("qq", "") or "")
            if qq and qq != self_id:
                result.append(qq)
    return result


def extract_target_id(event) -> str | None:
    """强娶/求婚目标: @ 优先, 无 @ 返回 None。"""
    ids = extract_at_ids(event)
    if not ids:
        return None
    return ids[0]


def is_allowed_group(group_id: str | None, config: dict) -> bool:
    """群聊白/黑名单判定(配置 whitelist_groups / blacklist_groups)。"""
    if not group_id:
        return False
    blacklist = {str(g) for g in (config.get("blacklist_groups") or [])}
    if group_id in blacklist:
        return False
    whitelist = {str(g) for g in (config.get("whitelist_groups") or [])}
    if whitelist and group_id not in whitelist:
        return False
    return True


async def api_call(ws_server, bot_id: str, action: str, params: dict) -> Any:
    """通用 OneBot API 调用(宽松解析 retcode/status)。

    超时 5s(默认 10s 太长, 串行兜底链会拖慢回复)。
    """
    resp = await ws_server.send_to_bot(bot_id, action, params, wait_response=True, timeout=5.0)
    if not resp:
        return None
    data = resp.get("data")
    return data


async def get_group_member_list(ws_server, bot_id: str, group_id: str) -> list[dict]:
    """获取群成员列表(失败返回空列表)。"""
    data = await api_call(
        ws_server, bot_id, "get_group_member_list", {"group_id": int(group_id)}
    )
    if isinstance(data, list):
        return data
    return []


async def get_group_info(ws_server, bot_id: str, group_id: str) -> dict:
    """获取群信息(失败返回空 dict)。"""
    data = await api_call(
        ws_server, bot_id, "get_group_info", {"group_id": int(group_id)}
    )
    return data if isinstance(data, dict) else {}


def resolve_member_name(members: list[dict], user_id: str, fallback: str) -> str:
    """群成员列表 → 名字(群名片优先, 再昵称; 空白名片视为无)。"""
    for m in members:
        if str(m.get("user_id")) == str(user_id):
            card = str(m.get("card") or "").strip()
            nickname = str(m.get("nickname") or "").strip()
            return card or nickname or fallback
    return fallback


async def resolve_name(ws_server, bot_id: str, group_id: str, user_id: str, fallback: str) -> str:
    """名字解析: 群名片 → QQ 昵称 → 数字兜底(走框架 get_nickname, 带缓存)。

    群成员列表获取失败时用于兜底, 保证不显示纯数字。
    """
    try:
        return await ws_server.get_nickname(bot_id, user_id, group_id)
    except Exception:
        return fallback


def build_user_map(members: list[dict]) -> dict[str, str]:
    """群成员列表 → {qq: 名字}(群名片优先, 空白名片回退昵称)。"""
    result: dict[str, str] = {}
    for m in members:
        card = str(m.get("card") or "").strip()
        nickname = str(m.get("nickname") or "").strip()
        uid = str(m.get("user_id"))
        result[uid] = card or nickname or uid
    return result


def at_segment(qq: str) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def text_segment(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def image_url_segment(url: str) -> dict:
    return {"type": "image", "data": {"file": url}}
