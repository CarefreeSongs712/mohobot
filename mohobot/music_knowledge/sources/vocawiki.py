"""Voca.wiki(VOCAWIKI)歌曲来源适配器。

站点为 MediaWiki 1.45, 直接走 ``api.php`` 读 wikitext:
- 列表: ``list=categorymembers``(cmnamespace=0, 按 continue.cmcontinue 翻页)
- 正文: ``prop=revisions&rvslots=main``(支持 titles=A|B|C 批量, 最多 50 个)
- 重定向: ``redirects=1``

许可: CC BY-NC-SA 4.0, 站点地址 https://voca.wiki 。
站点 robots 对 ``*`` 禁止 ``/*action=``; 本适配器只用于本地自用, 但仍按
``interval`` 做来源内限速, 失败按指数退避重试, 不与其他来源共享 sleep。

本模块把 wikitext 解析拆成纯函数(``parse_songbox`` /
``parse_introduction_template`` / ``parse_lyrics_kai`` / ``_strip_wiki_markup``
等), ``VocawikiAdapter`` 只负责 HTTP、限速与组装 ``SourceRecord``。
"""

from __future__ import annotations

import html
import re
import threading
import time
from typing import Any, Iterable, Iterator
from urllib.parse import quote

import requests
from loguru import logger

from mohobot.music_knowledge.normalize import (
    clean_year,
    detect_language,
    extract_bvid,
)
from mohobot.music_knowledge.sources.base import (
    SourceAdapter,
    SourceError,
    SourceRecord,
)

BASE_URL = "https://voca.wiki"
LICENSE = "CC BY-NC-SA 4.0 (https://voca.wiki)"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)

# 默认只覆盖中文歌姬分类 + 全量 VOCALOID 分类。
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "Category:洛天依歌曲",
    "Category:乐正绫歌曲",
    "Category:言和歌曲",
    "Category:星尘歌曲",
    "Category:心华歌曲",
    "Category:诗岸歌曲",
    "Category:使用VOCALOID的歌曲",
)

# include_non_chinese=False 时只遍历这些中文相关分类(全量分类会被跳过)。
_CHINESE_CATEGORY_HINTS: tuple[str, ...] = (
    "洛天依", "乐正绫", "言和", "星尘", "心华", "诗岸",
    "乐正龙牙", "徵羽摩柯", "墨清弦", "赤羽", "海伊", "苍穹",
    "汉语歌曲", "中文歌曲",
)

# STAFF 标签 → SourceRecord 字段。顺序决定同标签多命中时的字段顺序。
_CREDIT_ALIASES: dict[str, tuple[str, ...]] = {
    "lyricist": (
        "作词", "作詞", "词作", "詞作", "填词", "填詞", "作词家", "作詞家",
        "词", "詞",
    ),
    "composer": ("作曲", "曲作", "作曲者", "曲"),
    "arranger": ("编曲", "編曲", "编曲者", "編曲者", "arrange"),
    "mixer": ("混音", "混音师", "混音師", "混音后期", "混音後期", "mix", "mixing"),
    "tuner": ("调教", "調教", "调校", "調校", "调声", "調声", "调", "調", "tuning"),
    "mastering": ("母带", "母帶", "mastering"),
    "illustrator": (
        "曲绘", "曲繪", "插画", "插畫", "插图", "插圖",
        "绘图", "繪圖", "绘", "繪", "illustration",
    ),
    "pv": (
        "PV制作", "PV製作", "PV", "动画", "動畫", "映像",
        "视频制作", "視頻製作", "视频", "視頻", "影片",
        "MV制作", "MV",
    ),
    "singers": ("演唱", "歌唱", "歌手", "演唱者", "歌"),
    "uploader": ("UP主", "P主", "up主", "投稿者", "发布者", "發佈者"),
}

# 行内模板: 只保留正文, 丢弃样式/参数。
_INLINE_TEMPLATES_KEEP_FIRST = {
    "lj", "ja", "jp", "japanese", "日本語", "lang", "lang-ja", "lang-zh",
    "ruby", "rt", "ruby2", "注音", "pt", "photrans",
}
_INLINE_TEMPLATES_KEEP_LAST = {
    "color", "colour", "font", "textcolor", "colored",
    "交叉颜色", "交叉顏色", "荧光笔", "熒光筆", "黑幕", "剧透", "劇透", "隐藏",
}
# 隐藏/悬停类模板: 可见文本在 before, 展开文本在 after
_INLINE_TEMPLATES_KEEP_BEFORE = {"hoverinline", "texthover", "hover", "abbr"}
_INLINE_TEMPLATES_DROP_PREFIX = (
    "lyricskai", "photrans/button", "lyrics",
)
# 纯样式模板(参数只有颜色/尺寸), 直接丢弃。
_INLINE_TEMPLATES_DROP = {"文字描边", "文字顏色", "文字颜色"}


# ── wikitext 基础工具 ──────────────────────────────────────────


def _find_balanced_template(text: str, start: int) -> int:
    """返回从 ``start``(指向 ``{{``)起配对的 ``}}`` 之后的位置; 未闭合返回 -1。"""
    if not text.startswith("{{", start):
        return -1
    depth = 0
    i, n = start, len(text)
    while i < n - 1:
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


