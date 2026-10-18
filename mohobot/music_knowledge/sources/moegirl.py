"""萌娘百科(mzh.moegirl.org.cn)歌曲来源适配器。

站点事实(2026-10 实测):
- 桌面 UA 一律 403, 必须使用移动 UA。
- MediaWiki API 只剩 `prop=extracts` 与 `opensearch` 可用; `extracts` 的
  「歌词」章节是空的(JS 渲染), 所以歌词必须抓渲染后的 HTML。
- 分类枚举: `Category:<名称>` 每页 200 条, 用 `?from=<上一页最后一个标题>`
  翻页(不能用 `?pagefrom=`, robots 禁止且会跳登录页)。
- 词条页开头 `p.vc-preview` 是最可靠的歌名/UP主/年份/演唱来源;
  STAFF 基本不在信息框里, 而在正文表格中; 歌词在 `<h2 id="歌词">` 之后。
- 限速实测: 5s 间隔 0% 403, 并发 1 最稳; 默认 4.5s, 403 时退避 15-30s。

许可: CC BY-NC-SA 3.0 CN。AI 使用政策见 https://mzh.moegirl.org.cn/llms.txt
本适配器只负责抓取与解析, 不写数据库; 调用方必须遵守站点许可与限速要求。
"""

from __future__ import annotations

import html as _html
import json
import random
import re
import threading
import time as _time
from typing import Any, Iterable
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag
from loguru import logger

from mohobot.music_knowledge.normalize import (
    bracket_core,
    clean_year,
    detect_language,
    extract_bvid,
)
from mohobot.music_knowledge.sources.base import (
    SourceAdapter,
    SourceError,
    SourceRecord,
)

# ── 常量 ──────────────────────────────────────────────────────

BASE_URL = "https://mzh.moegirl.org.cn"
# 桌面 UA 一律 403, 只能用移动 UA。
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
)

MOEGIRL_LICENSE = (
    "CC BY-NC-SA 3.0 CN (https://creativecommons.org/licenses/by-nc-sa/3.0/cn/); "
    "来源: 萌娘百科 https://mzh.moegirl.org.cn; "
    "AI 使用政策: https://mzh.moegirl.org.cn/llms.txt"
)

# 12 个中文歌姬分类 + 初音未来, 共 13 个。
DEFAULT_CATEGORIES = [
    "洛天依歌曲",
    "乐正绫歌曲",
    "言和歌曲",
    "星尘歌曲",
    "心华歌曲",
    "赤羽歌曲",
    "海伊歌曲",
    "诗岸歌曲",
    "苍穹歌曲",
    "乐正龙牙歌曲",
    "徵羽摩柯歌曲",
    "墨清弦歌曲",
    "初音未来歌曲",
]

# 反爬/登录页标记。注意: 正常页面 RLCONF 里也会有 "wgMoeAuthConfig",
# 所以 MoeAuth 只认页面级标记(标题/id/class), 见 _is_challenge_page。
_CHALLENGE_STRONG_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "challenge-form",
    "cf_chl_opt",
    "<title>just a moment",
)

# 解析时一律跳过的标签(脚本/样式/注音/引用角标/参考文献)。
_SKIP_TAGS = {"script", "style", "noscript", "sup", "rt", "rp"}

_NAME_RE = re.compile(r"《\s*(.+?)\s*》\s*是")
_SINGER_RE = re.compile(r"[，,]\s*(?:由\s*)?(.+?)\s*演唱的歌曲")
_SINGER_ALT_RE = re.compile(r"由\s*(.+?)\s*演唱")
_UPLOADER_RE = re.compile(r"是\s*(.+?)\s*于\s*((?:19|20)\d{2})\s*年")
_UPLOADER_ALT_RE = re.compile(r"是\s*(?:由\s*)?(.+?)\s*投稿")
_REF_MARK_RE = re.compile(r"\[\s*\d{1,3}\s*\]")
_CATEGORY_TOTAL_RE = re.compile(r"共\s*([\d,]+)\s*个页面")

# `<br>` 的临时占位符: 先与源码中的换行合并, 再统一转成换行,
# 避免「<br />\n」被当成两个换行(每行之间出现空行)。
_BR_MARK = "\x00"

# 简介中必须剔除的成绩话术关键词(命中整句丢弃)。
_ACHIEVEMENT_KEYWORDS = (
    "殿堂曲",
    "传说曲",
    "神话曲",
    "殿堂用时",
    "再生数",
    "收藏",
    "硬币",
    "周刊",
    "门番",
    "截至现在",
    "截至目前",
    "播放量",
    "百万用时",
    "二次创作",
    "同系列",
    "成就",
    "达成",
)

# STAFF 键别名。按顺序匹配, 命中即取。
_CREDIT_ALIASES: dict[str, tuple[str, ...]] = {
    "lyricist": ("作词", "作詞", "词作", "詞作", "填词", "填詞"),
    "composer": ("作曲", "曲作", "作曲者"),
    "arranger": ("编曲", "編曲", "编曲者"),
    "mixer": ("混音", "混音师", "混音后期"),
    "tuner": ("调教", "調教", "调校", "調校", "调声", "调聲"),
    "mastering": ("母带", "母帶", "母带处理", "母带制作"),
    "pv": ("PV", "视频制作", "映像", "影片", "动画", "MV"),
    "illustrator": ("曲绘", "曲繪", "插画", "插畫", "绘图", "立绘", "立繪"),
    "uploader": ("UP主", "投稿者", "发布者"),
    "singers": ("演唱", "演唱者", "歌手", "主唱"),
}


