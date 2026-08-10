"""PIL 图片渲染工具 — /help 命令帮助卡片。

从 status 插件迁移的跨平台中文字体查找 + 深色卡片渲染。
渲染失败(无 PIL/无中文字体)时返回 None, 由调用方降级为文本。
"""

from __future__ import annotations

import os
import tempfile

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
    """查找可用的中文字体文件路径; 找不到返回 None。"""
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


def render_help_card(sections: list[dict]) -> str | None:
    """把命令分组渲染成深色帮助卡片 PNG, 返回临时文件路径; 失败返回 None。

    sections: [{"title": str, "commands": [{"name", "desc", "admin"}]}]
    每个分组标题横贯整行, 组内命令条目按两列排布。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font_path = find_cjk_font()
    if font_path is None:
        return None

    bg = (30, 34, 43)          # #1e222b 卡片底色
    fg = (214, 219, 228)       # 正文
    dim = (148, 155, 168)      # 次要文字
    accent = (102, 204, 255)   # 标题/强调 #66CCFF
    warn = (255, 170, 90)      # 管理员标记 #FFAA5A

    width = 880
    pad_x, pad_y = 30, 26
    col_gap = 40               # 两列之间的间距
    title_h = 46               # 卡片大标题区高度
    section_gap = 14           # 分组之间的间距
    line_h = 26

    try:
        title_font = ImageFont.truetype(font_path, 22)
        section_font = ImageFont.truetype(font_path, 17)
        body_font = ImageFont.truetype(font_path, 14)
    except Exception:
        return None

    # ── 预计算每个分组的渲染行数(两列, 每行最多 2 条) ──
    col_width = (width - pad_x * 2 - col_gap) // 2
    for sec in sections:
        cmds = sec["commands"]
        if not cmds:
            continue
        # 每条命令生成 (命令前缀文本, 描述文本) 并按列宽截断
        rows: list[tuple[str, str]] = []
        for c in cmds:
            label = f"/{c['name']}"
            if c.get("admin"):
                label += " [管理员]"
            desc = (c.get("desc") or "").strip()
            rows.append((label, desc))
        # 计算每条命令的实际渲染文本(截断到列宽)
        rendered: list[tuple[str, str]] = []
        for label, desc in rows:
            if desc:
                line = f"{label} — {desc}"
            else:
                line = label
            # 截断: 超过列宽则去尾加 …
            while True:
                w = None
                try:
                    w = body_font.getlength(line)
                except AttributeError:
                    w = body_font.getsize(line)[0]
                if w <= col_width or len(line) <= 3:
                    break
                line = line[:-1]
            if w > col_width:
                line = line.rstrip()[:-1] + "…"
            rendered.append(line)
        sec["_rows"] = [rendered[i:i + 2] for i in range(0, len(rendered), 2)]
        sec["_row_count"] = len(sec["_rows"])

    # ── 计算卡片总高度 ──
    height = pad_y * 2 + title_h
    for sec in sections:
        if not sec.get("_row_count"):
            continue
        height += section_font.getbbox("H")[3] + 8 + line_h * sec["_row_count"] + section_gap
    height += 10

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # ── 大标题(纯文字, PIL 无法渲染 emoji) ──
    draw.text((pad_x, pad_y), "指令帮助", font=title_font, fill=fg)
    draw.text((pad_x + 300, pad_y + 28), "Mohobot 可用指令一览", font=body_font, fill=dim)
    draw.rectangle([pad_x, pad_y + title_h - 10, pad_x + 180, pad_y + title_h - 6], fill=accent)

    y = pad_y + title_h + 6
    for sec in sections:
        rows = sec.get("_rows") or []
        if not rows:
            continue
        # 分组标题 + 下划线
        draw.text((pad_x, y), sec["title"], font=section_font, fill=accent)
        draw.rectangle([pad_x, y + section_font.getbbox("H")[3] + 2,
                        pad_x + 140, y + section_font.getbbox("H")[3] + 4], fill=(60, 70, 90))
        y += section_font.getbbox("H")[3] + 10
        for row in rows:
            for i, text in enumerate(row):
                x = pad_x + i * (col_width + col_gap)
                draw.text((x, y), text, font=body_font, fill=fg)
            y += line_h
        y += section_gap

    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="mohobot_help_")
    os.close(fd)
    img.save(tmp_path, "PNG")
    img.close()
    return tmp_path
