"""求婚交互 — 移植自 astrbot-plugin-wifepicker src/command/propose.py。

发起求婚后 30 秒内对方回复"同意/拒绝"; 拒绝后可回复"是/否"确认强娶。
交互回复在观察钩子(on_message_observed)中捕获所有群消息, 无需 @bot。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from loguru import logger

from ..core import (
    get_force_marry_cooldown_status,
    get_group_records,
    get_propose_cooldown_status,
    set_propose_cooldown,
    upsert_user_wife_record,
)
from ..utils import (
    at_segment,
    extract_target_id,
    get_group_id,
    get_sender_id,
    get_self_id,
    get_text,
    is_allowed_group,
    text_segment,
)

PROPOSE_RESPONSE_SECONDS = 30
FORCE_CONFIRM_SECONDS = 30

# 同意/拒绝/确认 关键词
AGREE_WORDS = {"同意求婚", "我同意", "同意"}
REFUSE_WORDS = {"拒绝求婚", "我拒绝", "拒绝", "不同意"}
FORCE_YES_WORDS = {"是", "确认", "强娶", "要"}
FORCE_NO_WORDS = {"否", "不", "不要", "算了", "取消"}


def _format_remaining(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}小时{mins}分"
    if mins > 0:
        return f"{mins}分{secs}秒"
    return f"{secs}秒"


async def _member_name(plugin, bot_id: str, group_id: str, user_id: str) -> str:
    """群成员名字(群名片 → QQ 昵称 → 数字, 走框架 get_nickname 带缓存)。"""
    fallback = f"用户({user_id})"
    try:
        return await plugin._ws_server.get_nickname(bot_id, user_id, group_id)
    except Exception:
        return fallback


async def cmd_propose(plugin, bot_id: str, event, rest: str):
    """发起求婚(/求婚 @某人)。"""
    group_id = get_group_id(event)
    if not group_id:
        return "求婚只能在群聊中进行哦~"
    if not is_allowed_group(group_id, plugin.plugin_config):
        return None

    user_id = get_sender_id(event)
    target_id = extract_target_id(event)
    if not target_id or target_id == "all":
        return "请 @ 一个你想求婚的人。"
    if target_id == user_id:
        return "不能向自己求婚哦！"

    config = plugin.plugin_config
    # 强娶/求婚冷却检查
    if get_force_marry_cooldown_status(plugin.store, group_id, user_id, config):
        return "你还在强娶冷却期内，暂时不能求婚。"
    if get_force_marry_cooldown_status(plugin.store, group_id, target_id, config):
        return "对方还在强娶冷却期内，暂时不能接受求婚。"
    user_cd = get_propose_cooldown_status(plugin.store, group_id, user_id)
    if user_cd:
        return f"你还在求婚冷却期内，请等待 {_format_remaining(user_cd['remaining'])} 后再试。"
    target_cd = get_propose_cooldown_status(plugin.store, group_id, target_id)
    if target_cd:
        return f"对方还在求婚冷却期内，请等待 {_format_remaining(target_cd['remaining'])} 后再试。"

    # 已有待处理求婚
    pending = plugin._propose_requests.get(group_id, {})
    if any(
        isinstance(r, dict) and r.get("proposer_id") == user_id
        and r.get("expire", 0) > time.time()
        for r in pending.values()
    ):
        return "你已经有一个待处理的求婚了，请等待对方回复或 30 秒后再试。"

    now = time.time()
    target_name = await _member_name(plugin, bot_id, group_id, target_id)
    proposer_name = await _member_name(plugin, bot_id, group_id, user_id)

    pending[target_id] = {
        "proposer_id": user_id,
        "proposer_name": proposer_name,
        "target_name": target_name,
        "expire": now + PROPOSE_RESPONSE_SECONDS,
        "bot_id": bot_id,
    }
    plugin._propose_requests[group_id] = pending

    # 30 秒后超时提醒(后台任务, 不阻塞回复)
    asyncio.create_task(_propose_timeout(plugin, bot_id, group_id, target_id, user_id))

    return (
        f"🌹 @{proposer_name} 向【{target_name}】发起了求婚！\n"
        "请在 30 秒内回复“同意”来接受，或回复“拒绝”来拒绝。"
    )


async def _propose_timeout(plugin, bot_id: str, group_id: str, target_id: str, proposer_id: str) -> None:
    """求婚超时提醒(30 秒后请求仍存在则提醒并清除)。"""
    await asyncio.sleep(PROPOSE_RESPONSE_SECONDS)
    try:
        req = plugin._propose_requests.get(group_id, {}).get(target_id)
        if req and req.get("proposer_id") == proposer_id:
            plugin._propose_requests.get(group_id, {}).pop(target_id, None)
            if not plugin._propose_requests.get(group_id):
                plugin._propose_requests.pop(group_id, None)
            if plugin._ws_server is not None:
                await plugin._ws_server.send_group_msg(
                    bot_id, int(group_id),
                    [at_segment(proposer_id),
                     text_segment(" ...很遗憾，求婚超时了，对方似乎没有答应...")],
                )
    except Exception as e:
        logger.warning(f"[propose] 超时提醒失败: {e}")


async def handle_propose_response(plugin, bot_id: str, event) -> str | list | None:
    """处理求婚回复(同意/拒绝)与拒绝后的强娶确认。返回回复内容, 未消费返回 None。"""
    group_id = get_group_id(event)
    if not group_id:
        return None
    user_id = get_sender_id(event)
    msg = get_text(event).strip()
    if not msg:
        return None

    # 拒绝后的强娶确认
    force_req = plugin._force_confirm_requests.get(group_id, {}).get(user_id)
    if isinstance(force_req, dict):
        if force_req.get("expire", 0) <= time.time():
            plugin._force_confirm_requests.get(group_id, {}).pop(user_id, None)
        elif msg in FORCE_YES_WORDS:
            target_id = str(force_req["target_id"])
            plugin._force_confirm_requests.get(group_id, {}).pop(user_id, None)
            return await plugin.cmd_force_marry(bot_id, event, None, target_id_override=target_id)
        elif msg in FORCE_NO_WORDS:
            plugin._force_confirm_requests.get(group_id, {}).pop(user_id, None)
            return "已取消强娶。"

    # 求婚回复(被求婚者 = user_id)
    req = plugin._propose_requests.get(group_id, {}).get(user_id)
    if not isinstance(req, dict):
        return None
    if req.get("expire", 0) <= time.time():
        plugin._propose_requests.get(group_id, {}).pop(user_id, None)
        return None

    config = plugin.plugin_config
    if msg in AGREE_WORDS:
        proposer_id = req["proposer_id"]
        proposer_name = req["proposer_name"]
        target_name = req["target_name"]
        if get_force_marry_cooldown_status(plugin.store, group_id, proposer_id, config) or \
           get_force_marry_cooldown_status(plugin.store, group_id, user_id, config):
            _clear_proposals_by_proposer(plugin, group_id, proposer_id)
            return "求婚已失效：你们中有人进入了强娶冷却期。"

        timestamp = datetime.now().isoformat()
        records = get_group_records(plugin.store, group_id)
        daily_limit = plugin.plugin_config.get("daily_limit", 1)
        upsert_user_wife_record(records, user_id=proposer_id, wife_id=user_id,
                                wife_name=target_name, timestamp=timestamp,
                                daily_limit=daily_limit)
        upsert_user_wife_record(records, user_id=user_id, wife_id=proposer_id,
                                wife_name=proposer_name, timestamp=timestamp,
                                daily_limit=daily_limit)

        now = time.time()
        set_propose_cooldown(plugin.store, group_id, proposer_id,
                             related_user_id=user_id, role="proposer", config=config, now=now)
        set_propose_cooldown(plugin.store, group_id, user_id,
                             related_user_id=proposer_id, role="target", config=config, now=now)

        _clear_proposals_by_proposer(plugin, group_id, proposer_id)
        await plugin.store.flush(force=True)
        return f"🎉 恭喜！{target_name} 接受了 {proposer_name} 的求婚！\n你们已正式结为夫妻！"

    if msg in REFUSE_WORDS:
        proposer_id = req["proposer_id"]
        target_name = req["target_name"]
        plugin._propose_requests.get(group_id, {}).pop(user_id, None)
        if not plugin._propose_requests.get(group_id):
            plugin._propose_requests.pop(group_id, None)

        group_confirm = plugin._force_confirm_requests.setdefault(group_id, {})
        group_confirm[proposer_id] = {
            "target_id": user_id,
            "target_name": target_name,
            "expire": time.time() + FORCE_CONFIRM_SECONDS,
            "bot_id": bot_id,
        }
        return [at_segment(proposer_id),
                text_segment(f" 很遗憾，【{target_name}】拒绝了你的求婚。\n"
                             "是否强娶？请在 30 秒内回复“是”，否则不会进入强娶逻辑。")]
    return None


def _clear_proposals_by_proposer(plugin, group_id: str, proposer_id: str) -> None:
    group = plugin._propose_requests.get(group_id)
    if not isinstance(group, dict):
        return
    for target_id in [t for t, r in group.items()
                      if isinstance(r, dict) and r.get("proposer_id") == proposer_id]:
        group.pop(target_id, None)
    if not group:
        plugin._propose_requests.pop(group_id, None)
