"""PIL 图片渲染工具 — /help 命令帮助卡片。

从 status 插件迁移的跨平台中文字体查找 + 深色卡片渲染。
渲染失败(无 PIL/无中文字体)时返回 None, 由调用方降级为文本。
"""

from __future__ import annotations

import glob
import os
import tempfile

# 自带字体: 思源宋体 Source Han Serif SC(仅 PIL 渲染用, 宋体风格,
# Adobe/Sil OFL 开源), 放在 data/fonts/ 或 data/ 根目录。
# 远程部署同样只需把字体文件放进 data/ 即可生效, 无需安装系统字体。
_DATA_FONT_NAMES = [
    "SourceHanSerifSC-Regular.otf",
    "SourceHanSerifSC-Bold.otf",
    "NotoSerifCJKsc-Regular.otf",
    "simsun.ttc",
]

_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",        # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",      # 黑体
    r"C:\Windows\Fonts\simsun.ttc",      # 宋体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Ubuntu/Debian
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",        # Fedora
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def find_cjk_font() -> str | None:
    """查找可用的中文字体文件路径; 找不到返回 None。

    优先级: data/fonts/ 或 data/ 下的宋体(思源宋体) → 系统候选字体 → fontconfig。
    """
    # 1. 项目自带宋体(正式部署/远程都放 data/, 与系统环境无关)
    data_dirs = ["./data/fonts", "./data"]
    for d in data_dirs:
        for name in _DATA_FONT_NAMES:
            path = os.path.join(d, name)
            if os.path.exists(path):
                return path
        # 目录里放了其他字体文件也接受(首个 ttf/ttc/otf)
        for pattern in ("*.ttf", "*.otf", "*.ttc"):
            hits = sorted(glob.glob(os.path.join(d, pattern)))
            if hits:
                return hits[0]

    # 2. 系统候选字体
    for candidate in _CJK_FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    # 兜底: 通过 fontconfig 查找任意支持中文的字体(不依赖固定路径)
    try:
        import subprocess
        proc = subprocess.run(
            ["fc-list", ":lang=zh", "file"],
            capture_output=True, text=True, timeout=5,
        )
        for line in (proc.stdout or "").splitlines():
            path = line.split(":", 1)[0].strip()
            if path and os.path.exists(path):
                return path
    except Exception:
        pass
    return None


