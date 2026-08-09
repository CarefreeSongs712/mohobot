"""rbq 排行 — 移植自 astrbot-plugin-wifepicker src/command/rbqrank.py。

Playwright 渲染 HTML→PNG, 失败降级为文本榜单。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from loguru import logger

from ..core import clean_rbq_stats
from ..renderer import available, render_png
from ..utils import build_user_map, get_group_member_list, is_allowed_group


async def cmd_rbq_ranking(plugin, bot_id: str, event, rest: str):
    group_id = str(getattr(event, "group_id", ""))
    if not group_id:
        return "私聊看不了榜单哦~"

    clean_rbq_stats(plugin.store)
    await plugin.store.flush(force=True)

    group_data = plugin.store.rbq_stats.get(group_id, {})
    if not group_data:
        return "本群近30天还没有人被强娶过，大家都很有礼貌呢。"

    user_map: dict[str, str] = {}
    try:
        members = await get_group_member_list(plugin._ws_server, bot_id, group_id)
        user_map = build_user_map(members)
    except Exception:
        pass

    sorted_list = [
        {"uid": uid, "name": user_map.get(uid, f"用户({uid})"), "count": len(ts_list)}
        for uid, ts_list in group_data.items()
    ]
    sorted_list.sort(key=lambda x: x["count"], reverse=True)
    top_10 = sorted_list[:10]

    current_rank = 1
    for i, user in enumerate(top_10):
        if i > 0 and user["count"] < top_10[i - 1]["count"]:
            current_rank = i + 1
        user["rank"] = current_rank

    template_path = Path(plugin._plugin_dir) / "template" / "rbq_ranking.html"
    if not template_path.exists():
        return "错误：找不到排行模板 rbq_ranking.html"
    template_content = template_path.read_text(encoding="utf-8")

    if available():
        try:
            import jinja2
            env = jinja2.Environment()
            html_content = env.from_string(template_content).render(
                group_id=group_id,
                ranking=top_10,
                title="❤️ 群rbq月榜 ❤️",
            )
            header_h, item_h, footer_h = 100, 60, 50
            rank_width = 400
            dynamic_height = header_h + (len(top_10) * item_h) + footer_h

            tmp_dir = Path(plugin._data_dir) / "plugins_data" / "wifepicker" / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            out_png = tmp_dir / f"rbq_{uuid.uuid4().hex}.png"
            ok = await render_png(
                html_content, out_png,
                width=rank_width, height=dynamic_height,
            )
            if ok and plugin._ws_server is not None:
                await plugin._ws_server.send_image(
                    bot_id, "group", int(group_id), str(out_png)
                )
                return None  # 图片已发送
            logger.warning("rbq 排行渲染失败, 降级为文本")
        except Exception as e:
            logger.error(f"渲染RBQ排行失败: {e}")

    # 文本降级
    lines = ["❤️ 群rbq月榜(文本版) ❤️"]
    for u in top_10:
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(u["rank"], f"{u['rank']}.")
        lines.append(f"{medal} {u['name']} — 被强娶 {u['count']} 次")
    return "\n".join(lines)