def _split_top_level(text: str, sep: str = "|") -> list[str]:
    """按顶层分隔符切分, 忽略 ``{{...}}`` 与 ``[[...]]`` 内部的 ``|``。"""
    parts: list[str] = []
    cur: list[str] = []
    tdepth = ldepth = 0
    i, n = 0, len(text)
    while i < n:
        if text.startswith("{{", i):
            tdepth += 1
            cur.append("{{")
            i += 2
            continue
        if text.startswith("}}", i):
            tdepth = max(0, tdepth - 1)
            cur.append("}}")
            i += 2
            continue
        if text.startswith("[[", i):
            ldepth += 1
            cur.append("[[")
            i += 2
            continue
        if text.startswith("]]", i):
            ldepth = max(0, ldepth - 1)
            cur.append("]]")
            i += 2
            continue
        ch = text[i]
        if ch == sep and tdepth == 0 and ldepth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _template_parts(tpl: str) -> tuple[str, dict[str, str], list[str]]:
    """拆模板为 (模板名, 命名参数, 位置参数)。"""
    inner = tpl[2:-2] if tpl.startswith("{{") and tpl.endswith("}}") else tpl
    parts = _split_top_level(inner)
    name = parts[0].strip()
    named: dict[str, str] = {}
    positional: list[str] = []
    for part in parts[1:]:
        m = re.match(r"^([^=\n]{1,40}?)\s*=\s*(.*)$", part, re.S)
        if m:
            key = m.group(1).strip()
            if key and key not in named:
                named[key] = m.group(2).strip()
        else:
            positional.append(part.strip())
    return name, named, positional


def _render_template(tpl: str) -> str:
    """把行内模板折叠为正文; 未识别的模板一律丢弃(避免模板名残留)。"""
    name, named, pos = _template_parts(tpl)
    low = re.sub(r"\s+", " ", name.lower().replace("_", " "))
    if low in _INLINE_TEMPLATES_DROP or low.startswith(_INLINE_TEMPLATES_DROP_PREFIX):
        return ""
    if low in _INLINE_TEMPLATES_KEEP_FIRST:
        return pos[0] if pos else ""
    if low in _INLINE_TEMPLATES_KEEP_LAST:
        return pos[-1] if pos else ""
    if low in _INLINE_TEMPLATES_KEEP_BEFORE:
        # {{HoverInline|before=＿＿|after=杀死|expand=after}} → ＿＿(悬停可见)
        return named.get("before", "") or (pos[0] if pos else "")
    return ""


def _expand_templates(text: str, rounds: int = 8) -> str:
    """迭代展开行内模板(处理嵌套), 直到不再出现 ``{{``。"""
    for _ in range(rounds):
        if "{{" not in text:
            break
        out: list[str] = []
        i, n = 0, len(text)
        found = False
        while i < n:
            j = text.find("{{", i)
            if j < 0:
                out.append(text[i:])
                break
            out.append(text[i:j])
            end = _find_balanced_template(text, j)
            if end < 0:
                # 未闭合: 原样保留, 交给后续清洗
                out.append(text[j:])
                break
            out.append(_render_template(text[j:end]))
            found = True
            i = end
        new = "".join(out)
        text = new
        if not found:
            break
    return text


def _strip_comments_refs(text: str) -> str:
    """去掉 HTML 注释与 <ref>/<references>。"""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>]*/\s*>", "", text, flags=re.I)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S | re.I)
    text = re.sub(r"<references\s*/>", "", text, flags=re.I)
    return text


