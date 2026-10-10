"""Bounded, lossless two-column comparison cards (in-memory PNG)."""
import base64
import io
import math

from mohobot.utils.image_card import find_cjk_font


def wrap_text(text, font, width):
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            if line and font.getlength(line + char) > width:
                lines.append(line)
                line = ""
            line += char
        lines.append(line)
    return lines


def render_comparison(identifier, left, right, max_pages=8):
    if len(left) + len(right) > 40000:
        raise ValueError("候选超过图卡字符限制，改发完整纯文字")
    from PIL import Image, ImageDraw, ImageFont
    font_path = find_cjk_font()
    if not font_path:
        raise ValueError("没有可用中文字体")
    font = ImageFont.truetype(font_path, 22)
    small = ImageFont.truetype(font_path, 18)
    columns = [wrap_text(text, font, 530) for text in (left, right)]
    per_page = 32
    pages = max(1, math.ceil(max(map(len, columns)) / per_page))
    if pages > max_pages:
        raise ValueError("候选超过图卡页数限制，改发完整纯文字")
    output = []
    for page in range(pages):
        image = Image.new("RGB", (1200, 1240), "#f5f7fc")
        draw = ImageDraw.Draw(image)
        draw.text((30, 20), f"匿名回复对比  {identifier}  第 {page + 1}/{pages} 页", font=small, fill="#182030")
        draw.text((30, 65), "A（左）", font=font, fill="#182030")
        draw.text((630, 65), "B（右）", font=font, fill="#182030")
        draw.line((600, 60, 600, 1130), fill="#a0a5af", width=2)
        for column, lines in enumerate(columns):
            for row, line in enumerate(lines[page * per_page:(page + 1) * per_page]):
                draw.text((30 + column * 600, 110 + row * 31), line, font=font, fill="#182030")
        draw.text((30, 1135), f"/ab vote {identifier} A（或 B / 平局 / 都不好 / 跳过）", font=small, fill="#182030")
        draw.text((30, 1175), "可以继续聊天并稍后投票；投票不会自动切换人设。", font=small, fill="#182030")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        output.append({"type": "image", "data": {"file": "base64://" + base64.b64encode(stream.getvalue()).decode("ascii")}})
    return output
