"""我的老婆 — 移植自 astrbot-plugin-wifepicker src/command/my_wife.py。"""

from datetime import datetime

from ..core import get_daily_limit
from ..utils import is_allowed_group


async def cmd_show_history(plugin, bot_id: str, event, rest: str):
    group_id = str(getattr(event, "group_id", ""))
    if not is_allowed_group(group_id, plugin.plugin_config):
        return None

    user_id = str(event.user_id)
    records = plugin.store.records
    if records.get("date") != datetime.now().strftime("%Y-%m-%d"):
        return "你今天还没有抽过老婆哦~"

    group_recs = records.get("groups", {}).get(group_id, {}).get("records", [])
    user_recs = [r for r in group_recs if str(r.get("user_id")) == user_id]
    if not user_recs:
        return "你今天还没有抽过老婆哦~"

    daily_limit = get_daily_limit(plugin.plugin_config)
    res = [f"🌸 你今日的老婆记录 ({len(user_recs)}/{daily_limit})："]
    for i, r in enumerate(user_recs, 1):
        try:
            time_str = datetime.fromisoformat(r["timestamp"]).strftime("%H:%M")
        except Exception:
            time_str = ""
        res.append(f"{i}. 【{r['wife_name']}】 ({time_str})")
    res.append(f"\n剩余次数：{max(0, daily_limit - len(user_recs))}次")
    return "\n".join(res)