def _strip_templates_named(text: str, names: set[str]) -> str:
    """删除顶层指定名字的模板(可含嵌套), 如 {{Cquote|...}}。

    模板名大小写不敏感(站点上 ``{{Cquote}}`` 与 ``{{cquote}}`` 混用)。
    """
    lowered = {n.lower() for n in names}
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        j = text.find("{{", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        end = _find_balanced_template(text, j)
        if end < 0:
            out.append(text[j:])
            break
        inner = text[j + 2:end - 2]
        tpl_name = re.split(r"[|\n]", inner, 1)[0].strip().lower()
        if tpl_name not in lowered:
            out.append(text[j:end])
        i = end
    return "".join(out)


def _link_text(match: re.Match[str]) -> str:
    """``[[a]]`` / ``[[a|b]]`` → a / b, 去掉 vcp: 等跨 wiki 前缀。"""
    target = (match.group(1) or "").strip()
    display = (match.group(2) or "").strip()
    if display:
        return display
    prefix, sep, rest = target.partition(":")
    if sep and prefix.lower() in {"vcp", "w", "wp", "jawp", "qiuwen", "zh", "en"}:
        return rest
    return target


def _strip_wiki_markup(text: str, *, multiline: bool = False) -> str:
    """清洗 wikitext 为纯文本。

    ``multiline=False`` 时把换行/``<br>`` 压成空格(用于字段值);
    ``multiline=True`` 时保留换行(用于歌词)。
    """
    if not text:
        return ""
    text = _strip_comments_refs(text)
    text = re.sub(r"<br\s*/?>", "\n" if multiline else "、", text, flags=re.I)
    text = _expand_templates(text)
    # 文件/分类链接整段丢弃
    text = re.sub(
        r"\[\[(?:File|Image|文件|檔案|档案|图像|分類|分类|Category)\s*:[^\]]*\]\]",
        "", text, flags=re.I,
    )
    # 内链: 优先显示文字; 反复几轮处理少量嵌套
    for _ in range(3):
        text = re.sub(
            r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]", _link_text, text,
        )
    # 外链
    text = re.sub(r"\[(?:https?:)?//[^\s\]]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[(?:https?:)?//[^\]]*\]", "", text)
    text = re.sub(r"'''?", "", text)
    # 常见行内标签
    text = re.sub(
        r"</?(?:span|div|center|small|big|u|b|i|s|sub|sup|font|poem|"
        r"ruby|rt|rb|p|table|tr|td|th|tbody|thead)\b[^>]*>",
        "", text, flags=re.I,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\u3000", " ")
    if multiline:
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
        text = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clean_credit_value(raw: str) -> str:
    """清洗创作人员/歌名字段: ``<br>`` 多人 → 顿号; 去链接/模板/多余空白。"""
    if not raw:
        return ""
    v = re.sub(r"<br\s*/?>", "、", raw, flags=re.I)
    v = _strip_wiki_markup(v, multiline=False)
    v = re.sub(r"\s*[、,，;；]\s*", "、", v)
    v = re.sub(r"、+", "、", v)
    return v.strip(" 、")


def _split_multi_names(value: str) -> list[str]:
    """把多人字段拆成名字列表。"""
    split_re = re.compile(
        r"\s*(?:、|,|，|/|／|&|＆|×|\||;|；|\n|"
        r"\s+(?:feat|ft)\.?\s+)\s*",
        re.I,
    )
    out: list[str] = []
    seen: set[str] = set()
    for part in split_re.split(value or ""):
        name = part.strip().strip(" .·-—_")
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def parse_singers(raw: str) -> str:
    """``[[Ken]]/[[V flower]]、[[洛天依]]`` → ``Ken、V flower、洛天依``。"""
    return "、".join(_split_multi_names(_clean_credit_value(raw)))


# ── Songbox ────────────────────────────────────────────────────

_SONGBOX_RE = re.compile(r"\{\{\s*VOCALOID[ _]+Songbox(?=\s*[|}])", re.I)
_SONGBOX_INTRO_RE = re.compile(
    r"\{\{\s*VOCALOID[ _]+Songbox[ _]+Introduction\b", re.I,
)


def parse_songbox(source: str) -> dict[str, str]:
    """解析第一个 ``{{VOCALOID Songbox ...}}`` 的命名参数。

    一花依世界等页面把 Songbox 嵌在 tabs 里, 这里只按正则找第一个模板
    (通常即中文版), 后续参数按出现顺序 first-wins。
    """
    m = _SONGBOX_RE.search(source or "")
    if not m:
        return {}
    end = _find_balanced_template(source, m.start())
    if end < 0:
        return {}
    _name, named, _pos = _template_parts(source[m.start():end])
    return named


def parse_song_names(raw: str) -> list[str]:
    """把 ``歌曲名称`` 字段拆成多个写法(按 ``<br>`` 分隔)。"""
    names: list[str] = []
    for part in re.split(r"<br\s*/?>", raw or "", flags=re.I):
        name = _strip_wiki_markup(part, multiline=False)
        name = name.strip().strip("《》「」『』\"'“”‘’ ")
        name = re.sub(r"\s+", " ", name)
        if name and name not in names:
            names.append(name)
    return names


def parse_all_song_names(source: str) -> list[str]:
    """收集全部 ``VOCALOID Songbox`` 里的歌名(含 tabs 内的多语言版本)。

    主字段只取第一个 Songbox, 多版本页(如一花依世界的中文版/日文版 tabs)
    的其它名称作为别名。
    """
    names: list[str] = []
    for m in _SONGBOX_RE.finditer(source or ""):
        end = _find_balanced_template(source, m.start())
        if end < 0:
            continue
        _name, named, _pos = _template_parts(source[m.start():end])
        for value in parse_song_names(named.get("歌曲名称", "")):
            if value not in names:
                names.append(value)
    return names


# ── STAFF ──────────────────────────────────────────────────────


def _split_labels(raw: str) -> list[str]:
    """拆复合标签(``作词<br>作曲`` / ``作词・作曲・调声``)。"""
    raw = re.sub(r"<br\s*/?>", "|", raw or "", flags=re.I)
    return [p.strip() for p in re.split(r"[|・·/／、,，&＆\n]+", raw) if p.strip()]


def _credit_keys_for_label(label: str) -> tuple[str, ...]:
    """标签 → SourceRecord 字段名; 复合标签可命中多个字段。"""
    label = _strip_wiki_markup(label or "", multiline=False).strip(" 　*;:")
    if not label or len(label) > 24:
        return ()
    keys: list[str] = []
    # 作编曲 / 作編曲: 用户接受"作曲 + 编曲"各记一份的近似
    if "作编曲" in label or "作編曲" in label:
        keys.extend(["composer", "arranger"])
    low = label.lower()
    for key, aliases in _CREDIT_ALIASES.items():
        for alias in aliases:
            a = alias.lower()
            # 单字别名(如 "歌" / "绘" / "词" / "曲")只做精确匹配, 避免
            # "曲绘" 被当成作曲、"歌曲" 被当成演唱
            if len(a) == 1:
                matched = low == a
            else:
                matched = low == a or low.startswith(a) or a in low
            if matched:
                if key not in keys:
                    keys.append(key)
                break
    return tuple(keys)


def parse_introduction_template(source: str) -> dict[str, str]:
    """解析 ``VOCALOID_Songbox_Introduction``(两种写法)。

    1. ``groupN`` / ``listN`` 成对: group 可能是复合标签
       (``作曲<br>作词<br>調教`` / ``作词・作曲・调声``), 按分隔符拆开后把
       **同一个 list 值**分配给每个标签; list 内的 ``<br>`` 视为多人。
    2. 直接命名参数(``|作曲 = ...`` / ``|词·曲·调 = ...`` /
       ``|混音<br>母带 = ...``): 键名同样按复合标签拆开。
    """
    credits: dict[str, str] = {}
    for m in _SONGBOX_INTRO_RE.finditer(source or ""):
        end = _find_balanced_template(source, m.start())
        if end < 0:
            continue
        _name, named, _pos = _template_parts(source[m.start():end])
        groups: dict[int, str] = {}
        lists: dict[int, str] = {}
        for key, value in named.items():
            mm = re.fullmatch(r"(group|list)(\d+)", key.strip().lower())
            if mm:
                idx = int(mm.group(2))
                (groups if mm.group(1) == "group" else lists)[idx] = value
                continue
            # 直接命名参数: 键本身可能是复合标签
            value_clean = _clean_credit_value(value)
            if not value_clean:
                continue
            for label in _split_labels(key):
                for field in _credit_keys_for_label(label):
                    credits.setdefault(field, value_clean)
        for idx in sorted(set(groups) & set(lists)):
            value = _clean_credit_value(lists[idx])
            if not value:
                continue
            for label in _split_labels(groups[idx]):
                for field in _credit_keys_for_label(label):
                    credits.setdefault(field, value)
    return credits


def _remove_caption_spans(inner: str) -> str:
    """去掉表格值里的小号说明文字(如千本樱 STAFF 表的灰色小字)。"""
    return re.sub(
        r"<span[^>]*(?:font-size\s*:\s*(?:9|10)px|color\s*:\s*#9c9c9c)"
        r"[^>]*>.*?</span>",
        "", inner, flags=re.S | re.I,
    )


def _parse_rowspan(attrs: str) -> int:
    m = re.search(r"rowspan\s*=\s*[\"']?(\d+)", attrs or "", re.I)
    return int(m.group(1)) if m else 1


_CELL_RE = re.compile(r"<t([dh])\b([^>]*)>(.*?)</t\1>", re.S | re.I)
_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)


