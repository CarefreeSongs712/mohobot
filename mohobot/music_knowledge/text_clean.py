# -*- coding: utf-8 -*-
"""wiki 标记清洗: 把 VCPedia wikitext 残留的标记转成纯文本。

清洗原则: 只做文本变换, 不丢内容——
- 彩色歌词的 <span>/<font>/<div> 标签壳删除, 标签内歌词全部保留;
- {{ruby|字|注音}} 展开为 "字（注音）"; 其余内容模板保留正文;
- 播放/收藏数模板({{bilibiliCount|...}})数字过时且无用, 整体删除;
- <ref> 注释(考据/引用)整体删除——注意自闭合 <ref/> 只删标记本身,
  绝不能连同其后的正文一起删;
- [[链接|显示文本]] 保留显示文本。

供两处使用:
- scripts/clean_knowledge_db.py 对现有库做一次性就地清洗;
- vcpedia.build_song_record 在爬虫入库前清洗(根治残留来源)。
"""
import html
import re
from typing import List

# ── 模板分类 ─────────────────────────────────────────────────────

# 观看/收藏数模板: 数字随时间过时, 对机器人无用 → 整体删除
_COUNT_TEMPLATES = {
    "bilibilicount", "youtubecount", "niconicocount",
}

# 无正文参数、整体无意义 → 整体删除
_JUNK_TEMPLATES = {
    "fact", "dead", "anchor", "pagename", "localmonthname", "localday",
    "已故人物标注", "vocaloid中文殿堂曲题头", "synthesizer v中文殿堂曲题头",
    "vocaloid_songbox", "ldanmu", "ldanmucanvas", "mathjax", "curly",
}

# 注音模板 → "字（注音）"
_RUBY_TEMPLATES = {"ruby", "rubyh"}

# 语言代码参数({{lang|en|text}} 的 'en'), 属样式段而非正文
_LANG_CODES = {
    "en", "ja", "zh", "ko", "ru", "fr", "de", "es", "it", "la", "pt",
    "ar", "th", "vi", "fra", "eng", "jpn", "zh-hans", "zh-hant",
    "zh-cn", "zh-tw", "zh-hk",
}

# 颜色词参数({{color|black|text}} 的 'black')
_COLOR_WORDS = {
    "black", "white", "red", "blue", "green", "yellow", "gold", "silver",
    "pink", "purple", "orange", "gray", "grey", "brown", "cyan", "violet",
    "indigo", "transparent", "crimson", "azure", "aqua", "coral", "ivory",
    "khaki", "lime", "maroon", "navy", "olive", "orchid", "plum", "tan",
    "teal", "amber",
}

_STYLE_ATTR_RE = re.compile(
    r"text-shadow|shadow|font|background|border|color|size|weight|style|opacity|rgba?\(",
    re.I,
)

# 命名参数(id=BVxx / type=4 / c1=#66ccff)
_NAMED_PARAM_RE = re.compile(r"^\s*[\w\u4e00-\u9fff\s]{1,20}\s*=[^=]", re.S)


