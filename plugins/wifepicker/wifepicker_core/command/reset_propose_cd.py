"""重置求婚冷却 — 移植自 astrbot-plugin-wifepicker src/command/reset_propose_cd.py。"""

from loguru import logger


async def cmd_reset_propose_cd(plugin, bot_id: str, event, rest: str):
    group_id = str(getattr(event, "group_id", ""))
    if not group_id:
        return "求婚冷却时间只能在群聊中重置哦~"

    group_records = plugin.store.marriage_actions.get(group_id)
    if not isinstance(group_records, dict) or not group_records:
        return "💡 本群目前没有人在求婚冷却期内。"

    reset_count = 0
    for user_id, record in list(group_records.items()):
        if isinstance(record, dict) and record.get("action") == "propose":
            group_records.pop(user_id, None)
            reset_count += 1

    if not group_records:
        plugin.store.marriage_actions.pop(group_id, None)

    if reset_count == 0:
        return "💡 本群目前没有人在求婚冷却期内。"

    await plugin.store.flush(force=True)
    logger.info(f"[Wife] reset propose cooldown for group {group_id}, count={reset_count}")
    return f"✅ 本群求婚冷却时间已重置！已清除 {reset_count} 条求婚冷却记录。"