def parse_staff_table(source: str) -> dict[str, str]:
    """兜底: 从 HTML STAFF 表格(如千本樱)按 th/td 标签→值解析。

    只填已知标签; 支持 rowspan(作词/作曲共用同一个值单元格)。
    """
    credits: dict[str, str] = {}
    for table in _TABLE_RE.findall(source or ""):
        cells = list(_CELL_RE.finditer(table))
        pending_value = ""
        pending_left = 0
        i = 0
        while i < len(cells):
            label_inner = _remove_caption_spans(cells[i].group(3))
            label = _strip_wiki_markup(label_inner, multiline=False).strip()
            keys = _credit_keys_for_label(label)
            if not keys:
                i += 1
                continue

            value = ""
            if i + 1 < len(cells):
                next_inner = _remove_caption_spans(cells[i + 1].group(3))
                next_text = _strip_wiki_markup(next_inner, multiline=False).strip()
                if not _credit_keys_for_label(next_text):
                    value = _clean_credit_value(next_inner)
                    pending_value = value
                    pending_left = max(0, _parse_rowspan(cells[i + 1].group(2)) - 1)
                    i += 2
                else:
                    if pending_left > 0:
                        value = pending_value
                        pending_left -= 1
                    i += 1
            else:
                if pending_left > 0:
                    value = pending_value
                    pending_left -= 1
                i += 1

            if value:
                for key in keys:
                    credits.setdefault(key, value)
    return credits


def parse_staff_section(source: str) -> dict[str, str]:
    """兜底: 解析 ``== STAFF ==`` 章节内 ``*作词：xxx`` 形式的行。"""
    credits: dict[str, str] = {}
    for m in re.finditer(r"^(={2,4})\s*STAFF\s*\1\s*$", source or "", re.M | re.I):
        level = len(m.group(1))
        tail = source[m.end():]
        nxt = re.search(rf"^={{1,{level}}}\s*[^=\n]", tail, re.M)
        block = tail[:nxt.start()] if nxt else tail
        for line in block.splitlines():
            mm = re.match(r"^\s*[*;:]\s*([^：:]{1,16})\s*[：:]\s*(.+)$", line)
            if not mm:
                continue
            value = _clean_credit_value(mm.group(2))
            if not value:
                continue
            for key in _credit_keys_for_label(mm.group(1)):
                credits.setdefault(key, value)
    return credits


# ── 歌词 ──────────────────────────────────────────────────────