# ── 通用文本清洗 ───────────────────────────────────────────────


def _node_text(node: Any, *, br: str = "\n") -> str:
    """按文档顺序取节点文本; `<br>` 转成分隔符, 跳过脚本/注音/引用角标。

    不用 BeautifulSoup.get_text(): 移动版部分节点(如 vc-preview)在
    bs4 4.14 下 get_text() 会异常返回空串, 逐节点遍历更稳定。
    """
    parts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, Comment):
            return
        if isinstance(n, NavigableString):
            parts.append(str(n))
            return
        name = getattr(n, "name", None)
        if name in _SKIP_TAGS:
            return
        if name == "br":
            parts.append(br)
            return
        for child in getattr(n, "children", ()):
            walk(child)

    walk(node)
    return "".join(parts)


def _clean_text(text: Any) -> str:
    """清理 wikitext/HTML 残留, 折叠空白(单行字段用)。

    规则参考 vcpedia 的清洗风格, 但不依赖其私有函数。
    """
    if not text:
        return ""
    s = _html.unescape(str(text))
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\[\[([^\]|]*)\|?[^\]]*\]\]", r"\1", s)       # [[a|b]] → a
    s = re.sub(r"\[(?:https?://)?[^\s\]]+\s([^\]]*)\]", r"\1", s)  # [url 文字] → 文字
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = _REF_MARK_RE.sub("", s)
    # &#12288;(全角空格) / &#8195;(em space) / &#8194; 等统一成普通空格
    s = re.sub(r"[\u2000-\u200a\u202f\u205f]", " ", s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"\s+", " ", s)
    # `value1 <br /> value2` 这类多值分隔符两侧不留空格
    s = re.sub(r"\s*、\s*", "、", s).strip()
    return s.strip("|").strip()


