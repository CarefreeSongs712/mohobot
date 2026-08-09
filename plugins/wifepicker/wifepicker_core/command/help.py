"""帮助命令 — 移植自 astrbot-plugin-wifepicker src/command/help.py。"""

from ..core import get_active_user_days, get_daily_limit
from ..utils import is_allowed_group


async def cmd_show_help(plugin, bot_id: str, event, rest: str):
    group_id = str(getattr(event, "group_id", ""))
    if not is_allowed_group(group_id, plugin.plugin_config):
        return None
    daily_limit = get_daily_limit(plugin.plugin_config)
    active_days = get_active_user_days(plugin.plugin_config)
    return (
        "===== 🌸 抽老婆帮助 =====\n"
        "1. 【/今日老婆】(抽老婆/jrlp)：随机抽取今日老婆\n"
        "2. 【/强娶 @某人】(qiangqu)：强行更换今日老婆(有冷却期)\n"
        "3. 【/我的老婆】(wdlp)：查看今日历史与次数\n"
        "4. 【/关系图】(gxt)：查看群友老婆的关系图谱\n"
        "5. 【/rbq排行】(rbqph)：近30天被强娶次数排行\n"
        "6. 【/求婚 @某人】(qh)：向群友求婚(30秒内回复同意/拒绝)\n"
        "7. 【/重置记录】(管理员)：清空今日抽取记录\n"
        "8. 【/重置强娶时间】(管理员)：清空本群强娶冷却\n"
        "9. 【/重置求婚时间】(管理员)：清空本群求婚冷却\n"
        f"当前每日上限：{daily_limit}次\n"
        "提示：可在配置开启“关键词触发”，直接发送关键词无需 / 前缀。\n"
        f"注：仅限{active_days}天内发言且当前在群的活跃群友。"
    )