def _find_template_end(text: str, start: int) -> int:
    """text[start:start+2] == '{{' 时, 返回匹配 '}}' 的结束下标(含2字符)。

    逐字符深度扫描, 正确处理嵌套模板; 找不到闭合返回 -1。
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        if text.startswith("{{", i):
            depth += 1
            i += 2
        elif text.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return -1


def _split_template_parts(body: str) -> List[str]:
    """按模板外层的 '|' 拆参数(嵌套模板内部的 '|' 不拆)。"""
    parts: List[str] = []
    cur: List[str] = []
    depth = 0
    i = 0
    n = len(body)
    while i < n:
        if body.startswith("{{", i) or body.startswith("[[", i):
            depth += 1
            cur.append(body[i:i + 2])
            i += 2
        elif body.startswith("}}", i) or body.startswith("]]", i):
            depth -= 1
            cur.append(body[i:i + 2])
            i += 2
        elif body[i] == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(body[i])
            i += 1
    parts.append("".join(cur))
    return parts


def _is_style_param(part: str) -> bool:
    """命名参数/纯样式段(如 id=BVxx、c1=#66ccff、text-shadow:...)。"""
    s = part.strip()
    if not s or _NAMED_PARAM_RE.match(s):
        return True
    if s.lower() in _LANG_CODES or s.lower() in _COLOR_WORDS:
        return True
    if s.startswith("#") or _STYLE_ATTR_RE.search(s):
        return True
    # 纯色值/样式串(如 #66ccff; 0 0 4px): 样式符号+数字, 不含 CJK/假名
    if len(s) <= 80 and re.fullmatch(r"[#\w;:,.()\- ]+", s) \
            and not re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", s) \
            and (";" in s or ":" in s or re.search(r"\d", s)):
        return True
    return False


def _expand_template(name: str, parts: List[str]) -> str:
    """单个模板 → 纯文本。name 已小写、去空白。"""
    if name in _COUNT_TEMPLATES or name in _JUNK_TEMPLATES:
        return ""
    if name in _RUBY_TEMPLATES:
        args = [p.strip() for p in parts[1:] if not _is_style_param(p)]
        if len(args) >= 2:
            return f"{args[0]}（{args[1]}）"
        return args[0] if args else ""
    # 其余内容模板(黑幕/lj/lang/color/文字描边等): 保留正文
    args = [p.strip() for p in parts[1:] if not _is_style_param(p)]
    return "".join(args)


def _expand_all_templates(text: str) -> str:
    """迭代展开全部模板(含嵌套), 直到无 '{{'。"""
    for _ in range(10):
        if "{{" not in text:
            break
        out: List[str] = []
        i = 0
        n = len(text)
        while i < n:
            if text.startswith("{{", i):
                end = _find_template_end(text, i)
                if end < 0:
                    # 未闭合: 丢弃残缺模板, 避免残留
                    break
                body = text[i + 2:end - 2]
                parts = _split_template_parts(body)
                name = parts[0].strip().lower()
                out.append(_expand_template(name, parts))
                i = end
            else:
                out.append(text[i])
                i += 1
        text = "".join(out)
    return text


# ── 链接 ────────────────────────────────────────────────────────

def _resolve_wikilinks(text: str) -> str:
    """[[A|B]] → B, [[A]] → A; 迭代处理嵌套。"""
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\[\[([^\[\]|]+)\|([^\[\]|]*)\]\]", r"\2", text)
        text = re.sub(r"\[\[([^\[\]]+)\]\]",
                      lambda m: m.group(1).split("#")[0], text)
    return text


# 跨 wiki 前缀(vjp: / cv: 等), 链接被剥壳后残留在文本里
_INTERWIKI_RE = re.compile(
    r"^(?:vjp|cv|moegirl|mmd|vocaloid|utau|niconico|sm|nm)\s*:\s*", re.I)

# 链接剥壳后的裸 "X|Y" 残留: X 为链接名, Y 为显示文本, 取 Y 丢 X。
# 注意 X 的匹配不能越过句读/书名号/括号, 否则会把链接名前面的正文
# 胶水(如 "《ONE OFF MIND》是" 里的 "MIND》是")一起吃掉。
_BARE_PIPE_CLASS = r"[^\s|，。；：！？、「」『』《》【】()（）\n]"
_BARE_PIPE_RE = re.compile(rf"{_BARE_PIPE_CLASS}{{1,40}}\|")
# 跨wiki链接残留: VJP:栗山夕璃|蜂屋ななし → 蜂屋ななし (冒号链接名单独处理,
# 避免通用规则把冒号前正文吃掉)
_BARE_PIPE_IW_RE = re.compile(r"[A-Za-z]{1,10}:[^|\n。，、]{1,40}\|")
# 成就/分类徽章链接残留: 已达成VOCALOID传说曲|传说 → 已达成传说
_BARE_PIPE_BADGE_RE = re.compile(
    r"(?:VOCALOID|UTAU|Synthesizer\s*V|ACE\s*Studio|Sharpkey|DeepVocal|"
    r"X\s*Studio|MUTA)[^|\n。，、]{0,25}\|")
# 括号消歧后缀残留: 葛平(声库)|葛平 / Aya(画师)|Aya → 葛平 / Aya
# 链接名不含胶水虚词(是/的/由...), 防止把 "是Aya(画师)|Aya" 的 "是" 一起吃掉
_BARE_PIPE_PAREN_RE = re.compile(
    r"[^\s|，。；：！？、「」『』《》【】\n是的了为於于由与和在]{1,8}\([^)\n]{1,20}\)\|")
# 书名号内残留: 《桃花雪(专辑)|桃花雪》→《桃花雪》
_BARE_PIPE_TITLE_RE = re.compile(r"《([^《》|]{1,60})\|([^《》|]{1,60})》")
# 「标题」|显示文本 残留(链接名含引号, 通用规则跨不过 」)
_BARE_PIPE_QUOTE_RE = re.compile(r"「[^「」|]{1,40}」\s*\|")


def _resolve_bare_pipes(text: str) -> str:
    text = _BARE_PIPE_IW_RE.sub("", text)
    text = _BARE_PIPE_BADGE_RE.sub("", text)
    text = _BARE_PIPE_PAREN_RE.sub("", text)
    text = _BARE_PIPE_QUOTE_RE.sub("", text)
    text = _BARE_PIPE_TITLE_RE.sub(lambda m: "《" + m.group(2).split("|")[-1] + "》", text)
    for _ in range(5):
        if "|" not in text:
            break
        text = _BARE_PIPE_RE.sub("", text)
    return text


# ── HTML ────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """删 HTML 注释/ref/标签壳, 保留标签内正文, 解码 HTML 实体。

    ref 必须先删成对的 <ref>..</ref>, 再删自闭合 <ref/> 与未闭合的
    <ref 属性> 标记壳——顺序错了会把自闭合 ref 之后的正文全部吞掉。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.I | re.S)
    text = re.sub(r"<ref[^>]*/>", "", text, flags=re.I)
    text = re.sub(r"<ref[^>]*>", "", text, flags=re.I)
    # nowiki 只是转义壳, 删标签留正文
    text = re.sub(r"</?nowiki>", "", text, flags=re.I)
    # 块级标签(div/p/br 等)是换行边界, 先转成换行防止相邻行粘连;
    # 相邻的块级标签折叠为单个换行, 避免逐行标签把歌词撑成隔行空行
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(?:div|p|poem|table|tr|td|th)[^>]*>", "\x00", text, flags=re.I)
    text = re.sub(r"\x00[ \t]*(?:\x00[ \t]*)+", "\x00", text)
    text = text.replace("\x00", "\n")
    # 其余标签壳(span/font/center 等)删除, 标签内正文保留
    # 标签属性可含换行(如 color:#EDE998;\n), 允许跨行匹配, 长度上限防误吞
    text = re.sub(r"<[^>]{0,200}>", "", text)
    return html.unescape(text)


