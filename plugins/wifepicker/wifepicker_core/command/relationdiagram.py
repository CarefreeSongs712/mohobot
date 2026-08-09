"""关系图 — 移植自 astrbot-plugin-wifepicker src/command/relationdiagram.py。

Playwright 渲染 HTML→PNG(vis-network), 未安装浏览器/渲染失败时降级为文本列表。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from loguru import logger

from ..core import get_group_records
from ..renderer import available, render_png
from ..utils import (
    build_user_map,
    get_group_info,
    get_group_member_list,
    is_allowed_group,
)

_TMP_DIR = "tmp"


async def cmd_show_graph(plugin, bot_id: str, event, rest: str):
    group_id = str(getattr(event, "group_id", ""))
    if not group_id:
        return "此功能仅在群聊中可用哦~"
    if not is_allowed_group(group_id, plugin.plugin_config):
        return None

    iter_count = plugin.plugin_config.get("iterations", 140)
    plugin_dir = Path(plugin._plugin_dir)

    # 读取模板与 vis-network JS
    vis_js_path = plugin_dir / "vis-network.min.js"
    template_path = plugin_dir / "template" / "graph_template.html"
    if not template_path.exists():
        return f"错误：找不到模板文件 {template_path}"
    vis_js_content = ""
    if vis_js_path.exists():
        vis_js_content = vis_js_path.read_text(encoding="utf-8")
    else:
        logger.error(f"找不到 JS 文件: {vis_js_path}")
    graph_html = template_path.read_text(encoding="utf-8")

    # 今日关系记录
    group_data = get_group_records(plugin.store, group_id)
    if not group_data:
        return "今日还没有任何老婆记录，先抽个老婆吧~"

    # 群信息 + 成员名映射
    group_name = "未命名群聊"
    user_map: dict[str, str] = {}
    try:
        info = await get_group_info(plugin._ws_server, bot_id, group_id)
        if info:
            group_name = str(info.get("group_name") or "未命名群聊")
        members = await get_group_member_list(plugin._ws_server, bot_id, group_id)
        user_map = build_user_map(members)
    except Exception as e:
        logger.warning(f"获取群信息失败: {e}")

    # 动态高度
    unique_nodes = set()
    for r in group_data:
        unique_nodes.add(str(r.get("user_id")))
        unique_nodes.add(str(r.get("wife_id")))
    node_count = len(unique_nodes)
    clip_width = 1920
    clip_height = 1080 + (max(0, node_count - 10) * 60)

    try:
        import jinja2
        env = jinja2.Environment()
        html_content = env.from_string(graph_html).render(
            vis_js_content=vis_js_content,
            group_id=group_id,
            group_name=group_name,
            user_map=user_map,
            records=group_data,
            iterations=iter_count,
        )
    except Exception as e:
        logger.error(f"关系图模板渲染失败: {e}")
        return _text_fallback(group_data, user_map)

    # 渲染 PNG
    if available():
        tmp_dir = plugin._data_dir / "plugins_data" / "wifepicker" / _TMP_DIR
        tmp_dir.mkdir(parents=True, exist_ok=True)
        out_png = tmp_dir / f"graph_{uuid.uuid4().hex}.png"
        ok = await render_png(
            html_content, out_png,
            width=clip_width, height=clip_height,
        )
        if ok and plugin._ws_server is not None:
            await plugin._ws_server.send_image(
                bot_id, "group", int(group_id), str(out_png)
            )
            return None  # 图片已发送
        logger.warning("关系图渲染失败, 降级为文本")

    return _text_fallback(group_data, user_map)


def _text_fallback(group_data: list[dict], user_map: dict[str, str]) -> str:
    """文本降级: 列出所有关系。"""
    lines = ["📊 今日老婆关系(文本版):"]
    seen = set()
    for r in group_data:
        uid = str(r.get("user_id"))
        wid = str(r.get("wife_id"))
        key = f"{uid}->{wid}"
        if key in seen:
            continue
        seen.add(key)
        uname = user_map.get(uid) or f"用户({uid})"
        wname = str(r.get("wife_name") or user_map.get(wid) or f"用户({wid})")
        tag = "强娶" if r.get("forced") else "抽中"
        lines.append(f"· {uname} 的{tag}: 【{wname}】")
    if len(lines) == 1:
        return "今日还没有任何老婆记录，先抽个老婆吧~"
    return "\n".join(lines)