def render_info_card(title: str, fields: list[tuple[str, str]], accent: tuple = (102, 204, 255)) -> str | None:
    """渲染"标题 + 字段行"的信息卡片 PNG, 返回临时文件路径; 失败返回 None。

    fields: [(标签, 值), ...] — 每行"标签: 值", 值超长自动截断。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font_path = find_cjk_font()
    if font_path is None:
        return None

    bg = (30, 34, 43)
    fg = (214, 219, 228)
    dim = (148, 155, 168)

    width = 760
    pad_x, pad_y = 26, 24
    title_h = 46
    line_h = 30

    try:
        title_font = ImageFont.truetype(font_path, 20)
        label_font = ImageFont.truetype(font_path, 15)
        body_font = ImageFont.truetype(font_path, 15)
    except Exception:
        return None

    label_w = 90
    value_x = pad_x + label_w
    value_w = width - value_x - pad_x

    # 预计算每行渲染文本(截断)
    rows: list[tuple[str, str]] = []
    for label, value in fields:
        value = str(value)
        while True:
            try:
                w = body_font.getlength(value)
            except AttributeError:
                w = body_font.getsize(value)[0]
            if w <= value_w or len(value) <= 3:
                break
            value = value[:-1]
        if w > value_w:
            value = value.rstrip()[:-1] + "…"
        rows.append((label, value))

    height = pad_y * 2 + title_h + line_h * len(rows) + 10
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    draw.text((pad_x, pad_y), title, font=title_font, fill=fg)
    draw.rectangle([pad_x, pad_y + title_h - 10, pad_x + 170, pad_y + title_h - 6], fill=accent)

    y = pad_y + title_h + 8
    for label, value in rows:
        draw.text((pad_x, y), label, font=label_font, fill=dim)
        draw.text((value_x, y), value, font=body_font, fill=fg)
        y += line_h

    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="mohobot_info_")
    os.close(fd)
    img.save(tmp_path, "PNG")
    img.close()
    return tmp_path


HELP_GROUP_TEXT = "交流群 398870315"
HELP_LINKS_TEXT = (
    "介绍 / 使用须知  https://7121099.xyz/\n"
    "管理员 3831097597"
)
HELP_INFO_TEXT = f"{HELP_GROUP_TEXT}\n{HELP_LINKS_TEXT}"


def _fit_text(text: str, font, max_width: float) -> str:
    """按字体实际宽度截断并以 … 结尾。"""
    if max_width <= 0:
        return ""
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


def _wrap_text(text: str, font, max_width: float) -> list[str]:
    """按字体实际宽度逐字符换行。"""
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


def _normalize_aliases(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        items = [p.strip() for p in value.replace("，", ",").split(",")]
    else:
        items = [str(p).strip() for p in value]
    return [item if item.startswith("/") else f"/{item}" for item in items if item]


def render_help_card(
    sections: list[dict],
    *,
    page: int = 1,
    pages: int = 1,
    total_commands: int | None = None,
    available_commands: int | None = None,
    hidden_admin: int = 0,
    contacts: dict | None = None,
) -> str | None:
    """把命令分组渲染成深色帮助卡片 PNG, 返回临时文件路径; 失败返回 None。

    sections: [{"title", "commands": [{"name", "desc", "usage", "aliases", "admin"}]}]
    命令按两列排布, 每条占「命令行 + 用法/别名行」; 描述按字体实际宽度截断,
    不会在命令或括号中间硬切。管理员命令使用独立徽标。

    page/pages: 分页信息(只影响标题); total/available: 顶部统计;
    hidden_admin: 普通用户被折叠的管理员命令数; contacts: resolve_contacts 结果。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font_path = find_cjk_font()
    if font_path is None:
        return None

    from mohobot.utils.contact_info import resolve_contacts

    bg = (30, 34, 43)          # #1e222b 卡片底色
    fg = (214, 219, 228)       # 正文
    dim = (148, 155, 168)      # 次要文字
    accent = (102, 204, 255)   # 标题/强调 #66CCFF
    admin_color = (244, 164, 96)

    width = 880
    pad_x, pad_y = 30, 26
    col_gap = 40
    col_width = (width - pad_x * 2 - col_gap) // 2
    body_line_h = 23
    detail_line_h = 20
    item_gap = 7
    section_gap = 18

    try:
        title_font = ImageFont.truetype(font_path, 22)
        section_font = ImageFont.truetype(font_path, 17)
        body_font = ImageFont.truetype(font_path, 14)
        small_font = ImageFont.truetype(font_path, 12)
    except Exception:
        return None

    contact = resolve_contacts(contacts)
    admin_text = "管理员 " + (", ".join(contact["admins"]) if contact["admins"] else "未配置")
    info_lines: list[tuple[str, tuple[int, int, int]]] = []
    if contact["group"]:
        info_lines.append((f"交流群 {contact['group']}", accent))
    info_lines.append((admin_text, dim))
    if contact["url"]:
        info_lines.append((f"使用须知 {contact['url']}", dim))

    # 预计算每个命令的渲染信息, 用于算高度与绘制。
    prepared: list[dict] = []
    for sec in sections:
        cmds = [c for c in (sec.get("commands") or []) if c.get("name")]
        if not cmds:
            continue
        items = []
        for c in cmds:
            name = str(c.get("name") or "").strip()
            if not name.startswith("/"):
                name = f"/{name}"
            desc = str(c.get("desc") or "").strip()
            usage = str(c.get("usage") or "").strip()
            aliases = _normalize_aliases(c.get("aliases"))
            detail_parts = []
            if usage:
                detail_parts.append(f"用法：{usage}")
            if aliases:
                detail_parts.append("别名：" + " ".join(aliases))
            detail = " · ".join(detail_parts)
            items.append({
                "name": name,
                "desc": desc,
                "detail": detail,
                "admin": bool(c.get("admin")),
                "h": body_line_h + (detail_line_h if detail else 0) + item_gap,
            })
        prepared.append({"title": str(sec.get("title") or ""), "items": items})

    total = total_commands if total_commands is not None else sum(
        len(p["items"]) for p in prepared
    )
    available = available_commands if available_commands is not None else total

    # 先按实际内容计算高度, 再建画布, 避免固定高度导致内容被裁掉。
    info_render_lines = [
        (line, color)
        for text, color in info_lines
        for line in _wrap_text(text, small_font, width - pad_x * 2)
    ]
    hidden_note_lines: list[str] = []
    if hidden_admin > 0:
        hidden_note_lines = _wrap_text(
            f"管理员指令已折叠：另有 {hidden_admin} 条（管理员可见）",
            small_font, width - pad_x * 2,
        )
    header_h = pad_y + 32 + 24 + 19 * len(info_render_lines) + 8
    body_h = 0
    for sec in prepared:
        items = sec["items"]
        if not items:
            continue
        body_h += 32
        for i in range(0, len(items), 2):
            body_h += max(item["h"] for item in items[i:i + 2])
        body_h += section_gap - item_gap
    canvas_h = max(180, header_h + body_h + 19 * len(hidden_note_lines) + pad_y)
    img = Image.new("RGB", (width, canvas_h), bg)
    draw = ImageDraw.Draw(img)

    y = pad_y
    draw.text((pad_x, y), "指令帮助", font=title_font, fill=fg)
    if pages > 1:
        page_text = f"{page}/{pages}"
        draw.text(
            (width - pad_x - body_font.getlength(page_text), y + 2),
            page_text, font=body_font, fill=dim,
        )
    y += 32

    count_text = f"共 {total} 条"
    if available != total:
        count_text += f" · 你可用 {available} 条"
    draw.text((pad_x, y), count_text, font=small_font, fill=dim)
    y += 24

    for line, color in info_render_lines:
        draw.text((pad_x, y), line, font=small_font, fill=color)
        y += 19
    y += 8

    for sec in prepared:
        items = sec["items"]
        if not items:
            continue
        draw.text((pad_x, y), sec["title"], font=section_font, fill=accent)
        draw.rectangle((pad_x, y + 24, pad_x + 140, y + 26), fill=(60, 70, 90))
        y += 32

        # 两列成对排布, 行高取两列较大者。
        for i in range(0, len(items), 2):
            pair = items[i:i + 2]
            row_h = max(item["h"] for item in pair)
            for col, item in enumerate(pair):
                cx = pad_x + col * (col_width + col_gap)
                name = _fit_text(item["name"], body_font, col_width * 0.45)
                name_w = body_font.getlength(name)
                pill_w = int(small_font.getlength("管理员")) + 14 if item["admin"] else 0
                desc_max = col_width - name_w - 10 - (pill_w + 8 if pill_w else 0)
                desc = _fit_text(item["desc"], body_font, desc_max) if item["desc"] else ""
                draw.text((cx, y), name, font=body_font, fill=accent)
                if desc:
                    draw.text((cx + name_w + 10, y), desc, font=body_font, fill=fg)
                if item["admin"]:
                    pill_x = cx + col_width - pill_w
                    try:
                        draw.rounded_rectangle(
                            (pill_x, y + 1, pill_x + pill_w, y + 19),
                            radius=9, fill=admin_color,
                        )
                    except AttributeError:
                        draw.rectangle(
                            (pill_x, y + 1, pill_x + pill_w, y + 19), fill=admin_color,
                        )
                    draw.text(
                        (pill_x + 7, y + 2), "管理员",
                        font=small_font, fill=(45, 32, 16),
                    )
                if item["detail"]:
                    detail = _fit_text(item["detail"], small_font, col_width)
                    draw.text((cx, y + body_line_h), detail, font=small_font, fill=dim)
            y += row_h

        y += section_gap - item_gap

    for line in hidden_note_lines:
        draw.text((pad_x, y), line, font=small_font, fill=admin_color)
        y += 19

    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="mohobot_help_")
    os.close(fd)
    img.save(tmp_path, "PNG")
    img.close()
    return tmp_path