# ── infobox / wikitable 残片 ────────────────────────────────────

_INFOBOX_CUT_RE = re.compile(r"\s*\|label\d*=|\s*\|image\s*=|\s*\|图片信息\s*=|\s*\|\s*text\d*\s*=")
_INFOBOX_PARAM_RE = re.compile(r"(^|\s)\|\s*[^\s=|]{1,15}\s{0,3}=[^=\n]")
_COUNT_SENTENCE_RE = re.compile(
    r"截[止至]现[在时]?\S{0,20}?已有?次观看[^。\n]{0,12}收藏[。；]?"
    r"|已有?次观看[^。\n]{0,12}收藏[。；]?")


def _convert_wikitable(text: str) -> str:
    """把内嵌 wiki 表格转成可读文本行(兼容被截断、无闭合 |} 的表格)。"""
    def table_repl(m):
        body = m.group(0)
        body = re.sub(r"^\s*\{\|[^\n]*", "", body)
        body = re.sub(r"\|\}\s*$", "", body)
        body = re.sub(r"^!.*?\|([^\n]*)$", r"\n【\1】", body, flags=re.M)
        body = re.sub(r'style="[^"]*"', "", body)
        body = body.replace("||", "：")
        body = re.sub(r"^\|\s*", "", body, flags=re.M)
        body = re.sub(r"\n\s*\|-?[^\n]*", "\n", body)
        body = re.sub(r"\n{2,}", "\n", body)
        return "\n" + body.strip() + "\n"
    return re.sub(r"\{\|.*?(?:\|\}|\s*$)", table_repl, text, flags=re.S)


def _clean_introduction_markup(text: str) -> str:
    # 图片链接残骸: File:xxx.jpg|缩略图|右|200px|标题
    text = re.sub(r"File:[^\s|\[\]]*\|", "", text)
    text = re.sub(r"(缩略图|右|左|无框|边框)\|", "", text)
    text = re.sub(r"\d+px\|", "", text)
    # 内嵌 wiki 表格 → 文本行; 未闭合(爬虫截断)的表格整段裁掉
    text = _convert_wikitable(text)
    text = re.sub(r"\s*\{\|.*$", "", text, flags=re.S)
    # 段首残缺的 infobox 模板(爬虫截断): 连同收尾 }} 一起删掉
    m = re.match(r"^\s*\|.*?\}\}", text, re.S)
    if m and "《" not in m.group(0):
        text = text[m.end():]
    text = _expand_all_templates(text)
    text = re.sub(r"\|[^|\n}]{0,30}\}\}", "", text)  # 截断模板尾段 |xxx}}
    text = text.replace("}}", "").replace("{{", "")  # 游离括号
    text = re.sub(r"黑幕\|", "", text)  # 未闭合的 {{黑幕| 残留
    # 段中/段尾的 infobox 残片(|label2=... |image = ...): 其后内容整体裁掉
    m = _INFOBOX_CUT_RE.search(text)
    if m and m.start() > 50:
        text = text[:m.start()]
    # 通用 infobox 参数残片(|key = value 形态, 前面是句号结尾的正文)
    m = _INFOBOX_PARAM_RE.search(text)
    if m and ("。" in text[:m.start()] or m.start() < 80):
        text = text[:m.start()]
    text = _resolve_wikilinks(text)
    text = re.sub(r"-\{([^{}]*)\}-", r"\1", text)  # 简繁转换标记
    text = _resolve_bare_pipes(text)
    text = text.replace("'''", "").replace("''", "")
    # 计数模板删除后遗留的空句子(已有次观看，人收藏。)
    text = _COUNT_SENTENCE_RE.sub("", text)
    text = re.sub(r"\s*\|\s*$", "", text)  # 尾部孤立管道符
    text = re.sub(r"[ \t\u3000]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n", text)
    return text.strip()


