"""`/status` 深色状态卡片渲染。

输入由 status 插件采集(psutil / BotManager / 插件配置), 本模块只负责排版,
便于离线单测:

    {
      "bot_id": "bot_003", "nickname": "昵称",
      "now": "2026-11-07 21:59",
      "runtime_secs": 432000, "plugin_runtime_secs": 432007,
      "system_uptime_secs": 867694,
      "cpu": {"count": 8, "average": 43.0, "cores": [23, 41, ...]},
      "memory": {"total": ..., "used": ..., "cache": ..., "free": ...,
                 "percent": 71.0, "swap_total": ..., "swap_used": ...,
                 "swap_percent": 20.0},
      "disks": [{"mount": "/", "used": ..., "total": ..., "percent": 76.0}],
      "bots": [{"bot_id": "bot_001", "nickname": "昵称", "online_secs": 40000}],
      "contacts": {"admins": ["3831097597"], "group": "398870315",
                   "url": "https://7121099.xyz/"},
    }

渲染失败(无 PIL / 无中文字体)返回 None, 由插件降级为文本。
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

# ── 配色 ─────────────────────────────────────────────────────
BG = (30, 34, 43)            # #1e222b
FG = (214, 219, 228)
DIM = (148, 155, 168)
ACCENT = (102, 204, 255)     # #66CCFF
GREEN = (74, 222, 128)
AMBER = (251, 191, 36)
RED = (248, 113, 113)
CACHE = (96, 165, 250)
TRACK = (58, 66, 80)
SEP = (55, 63, 76)

CARD_W = 820
PAD = 30
COL_GAP = 40

# CPU 方块: 每行最多 8 个; 超过 32 核按 8 组合并
CPU_PER_ROW = 8
CPU_SQUARE = 54
CPU_GAP = 10
CPU_AGGREGATE_OVER = 32


def format_duration(seconds: float | int | None) -> str:
    """把秒数格式化为紧凑中文时长: 10天1小时 / 23时44分 / 22分11秒。"""
    try:
        total = int(max(0, float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}天{hours}小时" if hours else f"{days}天"
    if hours:
        return f"{hours}时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def aggregate_cores(
    cores: list[float] | None,
    *,
    per_row: int = CPU_PER_ROW,
    aggregate_over: int = CPU_AGGREGATE_OVER,
) -> tuple[list[float], bool]:
    """返回 (用于绘制的核心值, 是否合并显示)。

    <= 32 核全部展示; > 32 核按 8 组取平均, 避免方块无限增长。
    """
    values = [float(v or 0) for v in (cores or [])]
    if len(values) <= aggregate_over:
        return values, False
    group_count = per_row
    group_size = max(1, (len(values) + group_count - 1) // group_count)
    grouped = [
        sum(values[i:i + group_size]) / len(values[i:i + group_size])
        for i in range(0, len(values), group_size)
    ]
    return grouped, True


def usage_color(percent: float) -> tuple[int, int, int]:
    """按占用率返回绿→黄→橙→红的连续色。"""
    p = max(0.0, min(100.0, float(percent or 0)))
    stops = [
        (0.0, GREEN),
        (50.0, AMBER),
        (80.0, (249, 115, 22)),
        (100.0, RED),
    ]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p <= p1:
            t = (p - p0) / (p1 - p0) if p1 > p0 else 0.0
            return tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))  # type: ignore[return-value]
    return RED


def _text_color(fill: tuple[int, int, int]) -> tuple[int, int, int]:
    """根据填充色亮度选择黑/白文字, 保证方块内百分比可读。"""
    r, g, b = fill
    return (25, 30, 38) if (r * 299 + g * 587 + b * 114) / 1000 > 150 else (250, 252, 255)


def _level_meta(percent: float) -> tuple[str, tuple[int, int, int]]:
    if percent >= 85:
        return "告警", RED
    if percent >= 70:
        return "注意", AMBER
    return "正常", GREEN


def _fmt_gb(value: float | int | None) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    gb = v / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f}GB"
    mb = v / (1024 ** 2)
    return f"{mb:.0f}MB"


def _fit(text: str, font, max_width: float) -> str:
    """按字体实际宽度截断并以 … 结尾。"""
    if max_width <= 0:
        return ""
    if font.getlength(text) <= max_width:
        return text
    ellipsis = "…"
    while text and font.getlength(text + ellipsis) > max_width:
        text = text[:-1]
    return (text + ellipsis) if text else ""


def _wrap(text: str, font, max_width: float) -> list[str]:
    """按字体实际宽度换行(逐字符, 适合中英混排)。"""
    if not text:
        return []
    lines: list[str] = []
    line = ""
    for ch in text:
        if line and font.getlength(line + ch) > max_width:
            lines.append(line)
            line = ""
        line += ch
    if line:
        lines.append(line)
    return lines


def _rounded_rect(draw, box, radius: int, fill) -> None:
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill)
    except AttributeError:  # 老版本 Pillow
        draw.rectangle(box, fill=fill)


def _bar(draw, x: int, y: int, width: int, height: int, percent: float,
         color: tuple[int, int, int]) -> None:
    _rounded_rect(draw, (x, y, x + width, y + height), height // 2, TRACK)
    fill_w = int(width * max(0.0, min(100.0, float(percent or 0))) / 100.0)
    if fill_w > 0:
        _rounded_rect(draw, (x, y, x + max(fill_w, height), y + height), height // 2, color)


def _chip(draw, x: int, y: int, color: tuple[int, int, int]) -> None:
    draw.rectangle((x, y + 3, x + 10, y + 13), fill=color)


def render_status_card(data: dict[str, Any]) -> str | None:
    """渲染状态卡片 PNG, 返回临时文件路径; 失败返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    from mohobot.utils.image_card import find_cjk_font

    font_path = find_cjk_font()
    if font_path is None:
        return None

    try:
        title_font = ImageFont.truetype(font_path, 24)
        subtitle_font = ImageFont.truetype(font_path, 16)
        section_font = ImageFont.truetype(font_path, 16)
        body_font = ImageFont.truetype(font_path, 14)
        small_font = ImageFont.truetype(font_path, 13)
        square_font = ImageFont.truetype(font_path, 14)
    except Exception:
        return None

    # 先画在足够高的画布上, 结束后按实际用高裁剪, 避免重复计算布局。
    canvas_h = 4096
    img = Image.new("RGB", (CARD_W, canvas_h), BG)
    draw = ImageDraw.Draw(img)

    # ── 标题区 ──
    x = PAD
    y = PAD
    draw.text((x, y), "Mohobot 状态", font=title_font, fill=FG)

    cpu = data.get("cpu") or {}
    mem = data.get("memory") or {}
    disks = data.get("disks") or []
    main_disk = disks[0] if disks else {}
    metrics = [
        float(cpu.get("average") or 0),
        float(mem.get("percent") or 0),
        float(main_disk.get("percent") or 0),
    ]
    available_metrics = [m for m in metrics if m > 0]
    level_pct = max(available_metrics) if available_metrics else 0.0
    level_text, level_color = _level_meta(level_pct)

    pill_text = level_text
    pill_w = int(small_font.getlength(pill_text)) + 24
    pill_x = CARD_W - PAD - pill_w
    _rounded_rect(draw, (pill_x, y + 2, pill_x + pill_w, y + 28), 13, level_color)
    draw.text(
        (pill_x + 12, y + 5), pill_text, font=small_font,
        fill=_text_color(level_color),
    )

    y += 36
    bot_id = str(data.get("bot_id") or "?")
    nickname = str(data.get("nickname") or bot_id)
    identity = f"{bot_id} · {nickname}"
    draw.text((x, y), _fit(identity, subtitle_font, CARD_W - 2 * PAD), font=subtitle_font, fill=ACCENT)

    y += 24
    summary = (
        f"框架 {format_duration(data.get('runtime_secs'))} · "
        f"插件 {format_duration(data.get('plugin_runtime_secs'))} · "
        f"系统 {format_duration(data.get('system_uptime_secs'))} · "
        f"{data.get('now') or ''}"
    )
    draw.text((x, y), _fit(summary, small_font, CARD_W - 2 * PAD), font=small_font, fill=DIM)

    # ── CPU ──
    y += 38
    cores, aggregated = aggregate_cores(cpu.get("cores"))
    avg = float(cpu.get("average") or 0)
    core_count = int(cpu.get("count") or len(cores))
    if aggregated:
        cpu_label = f"CPU  {core_count} 核（合并 {len(cores)} 组） · 平均 {avg:.0f}%"
    else:
        cpu_label = f"CPU  {core_count} 逻辑核 · 平均 {avg:.0f}%"
    draw.text((x, y), cpu_label, font=section_font, fill=FG)
    y += 28

    for row_start in range(0, len(cores), CPU_PER_ROW):
        row = cores[row_start:row_start + CPU_PER_ROW]
        for i, value in enumerate(row):
            sx = x + i * (CPU_SQUARE + CPU_GAP)
            color = usage_color(value)
            _rounded_rect(
                draw, (sx, y, sx + CPU_SQUARE, y + CPU_SQUARE), 8, color,
            )
            label = f"{int(round(value))}%"
            tw = square_font.getlength(label)
            draw.text(
                (sx + (CPU_SQUARE - tw) / 2, y + (CPU_SQUARE - 16) / 2),
                label, font=square_font, fill=_text_color(color),
            )
        y += CPU_SQUARE + CPU_GAP

    y += 2
    legend = [("低 <50%", GREEN), ("中 50–80%", AMBER), ("高 >80%", RED)]
    lx = x
    for text, color in legend:
        _chip(draw, lx, y, color)
        lx += 16
        draw.text((lx, y), text, font=small_font, fill=DIM)
        lx += int(small_font.getlength(text)) + 18

    # ── 内存 ──
    y += 32
    mem_total = float(mem.get("total") or 0)
    mem_used = float(mem.get("used") or 0)
    mem_cache = float(mem.get("cache") or 0)
    mem_free = float(mem.get("free") or max(mem_total - mem_used - mem_cache, 0))
    mem_pct = float(mem.get("percent") or 0)
    draw.text(
        (x, y),
        f"内存  {_fmt_gb(mem_used)} / {_fmt_gb(mem_total)}  ({mem_pct:.0f}%)",
        font=section_font, fill=FG,
    )
    y += 26
    bar_w = CARD_W - 2 * PAD
    bar_h = 26
    _rounded_rect(draw, (x, y, x + bar_w, y + bar_h), bar_h // 2, TRACK)
    if mem_total > 0:
        used_w = int(bar_w * min(mem_used / mem_total, 1.0))
        cache_w = int(bar_w * min(mem_cache / mem_total, 1.0))
        if used_w:
            draw.rectangle((x, y, x + used_w, y + bar_h), fill=usage_color(mem_pct))
        if cache_w:
            draw.rectangle((x + used_w, y, x + min(used_w + cache_w, bar_w), y + bar_h), fill=CACHE)
    y += bar_h + 4

    legend_items = [
        ("应用", mem_used, usage_color(mem_pct)),
        ("缓存", mem_cache, CACHE),
        ("可用", mem_free, TRACK),
    ]
    lx = x
    for label, value, color in legend_items:
        _chip(draw, lx, y, color)
        lx += 16
        text = f"{label} {_fmt_gb(value)}"
        draw.text((lx, y), text, font=small_font, fill=DIM)
        lx += int(small_font.getlength(text)) + 20

    # Swap
    swap_total = float(mem.get("swap_total") or 0)
    y += 26
    if swap_total > 0:
        swap_pct = float(mem.get("swap_percent") or 0)
        draw.text(
            (x, y),
            f"Swap  {_fmt_gb(mem.get('swap_used'))} / {_fmt_gb(swap_total)}  ({swap_pct:.0f}%)",
            font=body_font, fill=FG,
        )
        y += 22
        _bar(draw, x, y, bar_w, 14, swap_pct, usage_color(swap_pct))
        y += 18
    else:
        draw.text((x, y), "Swap  未启用", font=small_font, fill=DIM)
        y += 22

    # ── 磁盘 ──
    y += 18
    main_pct = float(main_disk.get("percent") or 0)
    mount = str(main_disk.get("mount") or "/")
    disk_label = (
        f"磁盘 {mount}  {_fmt_gb(main_disk.get('used'))} / "
        f"{_fmt_gb(main_disk.get('total'))}  ({main_pct:.0f}%)"
    )
    draw.text((x, y), disk_label, font=section_font, fill=FG)
    if main_disk:
        tag = "告警" if main_pct >= 85 else ("偏高" if main_pct >= 70 else "")
        if tag:
            tag_w = int(small_font.getlength(tag))
            draw.text(
                (x + CARD_W - 2 * PAD - tag_w, y + 2), tag,
                font=small_font, fill=usage_color(main_pct),
            )
    y += 26
    if main_disk:
        _bar(draw, x, y, bar_w, 22, main_pct, usage_color(main_pct))
        y += 30

    others = disks[1:4]
    if others:
        parts = [f"{d.get('mount')} {float(d.get('percent') or 0):.0f}%" for d in others]
        text = "其他盘：" + " · ".join(parts)
        if len(disks) > 4:
            text += f" · 等 {len(disks) - 1} 个"
        draw.text((x, y), _fit(text, small_font, bar_w), font=small_font, fill=DIM)
        y += 22

    # ── Bot 连接 ──
    bots = list(data.get("bots") or [])
    y += 16
    draw.text(
        (x, y), f"Bot 连接  {len(bots)} / {len(bots)}",
        font=section_font, fill=FG,
    )
    y += 28

    col_w = (CARD_W - 2 * PAD - COL_GAP) // 2
    rows = (len(bots) + 1) // 2
    for row in range(rows):
        for col in range(2):
            idx = row + col * rows
            if idx >= len(bots):
                continue
            bot = bots[idx]
            bx = x + col * (col_w + COL_GAP)
            draw.ellipse((bx + 2, y + 7, bx + 10, y + 15), fill=GREEN)
            duration = format_duration(bot.get("online_secs"))
            dur_w = int(small_font.getlength(duration))
            dur_x = bx + col_w - dur_w
            name_x = bx + 16
            name = str(bot.get("nickname") or bot.get("bot_id") or "?")
            bid = f"（{bot.get('bot_id')}）"
            bid_w = small_font.getlength(bid)
            max_name_w = col_w - 16 - dur_w - 10 - bid_w
            draw.text((name_x, y), _fit(name, body_font, max_name_w), font=body_font, fill=FG)
            draw.text(
                (name_x + int(body_font.getlength(_fit(name, body_font, max_name_w))), y + 1),
                bid, font=small_font, fill=DIM,
            )
            draw.text((dur_x, y + 1), duration, font=small_font, fill=DIM)
        y += 30

    # ── 页脚 ──
    y += 10
    draw.line((x, y, CARD_W - PAD, y), fill=SEP, width=1)
    y += 14

    contacts = data.get("contacts") or {}
    admins = contacts.get("admins") or []
    admin_text = "管理员 " + (", ".join(str(a) for a in admins) if admins else "未配置")
    for line in _wrap(admin_text, small_font, bar_w):
        draw.text((x, y), line, font=small_font, fill=DIM)
        y += 20

    group = str(contacts.get("group") or "")
    url = str(contacts.get("url") or "")
    contact_line = " · ".join(part for part in (
        f"交流群 {group}" if group else "",
        f"使用须知 {url}" if url else "",
    ) if part)
    for line in _wrap(contact_line, small_font, bar_w):
        draw.text((x, y), line, font=small_font, fill=DIM)
        y += 20

    y += 10
    final_h = min(canvas_h, max(240, y))
    img = img.crop((0, 0, CARD_W, final_h))
    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="mohobot_status_")
    os.close(fd)
    img.save(tmp_path, "PNG")
    img.close()
    return tmp_path