def _clean_lyrics_text(text: str) -> str:
    """歌词清洗: 展开模板(颜色/ruby/PT/Photrans), 去引用/注释/残留标签。"""
    if not text:
        return ""
    text = _strip_comments_refs(text)
    text = text.replace("-{", "").replace("}-", "")
    text = _expand_templates(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?poem[^>]*>", "", text, flags=re.I)
    text = re.sub(
        r"\[\[(?:File|Image|文件|檔案|档案|图像)\s*:[^\]]*\]\]",
        "", text, flags=re.I,
    )
    for _ in range(3):
        text = re.sub(
            r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]", _link_text, text,
        )
    text = re.sub(r"\[(?:https?:)?//[^\s\]]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[(?:https?:)?//[^\]]*\]", "", text)
    text = re.sub(r"'''?", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)

    lines: list[str] = []
    for ln in text.splitlines():
        s = ln.replace("\u3000", " ").strip()
        if not s:
            lines.append("")
            continue
        # 参数/样式残留
        if re.match(r"^[A-Za-z_][\w-]*\s*=", s):
            continue
        if re.match(r"^[#;:][\w#(),.% -]+$", s):
            continue
        if s.startswith("*") and re.search(r"翻[译譯]|翻译|譯文", s):
            continue
        if s in {"{{", "}}", "|", "-{", "}-"}:
            continue
        lines.append(s)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _find_section(source: str, keyword: str) -> str:
    """返回标题含 keyword 的章节正文(到下一个同级/更高级标题为止)。"""
    m = re.search(
        rf"^(={{2,4}})\s*[^=\n]*{re.escape(keyword)}[^=\n]*\1\s*$",
        source or "", re.M,
    )
    if not m:
        return ""
    level = len(m.group(1))
    tail = source[m.end():]
    nxt = re.search(rf"^={{1,{level}}}\s*[^=\n]", tail, re.M)
    return tail[:nxt.start()] if nxt else tail


def parse_lyrics_kai(source: str) -> dict[str, str]:
    """解析 ``{{LyricsKai|original=...|translated=...}}``。

    同名模板有多个时(如 ``LyricsKai/Roma/button`` 与 ``LyricsKai/Roma``),
    取含 original/translated 参数的块。部分页面把一个歌词章节拆成多个
    LyricsKai(中间夹 ``<poem>``), 这里按出现顺序合并; 若各块 original 语种
    不一致(中文版/日文版 tabs), 只保留第一块, 避免把两个版本混在一起。
    返回已清洗文本。
    """
    empty = {"original": "", "translated": "", "photrans": ""}
    blocks: list[dict[str, str]] = []
    for m in re.finditer(r"\{\{\s*LyricsKai\b", source or ""):
        end = _find_balanced_template(source, m.start())
        if end < 0:
            continue
        _name, named, _pos = _template_parts(source[m.start():end])
        if not named.get("original") and not named.get("translated"):
            continue
        blocks.append({
            "original": _clean_lyrics_text(named.get("original", "")),
            "translated": _clean_lyrics_text(named.get("translated", "")),
            "photrans": _clean_lyrics_text(named.get("photrans", "")),
        })
    if not blocks:
        return empty
    if len(blocks) > 1:
        langs = {detect_language(b["original"]) for b in blocks if b["original"]}
        langs.discard("unknown")
        if len(langs) > 1:
            blocks = blocks[:1]
    if len(blocks) == 1:
        return blocks[0]
    return {
        "original": "\n\n".join(b["original"] for b in blocks if b["original"]),
        "translated": "\n\n".join(b["translated"] for b in blocks if b["translated"]),
        "photrans": "\n\n".join(b["photrans"] for b in blocks if b["photrans"]),
    }


def parse_poem_lyrics(source: str) -> str:
    """无 LyricsKai 时从 ``<poem>`` 块取歌词(优先「歌词」章节, 取最长块)。"""
    section = _find_section(source, "歌词") or source
    blocks = re.findall(r"<poem[^>]*>(.*?)</poem>", section, re.S | re.I)
    cleaned = [_clean_lyrics_text(b) for b in blocks]
    cleaned = [c for c in cleaned if c]
    return max(cleaned, key=len) if cleaned else ""


def _choose_lyrics(source: str) -> tuple[str, dict[str, str]]:
    """按「original/translated 语种」决定 lyrics 字段, 返回 (lyrics, extra)。

    少数页面(如毒液克星)正文歌词写在 ``<poem>``, 而 LyricsKai 只包了几段
    日文念白; 当 LyricsKai 结果远短于 poem 时改用 poem, 避免歌词被截成残段。
    """
    extra: dict[str, str] = {}
    kai = parse_lyrics_kai(source)
    original = kai.get("original", "")
    translated = kai.get("translated", "")
    poem = parse_poem_lyrics(source)

    if not original and not translated:
        return poem, extra

    if original and translated:
        lang_original = detect_language(original)
        lang_translated = detect_language(translated)
        if lang_original == "ja" and lang_translated == "zh":
            extra["lyrics_original"] = original
            chosen = translated
        elif lang_original == "zh":
            extra["lyrics_translated"] = translated
            chosen = original
        else:
            extra["lyrics_translated"] = translated
            chosen = original
    elif original:
        chosen = original
    else:
        extra["lyrics_translated"] = translated
        chosen = translated

    if poem and len(chosen) < len(poem) * 0.6:
        extra["lyrics_kai"] = chosen
        return poem, extra
    return chosen, extra


# ── 简介 ──────────────────────────────────────────────────────