def _clean_lyrics_text(text: Any) -> str:
    """歌词专用清洗: 保留换行/分段, 去实体、引用角标与行内多余空白。"""
    if not text:
        return ""
    s = _html.unescape(str(text))
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S | re.I)
    # `<br />\n` 只算一个换行; 连续两个 <br>(中间只有空白)才算空行。
    s = re.sub(_BR_MARK + r"[ \t]*\n?", "\n", s)
    s = s.replace(_BR_MARK, "\n")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _REF_MARK_RE.sub("", s)
    s = re.sub(r"[\u2000-\u200a\u202f\u205f]", " ", s)
    s = s.replace("\u3000", " ").replace("\xa0", " ").replace("\u200b", "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1] != "":
            out.append("")  # 最多保留一个空行作为分段
    while out and not out[-1]:
        out.pop()
    return "\n".join(out).strip()


def _strip_achievement_lines(text: str) -> str:
    """剔除简介里的成绩话术句(殿堂/传说/收藏/周刊/播放量等)。

    按行 + 中文句末标点(。！？)切句, 命中的整句丢弃, 未命中的原样保留。
    只认全角句末标点, 避免把「SUPER HIT!!」里的半角 `!` 当句界切碎。
    """
    if not text:
        return ""
    kept_lines: list[str] = []
    for line in str(text).splitlines():
        segments = re.split(r"(?<=[。！？])", line)
        kept: list[str] = []
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            if any(k in seg for k in _ACHIEVEMENT_KEYWORDS):
                continue
            # 被过滤句子的引导语(如「本曲还创造了以下记录：」)一并丢弃
            if seg.endswith(("：", ":")) and "以下" in seg:
                continue
            kept.append(seg)
        if kept:
            kept_lines.append("".join(kept))
    return "\n".join(kept_lines).strip()


# ── 分类页解析 ─────────────────────────────────────────────────


def parse_category_titles(html: str) -> list[str]:
    """从分类页 HTML 解析页面标题(按出现顺序去重)。

    结构: `<div class="mw-category ...">` 下 `<li><a title="...">`。
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div.mw-category") or soup.select_one("#mw-pages")
    if root is None:
        return []
    titles: list[str] = []
    seen: set[str] = set()
    for a in root.select("li a[title]"):
        title = _clean_text(a.get("title") or a.get_text(" ", strip=True))
        if not title or title in seen:
            continue
        # 分类页里偶有 Category:/File: 等非条目链接, 直接跳过。
        if ":" in title:
            ns = title.split(":", 1)[0].strip().lower()
            if ns in {"category", "file", "image", "template", "special", "help", "user"}:
                continue
        seen.add(title)
        titles.append(title)
    return titles


_NEXT_PAGE_RE = re.compile(
    r'pagefrom=([^&"#]+)[^"]*"[^>]*>\s*下一页\s*<'
)


def parse_next_pagefrom(html: str) -> str:
    """解析分类页「下一页」链接里的 pagefrom 游标; 无下一页返回空串。

    实测(2026-10): 萌百分类页的 `?from=` 是**按排序键包含式**的游标,
    直接传"本页最后一个标题"会原地打转(排序键与标题不同, 例如中文标题按
    拼音排序), 导致第 2 页之后拿不到数据、分类被误判为提前结束
    (洛天依分类标称 3524, 旧实现只枚举到 2484)。
    正确做法是沿用站点自己生成的 pagefrom 游标, 它指向严格大于本页末项的
    排序键, 连续翻页无重复。
    """
    if not html:
        return ""
    match = _NEXT_PAGE_RE.search(html)
    if not match:
        return ""
    from urllib.parse import unquote

    return unquote(match.group(1)).strip()


def parse_category_total(html: str) -> int | None:
    """从分类页解析「共 N 个页面」中的 N。"""
    if not html:
        return None
    m = _CATEGORY_TOTAL_RE.search(html)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


# ── 页面结构定位 ───────────────────────────────────────────────


def _heading_level(tag: Tag) -> int:
    name = getattr(tag, "name", "") or ""
    if len(name) == 2 and name[0] == "h" and name[1].isdigit():
        return int(name[1])
    return 0


def _sibling_heading_level(node: Tag) -> int:
    """判断同级节点是否是标题(兼容 `<div class="mw-heading">` 包裹)。"""
    level = _heading_level(node)
    if level:
        return level
    if node.name == "div" and any(
        str(c).startswith("mw-heading") for c in (node.get("class") or [])
    ):
        inner = node.find(re.compile(r"^h[1-6]$"))
        if inner is not None:
            return _heading_level(inner)
    return 0


def _section_wrap(heading: Tag) -> Tag:
    parent = heading.parent
    if isinstance(parent, Tag) and any(
        str(c).startswith("mw-heading") for c in (parent.get("class") or [])
    ):
        return parent
    return heading


def _iter_section_siblings(heading: Tag) -> Iterable[Tag]:
    """迭代标题之后、下一个同级(或更高级)标题之前的顶层节点。"""
    level = _heading_level(heading)
    wrap = _section_wrap(heading)
    for sib in wrap.next_siblings:
        if not isinstance(sib, Tag):
            continue
        sib_level = _sibling_heading_level(sib)
        if sib_level and sib_level <= level:
            break
        yield sib


def _find_heading(soup: BeautifulSoup, keyword: str) -> Tag | None:
    """找第一个文本包含 keyword 的 h2-h4(命中「歌词及人设」这类标题)。"""
    for tag in soup.find_all(re.compile(r"^h[2-4]$")):
        text = _clean_text(_node_text(tag, br=" "))
        if keyword in text:
            return tag
    return None


def _find_content(soup: BeautifulSoup) -> Tag | None:
    """正文主体 `div.mw-parser-output`, 回退 `#mw-content-text`。"""
    return soup.select_one("div.mw-parser-output") or soup.select_one("#mw-content-text")


def _page_title(soup: BeautifulSoup, fallback: str) -> str:
    """页面标题: h1#firstHeading → 其它标题节点 → <title> → 调用方标题。"""
    node = (
        soup.select_one("h1#firstHeading")
        or soup.select_one(".mw-page-title-main")
        or soup.select_one("h1")
    )
    if node is not None:
        text = _clean_text(_node_text(node, br=" "))
        if text:
            return text
    if soup.title is not None:
        text = _clean_text(_node_text(soup.title, br=" "))
        text = re.sub(r"\s*-\s*萌娘百科.*$", "", text).strip()
        if text:
            return text
    return _clean_text(fallback)


def _page_categories(html: str) -> list[str]:
    """从 RLCONF 的 wgCategories 读页面分类(用于消歧义判断与附加信息)。"""
    m = re.search(r'"wgCategories"\s*:\s*(\[[^\]]*\])', html or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _is_challenge_page(html: str, status: int) -> bool:
    """判断是否 Cloudflare / MoeAuth 等反爬或登录挑战页。

    正常页面的 RLCONF 也含 `wgMoeAuthConfig`, 所以 MoeAuth 只认页面级标记
    (标题/id/class), 不认这段配置。
    """
    if status in (401, 403, 429, 503):
        return True
    if not html:
        return True
    head = html[:80000]
    low = head.lower()
    if any(mark in low for mark in _CHALLENGE_STRONG_MARKERS):
        return True
    if re.search(r"<title[^>]*>[^<]*moeauth", low) or re.search(
        r"(?:id|class)\s*=\s*[\"'][^\"']*moeauth", low
    ):
        return True
    if "mw-parser-output" in low:
        # 有正文主体: 不因 MoeAuth 配置或页脚登录链接误判。
        return False
    if "登录" in head or "login" in low:
        return True
    return False


# ── STAFF 表解析 ───────────────────────────────────────────────


def _credit_keys(label: str) -> list[str]:
    """把表格标签映射为标准字段; 「作编曲」「混音母带」拆成两个字段。"""
    raw = _clean_text(label)
    if not raw:
        return []
    keys: list[str] = []
    # 复合键优先: 作编曲 → composer + arranger; 混音母带 → mixer + mastering
    if "作编曲" in raw or ("作曲" in raw and "编曲" in raw):
        keys.extend(("composer", "arranger"))
    if "混音" in raw and "母带" in raw:
        keys.extend(("mixer", "mastering"))
    for key, aliases in _CREDIT_ALIASES.items():
        if key in keys:
            continue
        if any(alias in raw for alias in aliases):
            keys.append(key)
    # 去重保序
    return list(dict.fromkeys(keys))


def _staff_like(table: Tag) -> bool:
    """粗筛 STAFF 表: 至少两个可识别键, 排除信息框/导航框/超大表。"""
    classes = " ".join(str(c) for c in (table.get("class") or []))
    if "navbox" in classes or "moe-infobox" in classes or "infobox" in classes:
        return False
    rows = table.find_all("tr")
    if not (2 <= len(rows) <= 80):
        return False
    hits = 0
    checked = 0
    for tr in rows[:40]:
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        checked += 1
        if _credit_keys(_node_text(cells[0], br=" ")):
            hits += 1
        if hits >= 2:
            return True
    return checked >= 2 and hits >= 2


def parse_staff_table(table: Tag | Iterable[Tag]) -> dict[str, str]:
    """从 STAFF 表格解析创作人员。

    支持单元格成对(左标签右值)与一行多对; 同一字段只取第一次出现的值。
    `table` 可以是单个 Tag, 也可以是 Tag 列表(按顺序合并)。
    """
    tables = [table] if isinstance(table, Tag) else list(table or [])
    credits: dict[str, str] = {}
    for tbl in tables:
        if not isinstance(tbl, Tag):
            continue
        for tr in tbl.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                continue
            if len(cells) % 2 == 0:
                pairs = [(cells[i], cells[i + 1]) for i in range(0, len(cells), 2)]
            else:
                pairs = [(cells[0], cells[1])]
            for label_cell, value_cell in pairs:
                label = _clean_text(_node_text(label_cell, br=" "))
                value = _clean_text(_node_text(value_cell, br="、"))
                if not label or not value:
                    continue
                for key in _credit_keys(label):
                    if key and key not in credits:
                        credits[key] = value
    return credits


# 这些章节里的表格是衍生版本/翻唱 STAFF, 不能当作本曲主 STAFF。
_DERIVATIVE_SECTION_KEYWORDS = ("二次创作", "同系列", "翻唱", "衍生", "其他版本")


def _in_derivative_section(table: Tag) -> bool:
    """表格是否位于「二次创作/同系列/翻唱」等衍生章节内。"""
    for prev in table.find_all_previous(["h2", "h3", "h4"]):
        text = _clean_text(_node_text(prev, br=" "))
        if not text:
            continue
        if any(k in text for k in _DERIVATIVE_SECTION_KEYWORDS):
            return True
        # 只看到最近的 h2 为止(同级章节边界)
        if prev.name == "h2":
            return False
    return False


def _collect_staff(soup: BeautifulSoup, lyrics_heading: Tag | None) -> dict[str, str]:
    """在歌词标题附近找 STAFF 表。

    实测 STAFF 表有时在歌词标题之前(一花依世界/九九八十一), 有时紧跟在
    标题之后(万神纪/权御天下/逆浪千秋)。策略:
    1. 先看歌词章节内部(标题之后、下一个 h2 之前);
    2. 否则按文档序找全页第一个 STAFF 表, 跳过信息框/导航框与
       「二次创作/同系列/翻唱」等衍生章节里的表格。
    """
    after: list[Tag] = []
    if lyrics_heading is not None:
        seen_ids: set[int] = set()

        def add_after(node: Tag) -> None:
            candidates = [node] if node.name == "table" else node.find_all("table")
            for tbl in candidates:
                if id(tbl) in seen_ids:
                    continue
                seen_ids.add(id(tbl))
                if _staff_like(tbl):
                    after.append(tbl)

        for node in _iter_section_siblings(lyrics_heading):
            add_after(node)

    for tbl in after:
        credits = parse_staff_table(tbl)
        if len(credits) >= 2:
            return credits

    content = _find_content(soup)
    if content is None:
        return {}
    for tbl in content.find_all("table"):
        if not _staff_like(tbl) or _in_derivative_section(tbl):
            continue
        credits = parse_staff_table(tbl)
        if len(credits) >= 2:
            return credits
    return {}


# ── 歌词解析 ───────────────────────────────────────────────────


def _lyrics_lines(container: Tag, selector: str) -> str:
    """取 `.Lyrics-original` / `.Lyrics-translated` 的所有行(保留空行)。"""
    lines: list[str] = []
    line_nodes = container.select(".Lyrics-line")
    if line_nodes:
        for line in line_nodes:
            cell = line.select_one(selector)
            lines.append(_clean_lyrics_text(_node_text(cell, br=" ")) if cell else "")
    else:
        cell = container.select_one(selector)
        if cell is not None:
            lines.append(_clean_lyrics_text(_node_text(cell, br="\n")))
    # 去掉首尾空行, 中间最多保留一个空行
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out).strip()


def _poem_label(poem: Tag, lyrics_heading: Tag | None) -> str:
    """取歌词段落前的标题/说明文字(如「完整版」「短版」「洛天依」)。

    只接受歌词章节内部的说明: 反向遍历到歌词标题就停止, 避免把上一章节的
    「所用时间」「达成时间」当成段落标签。
    """
    tab = poem.find_parent("div", class_="Tab")
    if isinstance(tab, Tag):
        label = tab.select_one(".TabLabelText")
        if label is not None:
            text = _clean_text(_node_text(label, br=" "))
            if text:
                return text

    stop_node: Tag | None = None
    if lyrics_heading is not None:
        stop_node = _section_wrap(lyrics_heading)

    fallback = ""
    for prev in poem.previous_elements:
        if not isinstance(prev, Tag):
            continue
        if stop_node is not None and prev is stop_node:
            break
        if prev.name in ("h2", "h3", "h4", "h5"):
            text = _clean_text(_node_text(prev, br=" "))
            if not text or "歌词" in text:
                break
            return text if len(text) <= 24 else ""
        if not fallback and prev.name in ("p", "dt"):
            text = _clean_text(_node_text(prev, br=" "))
            if (
                text
                and len(text) <= 24
                and not text.endswith(("：", ":"))
                and not any(k in text for k in ("所用时间", "达成时间", "投稿时间", "著作权"))
            ):
                fallback = text
    return fallback


def parse_lyrics_section(
    soup: BeautifulSoup,
    heading: Tag | None = None,
) -> dict[str, Any]:
    """解析歌词章节。

    返回:
      lyrics        主歌词(中文优先)
      language      主歌词语种(detect_language)
      source        "Lyrics" / "poem" / ""
      original      原文(仅 Lyrics 结构区分出原文时)
      translated    译文(仅 Lyrics 结构区分出译文时)
      variants      其余版本 [{"label": ..., "text": ..., "language": ...}]

    规则:
    - 优先 `div.Lyrics` 的 `.Lyrics-original` / `.Lyrics-translated`。
    - 否则取章节内所有 `div.poem`, 最长一段为主歌词, 其余进 variants。
    - 主歌词若为 ja 而存在 zh 段, 用 zh 作 lyrics, ja 进 original/variants。
    """
    result: dict[str, Any] = {
        "lyrics": "",
        "language": "unknown",
        "source": "",
        "original": "",
        "translated": "",
        "variants": [],
    }
    if heading is None:
        heading = _find_lyrics_heading(soup)
    if heading is None:
        return result

    nodes = list(_iter_section_siblings(heading))

    # 按文档顺序收集 Lyrics 容器与 poem(去重)
    lyrics_containers: list[Tag] = []
    poems: list[Tag] = []
    seen: set[int] = set()

    def collect(node: Tag) -> None:
        if node.name == "div" and "Lyrics" in (node.get("class") or []):
            if id(node) not in seen:
                seen.add(id(node))
                lyrics_containers.append(node)
        if node.name == "div" and "poem" in (node.get("class") or []):
            if id(node) not in seen:
                seen.add(id(node))
                poems.append(node)
        for child in node.find_all(["div"]):
            classes = child.get("class") or []
            if "Lyrics" in classes and id(child) not in seen:
                seen.add(id(child))
                lyrics_containers.append(child)
            if "poem" in classes and id(child) not in seen:
                seen.add(id(child))
                poems.append(child)

    for node in nodes:
        collect(node)

    # ── 1) div.Lyrics 结构 ──────────────────────────────────
    if lyrics_containers:
        best = max(
            lyrics_containers,
            key=lambda c: max(
                len(_lyrics_lines(c, ".Lyrics-original")),
                len(_lyrics_lines(c, ".Lyrics-translated")),
            ),
        )
        original = _lyrics_lines(best, ".Lyrics-original")
        translated = _lyrics_lines(best, ".Lyrics-translated")
        lang_orig = detect_language(original)
        lang_trans = detect_language(translated)

        lyrics = ""
        language = "unknown"
        if original and lang_orig == "zh":
            lyrics, language = original, lang_orig
        elif original and lang_orig in ("ja", "ko", "en") and translated and lang_trans == "zh":
            # 中文 V 家库优先中文: ja 原文 + zh 译文时取译文。
            lyrics, language = translated, "zh"
        elif original:
            lyrics, language = original, lang_orig
        elif translated:
            lyrics, language = translated, lang_trans

        result.update(
            lyrics=lyrics,
            language=language,
            source="Lyrics",
            original=original if lyrics != original else "",
        )
        # translated 只在确实是「不同语种的译文」时保留;
        # 九九八十一这类页面的 Lyrics-translated 列其实是角色/演唱标注(同为中文),
        # 不能当译文入库。
        if translated and translated != lyrics and lang_trans != lang_orig:
            result["translated"] = translated
        # 同一章节里可能还有 div.poem(如「中文版」标签页), 作为版本保留。
        variants: list[dict[str, str]] = []
        for poem in poems:
            text = _clean_lyrics_text(_node_text(poem, br=_BR_MARK))
            if not text or text == lyrics or text == original:
                continue
            variants.append(
                {
                    "label": _poem_label(poem, heading),
                    "text": text,
                    "language": detect_language(text),
                }
            )
        result["variants"] = variants
        return result

    # ── 2) div.poem 结构 ────────────────────────────────────
    candidates: list[dict[str, str]] = []
    for poem in poems:
        text = _clean_lyrics_text(_node_text(poem, br=_BR_MARK))
        if not text:
            continue
        candidates.append(
            {
                "label": _poem_label(poem, heading),
                "text": text,
                "language": detect_language(text),
            }
        )
    if not candidates:
        return result

    main = max(candidates, key=lambda c: len(c["text"]))  # 最长, 并列取最先
    if main["language"] != "zh":
        zh = [c for c in candidates if c["language"] == "zh"]
        if zh:
            main = max(zh, key=lambda c: len(c["text"]))

    variants = [c for c in candidates if c is not main]
    # 去重(同文重复出现时只留一次)并按原顺序
    uniq: list[dict[str, str]] = []
    seen_text: set[str] = set()
    for item in variants:
        key = item["text"]
        if key in seen_text:
            continue
        seen_text.add(key)
        uniq.append(item)

    result.update(
        lyrics=main["text"],
        language=main["language"],
        source="poem",
        variants=uniq,
    )
    return result


def _find_lyrics_heading(soup: BeautifulSoup) -> Tag | None:
    """定位歌词章节标题(含「歌词及人设」「普通的歌词」等写法)。

    优先 h2(主歌词章节); 只有页面没有 h2 歌词标题时才往 h3/h4 找,
    避免先命中「二次创作」下的某个 h4「歌词」子章节。
    """
    for level in (2, 3, 4):
        for tag in soup.find_all(f"h{level}"):
            text = _clean_text(_node_text(tag, br=" "))
            if "歌词" in text:
                return tag
    return None


# ── 词条页解析 ─────────────────────────────────────────────────


def _preview_text(content: Tag | None) -> str:
    """取第一条非空的 `p.vc-preview` 文本。"""
    if content is None:
        return ""
    for p in content.select("p.vc-preview"):
        text = _clean_text(_node_text(p, br=" "))
        if text:
            return text
    return ""


def _extract_infobox_year(soup: BeautifulSoup) -> int | None:
    """从信息框找「于YYYY年」/「YYYY年M月」中的年份; 找不到返回 None。

    优先匹配「于YYYY年」, 避免命中信息框里的年份导航。
    """
    infobox = soup.select_one("table.moe-infobox") or soup.select_one(".moe-infobox")
    if infobox is None:
        return None
    text = _clean_text(_node_text(infobox, br=" "))
    m = re.search(r"于\s*((?:19|20)\d{2})\s*年", text)
    if m:
        return int(m.group(1))
    for line in text.splitlines():
        if "投稿" in line or "发布" in line:
            y = clean_year(line)
            if y:
                return y
    return None


def _extract_introduction(soup: BeautifulSoup) -> str:
    """简介章节: h2#简介 到下一个 h2 之间的 <p>, 过滤成绩话术, 截 600 字。"""
    heading = _find_heading(soup, "简介")
    if heading is None:
        return ""
    parts: list[str] = []
    for node in _iter_section_siblings(heading):
        if node.name != "p":
            continue
        text = _clean_text(_node_text(node, br=" "))
        if text:
            parts.append(text)
    text = _strip_achievement_lines("\n".join(parts))
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text[:600]


def _extract_bvid(soup: BeautifulSoup) -> str:
    """取正文/信息框里文档序第一个 BV 号(排除脚本/样式/导航框)。"""
    content = _find_content(soup)
    if content is None:
        return ""
    # 在副本上删除脚本与导航框, 避免其中残留的 BV 号抢先命中。
    clone = BeautifulSoup(str(content), "html.parser")
    for bad in clone.select("script, style, table.navbox"):
        bad.decompose()
    return extract_bvid(str(clone))


def _aliases(title: str, name: str, page_title: str) -> list[str]:
    """别名: 页面标题、去括号核名、歌名本身(去重保序)。"""
    out: list[str] = []
    for value in (
        page_title,
        title,
        name,
        bracket_core(page_title),
        bracket_core(title),
        bracket_core(name),
    ):
        text = _clean_text(value)
        if text and text not in out:
            out.append(text)
    return out


def parse_song_page(html: str, title: str) -> SourceRecord:
    """解析单个萌娘百科词条页为 SourceRecord。

    缺少任一元素时只留空串, 不抛异常; 消歧义页/空页面/非歌曲页抛 SourceError。
    """
    if not html or not html.strip():
        raise SourceError(f"萌娘百科: 页面内容为空: {title}")

    soup = BeautifulSoup(html, "html.parser")
    categories = _page_categories(html)

    # 消歧义页: 分类或首段「XXX可以指：」
    if "消歧义页" in categories:
        raise SourceError(f"萌娘百科: 消歧义页, 需要更精确的标题: {title}")

    content = _find_content(soup)
    if content is None:
        raise SourceError(f"萌娘百科: 未找到正文主体(可能被反爬/页面不存在): {title}")

    preview = _preview_text(content)
    if not preview:
        first_p = content.find("p")
        first_text = _clean_text(_node_text(first_p, br=" ")) if first_p else ""
        if first_text and re.search(r"可以指\s*[：:]", first_text[:120]):
            raise SourceError(f"萌娘百科: 消歧义页, 需要更精确的标题: {title}")

    page_title = _page_title(soup, title)
    lyrics_heading = _find_lyrics_heading(soup)

    # 歌名
    name = ""
    m = _NAME_RE.search(preview)
    if m:
        name = _clean_text(m.group(1))
    if not name:
        name = page_title

    # 演唱
    singers = ""
    m = _SINGER_RE.search(preview)
    if m:
        singers = _clean_text(m.group(1))
    if not singers:
        m = _SINGER_ALT_RE.search(preview)
        if m:
            singers = _clean_text(m.group(1))
    singers = re.sub(r"^由\s*", "", singers).strip("，,。；; ")

    # UP主 / 年份
    uploader = ""
    year: int | None = None
    m = _UPLOADER_RE.search(preview)
    if m:
        uploader = _clean_text(m.group(1))
        year = int(m.group(2))
    else:
        m = _UPLOADER_ALT_RE.search(preview)
        if m:
            uploader = _clean_text(m.group(1))
    uploader = re.sub(r"^由\s*", "", uploader).strip("，,。；; ")

    if year is None:
        year = _extract_infobox_year(soup)
    if year is None:
        year = clean_year(preview)
    introduction = _extract_introduction(soup)
    if year is None:
        year = clean_year(introduction)

    # STAFF / 歌词
    credits = _collect_staff(soup, lyrics_heading)
    parsed_lyrics = parse_lyrics_section(soup, lyrics_heading)

    lyrics = parsed_lyrics.get("lyrics") or ""
    if not singers:
        singers = credits.get("singers", "")

    # 空页面/非歌曲词条: 没有歌曲 preview, 也没有歌词 —— 只有简介的是角色/条目页
    if not preview and not lyrics:
        raise SourceError(f"萌娘百科: 页面未解析出歌曲信息(空页面/非歌曲词条): {title}")

    bvid = _extract_bvid(soup)

    extra: dict[str, Any] = {
        "page_title": page_title,
        "categories": categories,
    }
    if parsed_lyrics.get("source"):
        extra["lyrics_source"] = parsed_lyrics["source"]
        extra["lyrics_language"] = parsed_lyrics.get("language", "unknown")
    if parsed_lyrics.get("original"):
        extra["lyrics_original"] = parsed_lyrics["original"]
    if parsed_lyrics.get("translated"):
        extra["lyrics_translated"] = parsed_lyrics["translated"]
    if parsed_lyrics.get("variants"):
        extra["lyrics_variants"] = parsed_lyrics["variants"]

    source_url = f"{BASE_URL}/{quote(title, safe='')}"
    return SourceRecord(
        source="moegirl",
        source_title=title,
        source_url=source_url,
        license=MOEGIRL_LICENSE,
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
        year=year,
        introduction=introduction,
        lyrics=lyrics,
        bvid=bvid,
        aliases=_aliases(title, name, page_title),
        extra=extra,
    )


# ── 适配器本体 ─────────────────────────────────────────────────


class MoegirlAdapter(SourceAdapter):
    """萌娘百科来源适配器(同步 requests + BeautifulSoup)。

    默认 4.5s/请求、并发 1; 403/429/挑战页退避 15-30s, 最多重试 2 次。
    """

    name = "moegirl"
    license = MOEGIRL_LICENSE
    default_interval = 4.5
    default_concurrency = 1
    max_retries = 2

    def __init__(self, config: dict | None = None):
        config = config or {}
        self.base_url = str(config.get("base_url") or BASE_URL).rstrip("/")
        self.timeout = float(config.get("timeout", 30))
        raw_categories = config.get("categories")
        if raw_categories is None:
            self.categories = list(DEFAULT_CATEGORIES)
        elif isinstance(raw_categories, str):
            self.categories = [raw_categories] if raw_categories.strip() else list(DEFAULT_CATEGORIES)
        else:
            self.categories = [str(c).strip() for c in raw_categories if str(c).strip()]
            if not self.categories:
                self.categories = list(DEFAULT_CATEGORIES)
        self.max_pages_per_category = int(config.get("max_pages_per_category") or 0)
        self.interval = max(0.0, float(config.get("interval", self.default_interval)))
        self.max_retries = max(0, int(config.get("max_retries", type(self).max_retries)))
        self.user_agent = str(config.get("user_agent") or MOBILE_USER_AGENT)

        injected = config.get("session")
        self._owns_session = injected is None
        self.session = injected if injected is not None else requests.Session()
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            try:
                headers.setdefault("User-Agent", self.user_agent)
                headers.setdefault(
                    "Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                )
                headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9")
            except AttributeError:
                pass

        # 单调时钟节流(单进程协程/线程共用)
        self._throttle_lock = threading.Lock()
        self._next_allowed = 0.0

    # ── 请求与限速 ──────────────────────────────────────────

    def _wait_interval(self) -> None:
        """按 interval 预留请求时隙(锁内只排队, 锁外睡眠)。"""
        if self.interval <= 0:
            return
        with self._throttle_lock:
            now = _time.monotonic()
            slot = max(now, self._next_allowed)
            self._next_allowed = slot + self.interval
        delay = slot - _time.monotonic()
        if delay > 0:
            _time.sleep(delay)

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """指数退避: 首轮 15-30s, 之后翻倍, 上限 120s。"""
        return min(120.0, random.uniform(15.0, 30.0) * (2 ** attempt))

    def _get(self, url: str) -> Any:
        """带节流与退避的 GET; 反爬页/403/429 重试, 最终抛 SourceError。"""
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self._wait_interval()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"网络错误: {exc}"
                logger.warning(
                    f"萌娘百科: 请求失败({attempt + 1}/{self.max_retries + 1}) "
                    f"{url}: {exc}"
                )
                if attempt < self.max_retries:
                    delay = self._backoff_delay(attempt)
                    logger.info(f"萌娘百科: {delay:.1f}s 后重试")
                    _time.sleep(delay)
                    continue
                raise SourceError(f"萌娘百科: 请求失败 {url}: {exc}") from exc

            status = int(getattr(resp, "status_code", 0) or 0)
            text = getattr(resp, "text", "") or ""

            if status == 200 and not _is_challenge_page(text, status):
                return resp

            if status in (403, 429, 503) or _is_challenge_page(text, status):
                last_error = f"HTTP {status}(反爬挑战/限流)"
            elif status >= 500:
                last_error = f"HTTP {status}(服务端错误)"
            else:
                # 404 等确定性错误不重试
                return resp

            logger.warning(
                f"萌娘百科: {last_error} ({attempt + 1}/{self.max_retries + 1}) {url}"
            )
            if attempt < self.max_retries:
                delay = self._backoff_delay(attempt)
                logger.info(f"萌娘百科: {delay:.1f}s 后重试")
                _time.sleep(delay)

        raise SourceError(f"萌娘百科: 请求被拒绝/重试耗尽: {url} ({last_error})")

    @staticmethod
    def _quote_path(title: str) -> str:
        """按 URL 规范编码标题(空格为 %20; MediaWiki 与下划线等价)。"""
        return quote(title, safe="")

    def _category_url(self, category: str, from_title: str = "") -> str:
        name = category.strip()
        for prefix in ("Category:", "分类:"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        url = f"{self.base_url}/Category:{quote(name, safe='')}"
        if from_title:
            url += f"?from={quote(from_title, safe='')}"
        return url

    def _page_url(self, title: str) -> str:
        return f"{self.base_url}/{self._quote_path(title)}"

    # ── 契约实现 ────────────────────────────────────────────

    def list_titles(self, *, limit: int | None = None) -> Iterable[str]:
        """按分类逐页枚举标题, 用 `?from=<上一页最后标题>` 翻页并去重。"""
        if limit is not None and limit <= 0:
            return

        seen: set[str] = set()
        yielded = 0
        page_cap = self.max_pages_per_category

        for category in self.categories:
            from_title = ""
            pages = 0
            total: int | None = None
            category_count = 0

            while True:
                if page_cap > 0 and pages >= page_cap:
                    logger.info(
                        f"萌娘百科: 分类「{category}」达到 max_pages_per_category={page_cap}, "
                        f"已枚举 {category_count} 首"
                    )
                    break

                url = self._category_url(category, from_title)
                resp = self._get(url)
                status = int(getattr(resp, "status_code", 0) or 0)
                if status != 200:
                    raise SourceError(
                        f"萌娘百科: 分类页返回 HTTP {status}: {category} ({url})"
                    )

                body = getattr(resp, "text", "") or ""
                if _is_challenge_page(body, status):
                    raise SourceError(f"萌娘百科: 分类页被反爬拦截: {category} ({url})")

                if total is None:
                    total = parse_category_total(body)
                titles = parse_category_titles(body)
                next_cursor = parse_next_pagefrom(body)
                pages += 1
                # 注意: category_count 统计的是"跨分类去重后的净增", 因此
                # 它小于 total 属于正常现象(同一首歌可属多个歌姬分类),
                # 不能用它判断页面是否残缺。
                if not titles:
                    if total is not None and category_count < total and next_cursor:
                        # 空页但站点仍给出下一页游标: 换游标再试, 不能当成枚举结束
                        logger.warning(
                            f"萌娘百科: 分类「{category}」第 {pages} 页为空但仍有下一页, "
                            f"继续用游标 {next_cursor!r}"
                        )
                        if next_cursor == from_title:
                            break
                        from_title = next_cursor
                        continue
                    break

                new_count = 0
                for title in titles:
                    if title in seen:
                        continue
                    seen.add(title)
                    category_count += 1
                    new_count += 1
                    yield title
                    yielded += 1
                    if limit is not None and yielded >= limit:
                        logger.info(f"萌娘百科: 已达到 limit={limit}, 停止枚举")
                        return

                logger.info(
                    f"萌娘百科: 分类「{category}」第 {pages} 页, "
                    f"新增 {new_count} 首, 累计 {len(seen)} 首"
                    + (f" / 共 {total} 个页面" if total else "")
                )

                last_title = titles[-1]
                # 游标必须用站点给出的 pagefrom(排序键), 不能用标题本身:
                # 中文标题按拼音排序, 标题作游标会原地打转导致枚举提前结束。
                # 站点没有下一页链接时, 若仍未取满标称数量则记录告警。
                if not next_cursor or next_cursor == from_title:
                    # 站点已无下一页: 若本分类页面总数(含跨分类重复)仍少于
                    # 标称值, 说明页面可能不完整, 记录告警便于补跑排查。
                    if total is not None and pages * 200 < total:
                        logger.warning(
                            f"萌娘百科: 分类「{category}」枚举结束, 已取 {pages} 页 "
                            f"(标称 {total} 首), 可能不完整"
                        )
                    break
                from_title = next_cursor

            logger.info(
                f"萌娘百科: 分类「{category}」完成, "
                f"本分类枚举 {category_count} 首"
                + (f" (站点标注共 {total} 个页面)" if total else "")
            )

        logger.info(f"萌娘百科: 全部分类枚举完成, 去重后共 {len(seen)} 个标题")

    def fetch(self, title: str) -> SourceRecord:
        """抓取并解析单个词条页。"""
        title = (title or "").strip()
        if not title:
            raise SourceError("萌娘百科: 标题为空")
        url = self._page_url(title)
        resp = self._get(url)
        status = int(getattr(resp, "status_code", 0) or 0)
        if status == 404:
            raise SourceError(f"萌娘百科: 页面不存在: {title}")
        if status != 200:
            raise SourceError(f"萌娘百科: 页面返回 HTTP {status}: {title} ({url})")

        body = getattr(resp, "text", "") or ""
        if _is_challenge_page(body, status):
            raise SourceError(f"萌娘百科: 页面被反爬拦截: {title} ({url})")

        record = parse_song_page(body, title)
        logger.debug(
            f"萌娘百科: 解析 {title} → {record.name} / {record.singers} / "
            f"{record.year} / staff {sum(bool(x) for x in (record.composer, record.lyricist, record.arranger))} 项"
        )
        return record

    def close(self) -> None:
        """仅关闭自己创建的 Session; 注入的 session 由调用方管理。"""
        if self._owns_session and self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass


__all__ = [
    "BASE_URL",
    "DEFAULT_CATEGORIES",
    "MOBILE_USER_AGENT",
    "MOEGIRL_LICENSE",
    "MoegirlAdapter",
    "parse_category_titles",
    "parse_category_total",
    "parse_lyrics_section",
    "parse_song_page",
    "parse_staff_table",
]
