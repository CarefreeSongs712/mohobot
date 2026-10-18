"""歌曲名称/歌手/语言归一化 — 多来源对齐与模糊匹配共用的纯函数。

设计要点(实测数据支撑, 见 docs/DEVELOPMENT.md 歌曲知识一节):
- 名称归一化只用于"生成候选/建索引", 绝不直接作为唯一键: 本地库去括号后
  有 81 个同名冲突组(其中 50 组演唱集合不同, 是不同歌曲)。
- 括号后缀分两类: 演唱者/P主/社团/系列 是消歧信息; 中文版/翻唱/伴奏/Remix
  才是版本信息。只有后者参与 version 判定。
- 语言判定用于"中文 V 家优先、非中文也收录但不占主要"的入库配额,
  以及 LyricsKai 的 original/translated 谁是真原文的兜底判断。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# ── 名称归一化 ────────────────────────────────────────────────

# 归一化时保留的字符: 中日韩、字母数字
_KEEP_RE = re.compile(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring)\.?\s*[^)\]}]*", re.IGNORECASE)
# 名称尾部的括号组: 半角/全角圆括号、方括号、书名号外层
_TRAILING_BRACKET_RE = re.compile(r"\s*[\(（\[【]\s*([^\)）\]】]{1,40})\s*[\)）\]】]\s*$")

# 版本标记(命中才算"同一首歌的不同版本", 不是消歧后缀)
_VERSION_WORDS = (
    "中文版", "日文版", "英文版", "粤语版", "日语版", "国语版",
    "翻唱", "伴奏", "纯音乐", "instrumental", "off vocal", "offvocal",
    "remix", "重制", "重置", "混音版", "现场版", "live", "demo",
    "完整版", "短版", "tv size", "tvsize", "acoustic", "piano",
)
_VERSION_RE = re.compile(r"\b(?:ver|version)\.?\s*[\w.]*", re.IGNORECASE)

# 消歧后缀关键词(演唱者/P主/社团等): 不构成版本
_DISAMBIG_WORDS = (
    "歌曲", "曲", "v家", "虚拟歌手", "vocaloid", "utau", "翻调",
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")

_SINGER_SPLIT_RE = re.compile(r"[,，、/&＆×]|\s+feat\.?\s+|\s+ft\.?\s+", re.IGNORECASE)


def nfkc(text: str) -> str:
    """全角/半角、兼容字符统一(NFKC)。"""
    return unicodedata.normalize("NFKC", text or "")


def normalize_name(name: str) -> str:
    """歌名归一化: 用于候选生成与索引键, 不用于唯一约束。

    NFKC → 小写 → 去 feat./ft. → 去空白/标点/括号/书名号 → 保留字母数字与 CJK。
    """
    s = nfkc(name or "").strip()
    if not s:
        return ""
    s = _FEAT_RE.sub("", s)
    s = s.lower()
    return _KEEP_RE.sub("", s)


def bracket_core(name: str) -> str:
    """去掉尾部括号后缀后的核名(仅用于对齐候选, 不可直接当唯一键)。

    只剥一层; `彼岸花(洛天依)` → `彼岸花`, `X(中文版)` → `X`。
    """
    s = nfkc(name or "").strip()
    m = _TRAILING_BRACKET_RE.search(s)
    if not m:
        return s
    return s[:m.start()].strip()


def bracket_suffix(name: str) -> str:
    """取尾部括号内的后缀(无则空串)。"""
    m = _TRAILING_BRACKET_RE.search(nfkc(name or "").strip())
    return m.group(1).strip() if m else ""


def classify_suffix(name: str) -> str:
    """把尾部括号后缀分类: version / disambiguation / unknown / ""。"""
    suffix = bracket_suffix(name).lower()
    if not suffix:
        return ""
    if any(w in suffix for w in _VERSION_WORDS) or _VERSION_RE.search(suffix):
        return "version"
    # 纯年份 / 纯数字 / 演唱者名等都视为消歧信息
    if any(w in suffix for w in _DISAMBIG_WORDS):
        return "disambiguation"
    return "disambiguation"


def is_version_name(name: str) -> bool:
    """名称是否带版本标记(用于防止把不同版本合并)。"""
    s = nfkc(name or "").lower()
    if classify_suffix(name) == "version":
        return True
    if any(w in s for w in _VERSION_WORDS):
        return True
    return bool(_VERSION_RE.search(s))


def alias_keys(name: str) -> set[str]:
    """一首歌可被哪些写法指代: 原名归一化 + 去括号核名归一化。"""
    keys = {normalize_name(name)}
    core = bracket_core(name)
    if core and core != name:
        keys.add(normalize_name(core))
    keys.discard("")
    return keys


# ── 歌手集合 ──────────────────────────────────────────────────


def split_singers(value: str) -> list[str]:
    """把演唱字段拆成去重后的歌手列表(保持出现顺序)。"""
    seen: set[str] = set()
    out: list[str] = []
    for part in _SINGER_SPLIT_RE.split(value or ""):
        name = nfkc(part).strip(" .·-—_")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def singers_key(value: str) -> str:
    """歌手集合的稳定键(排序后拼接), 用于冲突检测。"""
    return "|".join(sorted(normalize_name(x) for x in split_singers(value)))


def singers_overlap(a: str, b: str) -> bool:
    """两个演唱字段是否有交集。"""
    sa = {normalize_name(x) for x in split_singers(a)}
    sb = {normalize_name(x) for x in split_singers(b)}
    sa.discard("")
    sb.discard("")
    return bool(sa & sb)


# ── 语言判定 ──────────────────────────────────────────────────


def detect_language(text: str) -> str:
    """粗粒度语种: zh / ja / ko / en / unknown。

    中文优先: 汉字占非空白字符 20% 以上且假名不足汉字一半时判为 zh。
    歌词里中日混排常见, 这里只用于入库配额与"原文/译文"兜底。
    """
    s = text or ""
    if not s.strip():
        return "unknown"
    cjk = len(_CJK_RE.findall(s))
    kana = len(_KANA_RE.findall(s))
    hangul = len(_HANGUL_RE.findall(s))
    latin = len(_LATIN_RE.findall(s))
    total = max(1, cjk + kana + hangul + latin)
    if hangul and hangul / total >= 0.3:
        return "ko"
    if kana and kana >= max(4, cjk * 0.5):
        return "ja"
    if cjk and cjk / total >= 0.2:
        return "zh"
    if latin / total >= 0.5:
        return "en"
    return "unknown"


def has_chinese_singer(singers: str, known: Iterable[str]) -> bool:
    """演唱字段是否包含给定的中文歌姬集合中的任一位。"""
    names = {normalize_name(x) for x in split_singers(singers)}
    return any(normalize_name(k) in names for k in known)


# ── 标识符清洗 ────────────────────────────────────────────────

_BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")


def extract_bvid(*texts: str) -> str:
    """从 URL/文本中提取 B 站 BV 号(找不到返回空串)。"""
    for text in texts:
        m = _BV_RE.search(text or "")
        if m:
            return m.group(0)
    return ""


def clean_year(value) -> int | None:
    """把 '2017-03-25' / '2017年' / 2017 归一为年份整数。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1990 <= value <= 2100 else None
    m = re.search(r"(19|20)\d{2}", str(value))
    if not m:
        return None
    year = int(m.group(0))
    return year if 1990 <= year <= 2100 else None