def parse_introduction_text(source: str, *, max_len: int = 600) -> str:
    """取 Songbox 之后正文首段(``《X》是...`` / ``「X」是...``)。

    先删 ``{{Cquote|...}}``、注释与引用, 再跳过模板/表格/标题行。
    找不到返回空串。
    """
    text = _strip_comments_refs(source or "")
    text = _strip_templates_named(text, {"Cquote"})

    start = 0
    m = _SONGBOX_RE.search(text)
    if m:
        end = _find_balanced_template(text, m.start())
        if end > 0:
            start = end
    tail = text[start:]
    lines = tail.splitlines()

    skip_prefix = (
        "|", "{", "}", "=", "*", ";", ":", "<",
        "[[File:", "[[文件:", "[[檔案:", "[[分类:", "[[分類:", "[[Category:",
    )
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith(skip_prefix):
            continue
        if not (re.match(r"^[《「『]", s) or ("是" in s and len(s) > 6)):
            continue
        para = [s]
        for nxt in lines[i + 1:]:
            t = nxt.strip()
            if not t or t.startswith(skip_prefix) or t.startswith("[[File:"):
                break
            para.append(t)
        cleaned = _strip_wiki_markup(" ".join(para), multiline=False)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # 计数类模板({{bilibiliCount|...}})被丢弃后会留下"已有次观看"等残句
        cleaned = re.sub(r"已有\s*次(?:观看|觀看|播放|收聽|收听)", "", cleaned)
        cleaned = re.sub(r"[，,]\s*人收藏", "", cleaned)
        cleaned = re.sub(r"[，,]?\s*截至现在\s*(?=[。，,])", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ，,")
        if cleaned:
            return cleaned[:max_len]
    return ""


# ── 组装 SourceRecord ─────────────────────────────────────────


def _uploader_from_box(box: dict[str, str]) -> str:
    for key in ("UP主", "up主", "UP", "P主", "p主", "投稿者", "发布者"):
        if box.get(key):
            value = _clean_credit_value(box[key])
            if value:
                return value
    return ""


_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
_YEAR_ONLY_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _year_from_text(value: str) -> int | None:
    """从字段文本里取年份, 优先完整日期, 避免 ``sm19408638`` 这类 ID 干扰。"""
    if not value:
        return None
    m = _DATE_RE.search(value)
    if m:
        return clean_year(m.group(1))
    m = _YEAR_ONLY_RE.search(value)
    if m:
        return clean_year(m.group(1))
    return clean_year(value)


def _year_from_box(box: dict[str, str]) -> int | None:
    for key in (
        "bb_date", "nnd_date", "yt_date",
        "投稿日期", "投稿时间", "投稿時間", "发布日期", "發佈日期",
    ):
        if box.get(key):
            year = _year_from_text(box[key])
            if year:
                return year
    # 投稿 字段常是多个 {{VOCALOID_Songbox/card|...}} 列表, 取第一个日期
    if box.get("投稿"):
        return _year_from_text(box["投稿"])
    return None


def _is_disambiguation(source: str, introduction: str = "") -> bool:
    """消歧义页: 含 ``{{消歧义}}``, 或正文首段以「可以指」开头。"""
    if re.search(r"\{\{\s*消歧义", source or ""):
        return True
    if introduction.strip().startswith("可以指"):
        return True
    # 正文可能因为模板/短句而不被 parse_introduction_text 选中, 这里再兜一层
    text = _strip_comments_refs(source or "")
    for line in text.splitlines():
        s = _strip_wiki_markup(line, multiline=False).strip()
        if not s or s.startswith(("{", "|", "}", "=", "#", "*", ";")):
            continue
        return s.startswith("可以指")
    return False


def parse_record(
    title: str,
    source: str,
    *,
    source_name: str = "vocawiki",
    license_text: str = LICENSE,
    base_url: str = BASE_URL,
) -> SourceRecord:
    """把单个词条的 wikitext 解析为 ``SourceRecord``(纯函数, 便于单测)。"""
    introduction = parse_introduction_text(source)
    if _is_disambiguation(source, introduction):
        raise SourceError("disambiguation")

    box = parse_songbox(source)
    all_names = parse_all_song_names(source)
    names = parse_song_names(box.get("歌曲名称", "")) or all_names
    name = names[0] if names else (title or "").strip()

    lyrics, extra = _choose_lyrics(source)

    # 专辑/列表页也可能落在歌曲分类里: 既无 Songbox 又无歌词时不当作歌曲
    if not box and not lyrics:
        raise SourceError("not a song page")

    # STAFF: Introduction 模板优先, 表格与 STAFF 章节只补缺
    credits: dict[str, str] = {}
    for block in (
        parse_introduction_template(source),
        parse_staff_table(source),
        parse_staff_section(source),
    ):
        for key, value in block.items():
            if value and key not in credits:
                credits[key] = value

    singers = parse_singers(box.get("演唱", "") or box.get("歌手", ""))
    if not singers:
        singers = parse_singers(credits.pop("singers", ""))
    else:
        credits.pop("singers", None)

    uploader = _uploader_from_box(box) or credits.pop("uploader", "")

    extra["lang"] = detect_language(lyrics or name)

    aliases: list[str] = []
    for alias in all_names + [title.strip()]:
        if alias and alias != name and alias not in aliases:
            aliases.append(alias)

    record = SourceRecord(
        source=source_name,
        source_title=title,
        source_url=f"{base_url.rstrip('/')}/{quote(title.replace(' ', '_'), safe='/')}",
        license=license_text,
        name=name,
        singers=singers,
        uploader=uploader,
        lyricist=credits.get("lyricist", ""),
        composer=credits.get("composer", ""),
        arranger=credits.get("arranger", ""),
        mixer=credits.get("mixer", ""),
        tuner=credits.get("tuner", ""),
        mastering=credits.get("mastering", ""),
        pv=credits.get("pv", ""),
        illustrator=credits.get("illustrator", ""),
        year=_year_from_box(box),
        introduction=introduction,
        lyrics=lyrics,
        bvid=extract_bvid(
            box.get("bb_id", ""),
            box.get("bb_url", ""),
            box.get("bilibili", ""),
            box.get("投稿", ""),
        ),
        aliases=aliases,
        extra=extra,
    )
    return record


# ── 网络适配器 ────────────────────────────────────────────────


def _is_chinese_category(category: str) -> bool:
    low = (category or "").lower()
    return any(hint.lower() in low for hint in _CHINESE_CATEGORY_HINTS)


class VocawikiAdapter(SourceAdapter):
    """Voca.wiki 适配器(api.php + wikitext 解析)。"""

    name = "vocawiki"
    license = LICENSE
    default_interval = 0.4
    default_concurrency = 3

    def __init__(self, config: dict | None = None):
        cfg = dict(config or {})
        self.config = cfg
        self.base_url = str(cfg.get("base_url") or BASE_URL).rstrip("/")
        self.timeout = float(cfg.get("timeout", 30))

        raw_categories = cfg.get("categories")
        if isinstance(raw_categories, str):
            raw_categories = [raw_categories]
        categories = [
            str(c).strip() for c in (raw_categories or DEFAULT_CATEGORIES)
            if str(c).strip()
        ]
        self.categories = categories or list(DEFAULT_CATEGORIES)
        self.max_pages_per_category = int(cfg.get("max_pages_per_category", 50))
        self.include_non_chinese = bool(cfg.get("include_non_chinese", True))
        self.interval = float(cfg.get("interval", self.default_interval))
        self.max_retries = int(cfg.get("max_retries", 3))

        # session 可注入(测试用); 注入的 session 不修改、不关闭
        session = cfg.get("session")
        self._owns_session = session is None
        if session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
        self.session: requests.Session = session

        # 来源内限速: 共享一把锁, 保证同一适配器的请求间隔 >= interval
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0

    # ── 限速/请求 ─────────────────────────────────────────────

    def _throttle(self) -> None:
        """单调时钟节流; 只影响当前适配器实例。"""
        if self.interval <= 0:
            return
        with self._throttle_lock:
            now = time.monotonic()
            wait = self.interval - (now - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _api_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """GET api.php, 对 429/5xx/超时/连接错误按指数退避重试。"""
        url = f"{self.base_url}/api.php"
        attempts = max(1, self.max_retries + 1)
        last_error = ""
        for attempt in range(attempts):
            self._throttle()
            retry_after = ""
            try:
                resp = self.session.get(
                    url, params=params, timeout=self.timeout,
                    headers={"Accept": "application/json"},
                )
            except requests.Timeout:
                last_error = "timeout"
            except requests.ConnectionError as exc:
                last_error = f"connection: {exc}"
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        last_error = f"invalid json: {exc}"
                    else:
                        if isinstance(data, dict):
                            return data
                        last_error = "invalid json payload"
                elif resp.status_code == 429 or 500 <= resp.status_code < 600:
                    last_error = f"HTTP {resp.status_code}"
                    header = resp.headers.get("Retry-After") or ""
                    if header.isdigit():
                        retry_after = header
                else:
                    # 4xx(非 429)不重试
                    raise SourceError(f"HTTP {resp.status_code}: {url}")

            if attempt < attempts - 1:
                delay = min(30.0, 0.8 * (2 ** attempt))
                if retry_after:
                    # 站点明确要求等待时, 以 Retry-After 为准(同一来源内不叠加)
                    delay = min(60.0, float(retry_after))
                logger.warning(
                    f"Vocawiki: 请求失败({last_error}), "
                    f"{delay:.1f}s 后重试 ({attempt + 1}/{attempts - 1})"
                )
                time.sleep(delay)
        raise SourceError(f"请求失败: {last_error or 'unknown'}")

    # ── 列表 ─────────────────────────────────────────────────

    def list_titles(self, limit: int | None = None) -> Iterator[str]:
        """遍历配置分类并去重; 达到 limit 立即停止。"""
        if limit is not None and limit <= 0:
            return
        seen: set[str] = set()
        count = 0
        for category in self.categories:
            if not self.include_non_chinese and not _is_chinese_category(category):
                logger.debug(f"Vocawiki: include_non_chinese=False, 跳过 {category}")
                continue
            cmcontinue: str | None = None
            pages = 0
            while pages < self.max_pages_per_category:
                params: dict[str, Any] = {
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": category,
                    "cmlimit": "500",
                    "cmnamespace": "0",
                    "format": "json",
                }
                if cmcontinue:
                    params["cmcontinue"] = cmcontinue
                try:
                    data = self._api_get(params)
                except SourceError as exc:
                    logger.warning(f"Vocawiki: 分类 {category} 拉取失败: {exc}")
                    break
                members = (
                    (data.get("query") or {}).get("categorymembers") or []
                )
                for member in members:
                    title = (member.get("title") or "").strip()
                    if not title or title in seen:
                        continue
                    seen.add(title)
                    yield title
                    count += 1
                    if limit is not None and count >= limit:
                        return
                pages += 1
                cmcontinue = (data.get("continue") or {}).get("cmcontinue")
                if not cmcontinue:
                    break

    # ── 单页/批量抓取 ─────────────────────────────────────────

    def _query_wikitexts(self, titles: list[str]) -> dict[str, str]:
        """批量取 wikitext, 返回 {请求标题: 正文}。

        MediaWiki 的 ``titles`` 以 ``|`` 分隔, 因此标题里的 ``|`` 无法表达
        (MediaWiki 标题本身也不允许该字符) —— 这类标题直接跳过并报错。
        其余特殊字符(空格 / # / & / + / % / 中文)全部交给 requests 的
        params 做 URL 编码, 不手工拼 URL。
        """
        if not titles:
            return {}
        invalid = [t for t in titles if "|" in t]
        if invalid:
            logger.warning(f"Vocawiki: 标题含 '|', 无法通过 API 查询: {invalid}")
            titles = [t for t in titles if "|" not in t]
            if not titles:
                raise SourceError("invalid title: '|'")
        # MediaWiki 单次 titles 上限 50 个; 超出时拆批合并(仍受同一节流约束)
        if len(titles) > 50:
            merged: dict[str, str] = {}
            for offset in range(0, len(titles), 50):
                merged.update(self._query_wikitexts(titles[offset:offset + 50]))
            return merged
        params = {
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
            "redirects": "1",
            "titles": "|".join(titles),
        }
        data = self._api_get(params)
        query = data.get("query") or {}
        pages = query.get("pages") or {}

        normalized: dict[str, str] = {}
        for item in query.get("normalized") or []:
            if item.get("from") and item.get("to"):
                normalized[item["from"]] = item["to"]
        redirects: dict[str, str] = {}
        for item in query.get("redirects") or []:
            if item.get("from") and item.get("to"):
                redirects[item["from"]] = item["to"]

        by_title: dict[str, dict[str, Any]] = {}
        for page in pages.values():
            page_title = page.get("title") or ""
            if page_title:
                by_title[page_title] = page

        result: dict[str, str] = {}
        for title in titles:
            current = normalized.get(title, title)
            seen: set[str] = set()
            while current in redirects and current not in seen:
                seen.add(current)
                current = redirects[current]
            page = by_title.get(current) or by_title.get(title)
            if not page:
                continue
            if "missing" in page or "invalid" in page:
                continue
            revisions = page.get("revisions") or []
            if not revisions:
                continue
            slot = (revisions[0].get("slots") or {}).get("main") or {}
            content = slot.get("*")
            if content is None:
                content = slot.get("content") or ""
            if content:
                result[title] = content
        return result

    def _accept(self, record: SourceRecord) -> None:
        """include_non_chinese=False 时拒绝非中文原文歌曲。"""
        if self.include_non_chinese:
            return
        original = (
            record.extra.get("lyrics_original")
            or record.lyrics
            or record.name
        )
        if detect_language(original) not in ("zh", "unknown"):
            raise SourceError("non-chinese")

    def fetch(self, title: str) -> SourceRecord:
        """抓取并解析单个词条。"""
        title = (title or "").strip()
        if not title:
            raise SourceError("empty title")
        logger.debug(f"Vocawiki: 抓取 {title}")
        contents = self._query_wikitexts([title])
        source = contents.get(title)
        if not source:
            raise SourceError(f"missing page: {title}")
        try:
            record = parse_record(
                title, source,
                source_name=self.name,
                license_text=self.license,
                base_url=self.base_url,
            )
        except SourceError:
            raise
        except Exception as exc:  # 解析异常统一收敛为来源错误
            raise SourceError(f"parse failed: {title}: {exc}") from exc
        self._accept(record)
        return record

    def fetch_many(self, titles: Iterable[str], *, chunk_size: int = 50) -> dict[str, SourceRecord]:
        """批量抓取(每批最多 50 个标题); 单页失败只记日志, 不出现在结果中。"""
        unique: list[str] = []
        seen: set[str] = set()
        for raw in titles:
            title = (raw or "").strip()
            if title and title not in seen:
                seen.add(title)
                unique.append(title)

        records: dict[str, SourceRecord] = {}
        for offset in range(0, len(unique), max(1, chunk_size)):
            chunk = unique[offset:offset + max(1, chunk_size)]
            try:
                contents = self._query_wikitexts(chunk)
            except SourceError as exc:
                logger.warning(f"Vocawiki: 批量抓取失败 {chunk[:3]}...: {exc}")
                continue
            for title in chunk:
                source = contents.get(title)
                if not source:
                    logger.warning(f"Vocawiki: 页面缺失 {title}")
                    continue
                try:
                    record = parse_record(
                        title, source,
                        source_name=self.name,
                        license_text=self.license,
                        base_url=self.base_url,
                    )
                    self._accept(record)
                except SourceError as exc:
                    logger.warning(f"Vocawiki: 解析失败 {title}: {exc}")
                    continue
                records[title] = record
        return records

    def close(self) -> None:
        """只关闭自己创建的 session(注入的 session 由调用方负责)。"""
        if self._owns_session:
            try:
                self.session.close()
            except Exception:
                pass


__all__ = [
    "VocawikiAdapter",
    "parse_songbox",
    "parse_song_names",
    "parse_all_song_names",
    "parse_introduction_template",
    "parse_staff_table",
    "parse_staff_section",
    "parse_lyrics_kai",
    "parse_poem_lyrics",
    "parse_introduction_text",
    "parse_singers",
    "parse_record",
]