# ── 歌词专用: 弹幕画布 / LaTeX 残行过滤 ─────────────────────────

_LATEX_RE = re.compile(
    r"\\frac|\\left|\\right|\\sqrt|\\sum|\\cdot|\\quad|\\pi|\\Delta|\\mu"
    r"|\\sigma|\\geq|\\pm")


def _filter_lyrics_lines(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        if re.match(r"^(@canvas|height=|comment=|setLoop=|[}\s]+$)", s):
            continue
        if LATEX_JUNK := (_LATEX_RE.search(s) and not re.search(r"[\u4e00-\u9fff]", s)):
            continue
        if "<nowiki>" in s or "</nowiki>" in s:
            s = s.replace("<nowiki>", "").replace("</nowiki>", "")
            if _LATEX_RE.search(s) and not re.search(r"[\u4e00-\u9fff]", s):
                continue
        if re.match(r"^(cssa|after|before|width|height|comment|setLoop|[a-z]+\d*)\s*=\S*$", s) \
                and not re.search(r"[\u4e00-\u9fff]", s):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"^\s*'{1,3}\s*$", "", text, flags=re.M)  # 孤立引号行
    text = re.sub(r"^\s*\|o\d*=\s*$", "", text, flags=re.M)  # LyricsKai 参数残留
    text = re.sub(r"」\s*\|\s*「", "」\n「", text)  # 对唱声部分隔
    return text


# ── 对外接口 ────────────────────────────────────────────────────

def clean_introduction(text: str) -> str:
    """清洗简介: 粗体三引号/模板/链接残留/infobox残片/HTML。"""
    if not text:
        return text or ""
    text = _strip_html(text)
    return _clean_introduction_markup(text)


def clean_lyrics(text: str) -> str:
    """清洗歌词: HTML 标签壳删除但歌词正文保留; ref 注释整体删除。"""
    if not text:
        return text or ""
    text = _strip_html(text)
    text = _expand_all_templates(text)
    # 残余未闭合的模板开头({{color|red|... 无闭合), 连同首参数一起删
    text = re.sub(
        r"\{\{\s*(color|crosscolor|交叉颜色|lj|ruby|lang[a-z\-]*|font|span)"
        r"\s*\|[^|\n{}]{0,20}\|", "", text)
    text = re.sub(r"\{\{[^\n{}]{0,40}$", "", text, flags=re.M)  # 孤立模板残头
    text = text.replace("}}", "").replace("{{", "")
    text = _resolve_wikilinks(text)
    text = re.sub(r"\[\[(?![^\[\]]*\]\])", "", text)  # 未闭合的 [[
    text = re.sub(r"-\{([^{}]*)\}-", r"\1", text)  # 简繁转换标记
    text = text.replace("'''", "").replace("''", "")
    text = _filter_lyrics_lines(text)
    text = re.sub(r"(?<!\S)\|(?!\S*\=)", "\n", text)  # 声部分隔管道符
    text = re.sub(r"[ \t\u3000]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_credit(text: str) -> str:
    """清洗创作人员/歌手字段: 去掉 [[ 壳、跨wiki前缀与粗体引号。"""
    if not text:
        return text or ""
    text = text.replace("[[", "").replace("]]", "")
    text = _strip_html(text)
    text = _expand_all_templates(text)
    out = []
    for seg in re.split(r"[、,，/]", text):
        seg = _INTERWIKI_RE.sub("", seg.strip())
        seg = seg.split("#")[0].strip()  # 星尘#Synthesizer_V → 星尘
        seg = seg.replace("'''", "").replace("''", "")
        if seg:
            out.append(seg)
    return "、".join(out)


def clean_display_name(text: str) -> str:
    """清洗歌名(防御性保留, 现库歌名已干净)。"""
    if not text:
        return text or ""
    text = _strip_html(text)
    text = _expand_all_templates(text)
    text = _resolve_wikilinks(text)
    text = text.replace("'''", "").replace("''", "")
    return text.strip()
