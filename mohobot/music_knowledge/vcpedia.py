"""VCPedia 新歌同步 — 重写(适配 Anubis PoW 反爬 + 完整创作人员/歌词)。

与旧版(mohobot/agent/music_knowledge/vcpedia.py)的区别:
- 站点已启用 Anubis 1.27 PoW 挑战(明文请求全部 403), 需先解题拿 auth cookie;
  cookie 持久化到 data/song_knowledge/anubis_cookies.json 复用(失效自动重解)。
- 列表入口: MediaWiki api.php list=categorymembers(Category:洛天依歌曲)全量分页,
  不再依赖当年模板页。
- 词条: 优先 rest.php/v1/page/{title}/source 取 wikitext(命中不依赖渲染,
  歌词/STAFF 表/歌曲名在 source 中结构稳定); 渲染 HTML 作为兜底。
- 入库: 新增 词/曲/编/混/调(调校)/母带/PV/曲绘/年份 字段, 完整歌词保留换行;
  不再生成 song_name_keywords.txt / song_lyric_keywords.txt。
"""

from __future__ import annotations

import hashlib
import json
import re
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from loguru import logger

from mohobot.music_knowledge.pool import ensure_init, get_session
from mohobot.music_knowledge.song_database import Song, update_song_stats

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)

# Anubis 挑战元数据(从 challenge 页解析)
_CHALLENGE_SCRIPT_ID = "anubis_challenge"
_VERSION_SCRIPT_ID = "anubis_version"
_ANUBIS_COOKIE_FILE = "anubis_cookies.json"


# ── Anubis PoW 客户端 ──────────────────────────────────────────


class AnubisClient:
    """解 Anubis PoW 挑战并维护 auth cookie(持久化复用, 失效自动重解)。"""

    def __init__(self, base_url: str = "https://vcpedia.cn",
                 cookie_file: str | Path | None = None,
                 timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self.auth_cookie: Optional[str] = None  # techaro.lol-anubis-auth-* 值
        self.cookie_name: str = ""

    # ── 会话管理 ───────────────────────────────────────────────

    def _cookie_jar(self):
        jar = requests.cookies.RequestsCookieJar()
        if self.auth_cookie and self.cookie_name:
            jar.set(self.cookie_name, self.auth_cookie, domain="vcpedia.cn", path="/")
        return jar

    def _load_cookie(self) -> bool:
        """从磁盘恢复 auth cookie(未过期则直接复用)。"""
        if self.cookie_file is None or not self.cookie_file.exists():
            return False
        try:
            data = json.loads(self.cookie_file.read_text(encoding="utf-8"))
            name = data.get("name", "")
            value = data.get("value", "")
            expires = data.get("expires", 0)
            if name and value and (expires == 0 or expires > _time.time()):
                self.cookie_name = name
                self.auth_cookie = value
                logger.info("VCPedia: 复用已保存的 Anubis auth cookie")
                return True
        except Exception as e:
            logger.warning(f"VCPedia: 读取 cookie 失败: {e}")
        return False

    def _save_cookie(self) -> None:
        if self.cookie_file is None:
            return
        try:
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self.cookie_file.write_text(
                json.dumps({
                    "name": self.cookie_name,
                    "value": self.auth_cookie,
                    "expires": _time.time() + 6 * 24 * 3600,  # ~一周后过期
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("VCPedia: Anubis auth cookie 已保存")
        except Exception as e:
            logger.warning(f"VCPedia: 保存 cookie 失败: {e}")

    # ── 挑战解题 ───────────────────────────────────────────────

    def _parse_challenge(self, html: str) -> Dict[str, Any]:
        """从挑战页解析 id / randomData / method / difficulty。

        challenge 为 <script id="anubis_challenge">JSON</script>,
        JSON 形如 {"challenge": {"id": ..., "randomData": ..., "method": "fast"},
                   "rules": {"difficulty": 4}}。
        """
        m = re.search(rf'<script id="{_CHALLENGE_SCRIPT_ID}">(.*?)</script>', html, re.S)
        if not m:
            raise RuntimeError("VCPedia: 未找到 Anubis challenge 脚本")
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"VCPedia: challenge JSON 解析失败: {e}") from e
        challenge = data.get("challenge") or {}
        rules = data.get("rules") or {}
        cid = challenge.get("id")
        random_data = challenge.get("randomData")
        method = challenge.get("method") or "fast"
        difficulty = int(rules.get("difficulty", 4))
        if not cid or not random_data:
            raise RuntimeError("VCPedia: challenge 缺少 id/randomData")
        return {"id": cid, "randomData": random_data, "method": method,
                "difficulty": difficulty}

    @staticmethod
    def _solve_pow(random_data: str, difficulty: int, max_tries: int = 2_000_000) -> int:
        """找 nonce 使 sha256(randomData + nonce) 以 difficulty 个 0 开头。"""
        target = "0" * difficulty
        for nonce in range(1, max_tries + 1):
            digest = hashlib.sha256(
                (random_data + str(nonce)).encode("utf-8")
            ).hexdigest()
            if digest.startswith(target):
                return nonce
        raise RuntimeError("VCPedia: PoW 未能在限定次数内解出")

    def _fetch_cookie(self) -> None:
        """完整走一遍: GET 挑战页 → 解 PoW → pass-challenge → 取 auth cookie。"""
        sess = requests.Session()
        sess.headers.update({"User-Agent": USER_AGENT})
        sess.headers.update({"Accept": "text/html,application/xhtml+xml,*/*"})
        cache_buster = int(_time.time() * 1000)
        target = f"{self.base_url}/?cb={cache_buster}"
        r = sess.get(target, timeout=self.timeout, allow_redirects=False)
        if r.status_code != 403:
            # 可能没有挑战(反爬关闭): 无 auth cookie 也能直连? 保守起见视为失败
            logger.info(f"VCPedia: GET {target} 返回 {r.status_code}, 尝试直接使用会话")
            sess.close()
            return
        html = r.text
        try:
            ch = self._parse_challenge(html)
        except RuntimeError as e:
            logger.warning(f"VCPedia: {e}, 放弃挑战(同步将失败)")
            sess.close()
            return
        nonce = self._solve_pow(ch["randomData"], ch["difficulty"])

        # 保留挑战设置的验证 cookie(Partitioned; SameSite=None)
        cookies = sess.cookies
        pass_url = (
            f"{self.base_url}/.within.website/x/cmd/anubis/api/pass-challenge"
            f"?id={quote(ch['id'])}&response=1&nonce={nonce}"
            f"&redir={quote(target)}&elapsedTime={_time.time() * 1000:.0f}"
        )
        resp = sess.get(
            pass_url, timeout=self.timeout, allow_redirects=False,
            headers={"Referer": target},
        )
        if resp.status_code != 200:
            logger.warning(f"VCPedia: pass-challenge 返回 {resp.status_code}")
            sess.close()
            return
        # 从 Set-Cookie 提取 auth cookie
        for name, value in sess.cookies.items():
            if "anubis-auth" in name:
                self.cookie_name = name
                self.auth_cookie = value
        if not self.auth_cookie:
            for name, value in cookies.items():
                if "anubis-auth" in name:
                    self.cookie_name = name
                    self.auth_cookie = value
        if self.auth_cookie:
            logger.info("VCPedia: Anubis PoW 解题成功, 已取得 auth cookie")
            self._save_cookie()
        else:
            logger.warning("VCPedia: pass-challenge 后未找到 auth cookie")
        sess.close()

    # ── 对外请求 ───────────────────────────────────────────────

    def get(self, url: str, **kwargs) -> requests.Response:
        """带 auth cookie 的 GET(402/403 时自动重解一次)。"""
        timeout = kwargs.pop("timeout", self.timeout)
        if not self._load_cookie() and not self.auth_cookie:
            self._fetch_cookie()
        for attempt in range(2):
            resp = requests.get(
                url, headers={"User-Agent": USER_AGENT, **kwargs.pop("headers", {})},
                cookies=self._cookie_jar(), timeout=timeout, allow_redirects=True,
                **kwargs,
            )
            if resp.status_code in (401, 403):
                # cookie 失效 → 重解
                self.auth_cookie = None
                self._fetch_cookie()
                if self.auth_cookie:
                    continue
                return resp
            return resp
        return resp


# ── wikitext 解析 ──────────────────────────────────────────────

# STAFF 表 / 信息行常见的键别名(按顺序匹配)
_CREDIT_ALIASES: Dict[str, List[str]] = {
    "uploader": ["UP主", "投稿者", "发布者", "UP"],
    "singers": ["演唱", "歌手", "演唱者"],
    "lyricist": ["作词", "词作", "作詞"],
    "composer": ["作曲", "曲作", "作曲者"],
    "arranger": ["编曲", "编曲者", "編曲"],
    "mixer": ["混音", "混合", "remix", "混音后期"],
    "tuner": ["调教", "调校", "调声", "調教", "VOCALOID调教"],
    "mastering": ["母带", "母带处理"],
    "pv": ["PV", "视频制作", "映像", "影片"],
    "illustrator": ["曲绘", "绘", "插画", "绘图", "曲繪"],
}

# 行内 infobox 可能出现的标题键(识别单列交替行)
_INFIX_KEYS = (
    "演唱", "作词", "作曲", "编曲", "作编曲", "PV", "UP主", "曲绘",
    "混音", "调教", "调校", "母带",
)


def _first_alias(text: str, aliases: List[str]) -> str:
    """在文本中找第一个出现的别名, 取其后内容(直到行尾/分号/括回)。"""
    for a in aliases:
        idx = text.find(a)
        if idx >= 0:
            rest = text[idx + len(a):].strip()
            # 去掉前后可能的分隔符
            rest = rest.strip(": ：= ==").strip()
            # 截到常见分隔(换行/ "|" / "，" / "；" 前半) — 保守起见保留到行尾再清洗
            return _clean_value(rest)
    return ""


def _clean_value(v: str) -> str:
    """清洗 wikitext 值: 去链接/模板/全角空格/多余空白。"""
    if not v:
        return ""
    v = re.sub(r"\[\[([^\]|]*)\|?[^\]]*\]\]", r"\1", v)      # [[a|b]] → a
    v = re.sub(r"\[(?:https?://)?[^\s\]]+\s([^\]]*)\]", r"\1", v)  # [url 文字] → 文字
    v = re.sub(r"<ref[^>]*>.*?</ref>", "", v, flags=re.S)
    v = re.sub(r"<[^>]+>", "", v)
    v = v.replace("\u3000", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    # 去掉结尾残留的 }}(模板收尾)等
    v = re.sub(r"[}]+$", "", v).strip()
    v = v.strip("|").strip()
    parts = [p for p in re.split(r"[\n]|(?:\|\|)", v) if p.strip()]
    if parts:
        v = parts[0].strip()
    return re.sub(r"\s+", " ", v).strip()


def _parse_credits_from_lines(lines: List[str]) -> Dict[str, str]:
    """从 wikitext 逐行解析创作人员/年份; 返回 {key: value}。

    兼容两种形态: "== 歌词 ==" 前的 {{信息|演唱=洛天依}} 一行内多次赋值,
    以及 "|作词=青柠" 这类表格行。
    """
    credits: Dict[str, str] = {}
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        # 单行内可能存在多个 键=值 或 键：值(如 {{STAFF|演唱=xx|作词=xx}})
        tokens = re.split(r"[|{}]", line)
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            m = re.match(r"^([^=：:]{1,8})\s*[=：:]\s*(.+)$", token)
            if m:
                key_raw, val_raw = m.group(1).strip(), m.group(2).strip()
                key = _normalize_credit_key(key_raw)
                if key and key not in credits:
                    val = _clean_value(val_raw)
                    if val and len(val) < 64:
                        credits[key] = val
                        break  # 每行只取第一个命中键(防一行内多个键串扰)
        # 年份: 页面标题/内容中 "2025年X月X日投稿"
        if "year" not in credits:
            m = re.search(r"(20\d{2})\s*年", line)
            if m:
                credits["year"] = m.group(1)
    return credits


def _normalize_credit_key(raw: str) -> str:
    """把 wikitext 键名规范化到 CREDIT_ALIASES 的标准键。"""
    for key, aliases in _CREDIT_ALIASES.items():
        if raw == key or raw in aliases:
            return key
    return ""


def _parse_lyrics_from_source(source: str) -> str:
    """从 wikitext 提取完整歌词(==歌词== 段, 保留换行)。

    仅把各演唱者的「歌词行」按出现顺序拼接; 排除 STAFF/注释/标题行。
    """
    # 定位 ===歌词=== 段(有的用 === / ==== 层级)
    m = re.search(r"^={2,4}\s*歌词\s*={2,4}\s*$", source, re.M)
    if not m:
        return ""
    tail = source[m.end():]
    lines: List[str] = []
    for line in tail.splitlines():
        if re.match(r"^\s*={2,4}\s*\S+\s*={2,4}\s*$", line):
            break  # 下一个章节
        s = line.strip()
        if not s:
            lines.append("")
            continue
        # 跳过注释/模板/文字说明(只保留歌词内容行)
        if s.startswith("{{") or s.startswith("<!--") or  \
                re.match(r"^\s*(?:演唱|作词|作曲|编曲|调教|混音|母带|曲绘|PV)\s*[：:=]", s):
            continue
        # 去 span/引用/全角空格
        s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
        s = re.sub(r"<[^>]+>", "", s)
        s = s.replace("[", "").replace("]", "").replace("\u3000", " ")
        # 保留“词句”中的内部格式(如“【间奏】”)
        if s.startswith("{{") or s.startswith("}") or s.startswith("[["):
            continue
        lines.append(s.strip())
    # 合并空行(去除连续空行)
    cleaned: List[str] = []
    prev_blank = False
    for ln in lines:
        if not ln:
            if not prev_blank:
                cleaned.append("")
            prev_blank = True
        else:
            cleaned.append(ln)
            prev_blank = False
    return "\n".join(cleaned).strip()


def _parse_introduction_from_source(source: str) -> str:
    """从 wikitext 提取简介(==简介== 段前若干行文本)。"""
    m = re.search(r"^={2,4}\s*(?:简介|概述|歌曲信息)\s*={2,4}\s*$", source, re.M)
    if not m:
        return ""
    tail = source[m.end():]
    lines: List[str] = []
    for line in tail.splitlines():
        if re.match(r"^\s*={2,4}\s*\S+\s*={2,4}\s*$", line):
            break
        s = line.strip()
        if not s or s.startswith("{{"):
            continue
        s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
        s = re.sub(r"<[^>]+>", "", s)
        s = s.replace("[[", "").replace("]]", "").replace("\u3000", " ")
        lines.append(s)
    text = " ".join(lines).strip()
    return text[:500]


# ── HTML 解析(兜底) ────────────────────────────────────────────


def _parse_credits_from_html(soup: BeautifulSoup) -> Dict[str, str]:
    """从渲染 HTML 提取创作人员: 优先 moe-infobox, 再扫正文含键的行。"""
    credits: Dict[str, str] = {}

    def _try_assign(key: str, val: str) -> None:
        if key in credits:
            return
        v = _clean_value(val)
        if v and len(v) < 64:
            credits[key] = v

    # infobox: 键 td 值 td 相邻
    infobox = soup.find("table", class_="moe-infobox")
    if infobox:
        cells = infobox.find_all(["th", "td"])
        for i in range(len(cells) - 1):
            text = cells[i].get_text(strip=True)
            for key, aliases in _CREDIT_ALIASES.items():
                if any(text == a or text.startswith(a) for a in aliases):
                    val = cells[i + 1].get_text(" ", strip=True)
                    _try_assign(key, val)
                    break

    # 正文表格 / 段落: 形如 "作词 | xxx" 或 "作词：xxx"
    if not credits:
        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            text = cells[0].get_text(strip=True)
            for key, aliases in _CREDIT_ALIASES.items():
                if any(text == a or text.startswith(a) for a in aliases):
                    val = cells[1].get_text(" ", strip=True)
                    _try_assign(key, val)
                    break
    return credits


def _parse_lyrics_from_html(soup: BeautifulSoup) -> str:
    """从渲染 HTML 提取歌词(div.poem; 出现多个时全部拼接保留换行)。"""
    poems = soup.select("div.poem")
    if not poems:
        return ""
    lines: List[str] = []
    for poem in poems:
        spans = poem.find_all("span")
        for span in spans:
            s = span.get_text("\n", strip=True)
            if s:
                lines.append(s)
    text = "\n".join(lines)
    text = text.replace("\u3000", " ").strip()
    return text


def _parse_introduction_from_html(soup: BeautifulSoup) -> str:
    """从渲染 HTML 提取简介(简介 h2 后的文本段)。"""
    for h2 in soup.find_all("h2"):
        if "简介" in h2.get_text() or "概述" in h2.get_text():
            parts: List[str] = []
            for sibling in h2.next_siblings:
                if getattr(sibling, "name", None) in ("h2", "h3"):
                    break
                if getattr(sibling, "name", None) == "p":
                    t = sibling.get_text(strip=True)
                    if t and not t.startswith("{{"):
                        parts.append(t)
            text = " ".join(parts).strip()
            if text:
                return text[:500]
    return ""


def _parse_year_from_html(soup: BeautifulSoup) -> str:
    m = re.search(r"(20\d{2})\s*年", soup.get_text("\n"))
    return m.group(1) if m else ""


# ── 主同步 ─────────────────────────────────────────────────────


def _safe_song_name(name: str) -> str:
    return "".join(c for c in (name or "") if c.isalnum() or c in (" ", "-", "_")).strip()


def _song_exists(db, song_name: str) -> bool:
    safe = _safe_song_name(song_name)
    return db.query(Song).filter(
        (Song.name == song_name) | (Song.safe_name == safe)
    ).first() is not None


def build_song_record(name: str, credits: Dict[str, str],
                      introduction: str, lyrics: str) -> Song:
    """按新 schema 构造 Song(缺失的 credits 字段留空)。"""
    return Song(
        name=name,
        safe_name=_safe_song_name(name),
        uploader=credits.get("uploader") or "",
        singers=credits.get("singers") or "",
        lyricist=credits.get("lyricist") or "",
        composer=credits.get("composer") or "",
        arranger=credits.get("arranger") or "",
        mixer=credits.get("mixer") or "",
        tuner=credits.get("tuner") or "",
        mastering=credits.get("mastering") or "",
        pv=credits.get("pv") or "",
        illustrator=credits.get("illustrator") or "",
        year=int(credits["year"]) if credits.get("year", "").isdigit() else None,
        introduction=introduction,
        lyrics=lyrics,
    )


class VCPediaFetcher:
    """抓取单个词条: 优先 wikitext(rest.php), 兜底渲染 HTML。"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.base_url = (self.config.get("base_url") or "https://vcpedia.cn").rstrip("/")
        cookie_dir = self.config.get("cookie_dir") or "data/song_knowledge"
        self.anubis = AnubisClient(
            base_url=self.base_url,
            cookie_file=Path(cookie_dir) / _ANUBIS_COOKIE_FILE,
            timeout=int(self.config.get("timeout", 20)),
        )

    def fetch_entity(self, entity_name: str) -> Optional[Dict[str, Any]]:
        """抓取并解析词条: 返回 {name, credits, introduction, lyrics, source}。"""
        data = self._fetch_wikitext(entity_name)
        source = ""
        if data:
            source = data.get("source") or ""
            if source:
                return self._parse_wikitext_entity(entity_name, source)
        # wikitext 不可用 → HTML 兜底
        html = self._fetch_html_page(entity_name)
        if not html:
            return None
        return self._parse_html_entity(entity_name, html)

    @staticmethod
    def _parse_wikitext_entity(name: str, source: str) -> Dict[str, Any]:
        lines = source.splitlines()
        credits = _parse_credits_from_lines(lines)
        introduction = _parse_introduction_from_source(source)
        lyrics = _parse_lyrics_from_source(source)
        # 若表格/内联里已解析出创作人员, 直接用
        return {
            "name": name,
            "credits": credits,
            "introduction": introduction,
            "lyrics": lyrics,
        }

    def _fetch_wikitext(self, entity_name: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/rest.php/v1/page/{quote(entity_name, safe='')}/source"
        try:
            resp = self.anubis.get(url)
            if resp.status_code == 200 and resp.text.strip():
                try:
                    return resp.json()
                except Exception:
                    return {"source": resp.text}
        except Exception as e:
            logger.debug(f"wikitext 抓取失败 {entity_name}: {e}")
        return None

    def _fetch_html_page(self, entity_name: str) -> Optional[str]:
        url = f"{self.base_url}/{quote(entity_name, safe='')}"
        try:
            resp = self.anubis.get(url)
            if resp.status_code == 200 and resp.text:
                return resp.text
        except Exception as e:
            logger.debug(f"HTML 抓取失败 {entity_name}: {e}")
        return None

    @staticmethod
    def _parse_html_entity(name: str, html: str) -> Dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        credits = _parse_credits_from_html(soup)
        year = _parse_year_from_html(soup)
        if year and "year" not in credits:
            credits["year"] = year
        return {
            "name": name,
            "credits": credits,
            "introduction": _parse_introduction_from_html(soup),
            "lyrics": _parse_lyrics_from_html(soup),
        }


def fetch_song_title_list(client: AnubisClient, base_url: str,
                          category: str = "Category:洛天依歌曲",
                          max_pages: int = 50) -> List[str]:
    """用 MediaWiki api.php list=categorymembers 全量分页拉歌曲标题。"""
    titles: List[str] = []
    seen: Set[str] = set()
    cmcontinue = ""
    api_url = f"{base_url}/api.php"
    for _ in range(max_pages):
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": category,
            "cmnamespace": "0",
            "cmlimit": "500",
            "format": "json",
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        resp = client.get(api_url, params=params)
        if resp.status_code != 200:
            logger.warning(f"VCPedia: api.php 返回 {resp.status_code}")
            break
        try:
            data = resp.json()
        except Exception as e:
            logger.warning(f"VCPedia: api.php JSON 解析失败: {e}")
            break
        members = (data.get("query") or {}).get("categorymembers") or []
        for member in members:
            title = (member.get("title") or "").strip()
            if title and title not in seen:
                seen.add(title)
                titles.append(title)
        cmcontinue = (data.get("continue") or {}).get("cmcontinue", "")
        if not cmcontinue:
            break
    return titles


def sync_vcpedia_new_songs(song_knowledge_config: Dict[str, Any]) -> Dict[str, Any]:
    """手动同步 VCPedia 歌曲(全量增量)。

    返回 {"added": [...], "failed": [...], "skipped": N}。
    同步后刷新 song_stats(供挂载判断/统计)。
    """
    song_db_cfg = song_knowledge_config.get("song_database") or {}
    if not song_db_cfg:
        raise ValueError("缺少 music_knowledge.song_database 配置")

    crawler_cfg = song_knowledge_config.get("crawler") or {}
    db_folder = song_db_cfg.get("db_folder", "./data/song_knowledge")
    db_file = song_db_cfg.get("db_file", "knowledge_db.db")

    ensure_init(db_folder, db_file)
    fetcher = VCPediaFetcher(crawler_cfg)

    titles = fetcher_song_titles(fetcher, crawler_cfg)
    if not titles:
        logger.warning("VCPedia: 未能获取歌曲列表(Anubis 挑战可能失败)")
        return {"added": [], "failed": [], "skipped": 0}

    added: List[str] = []
    failed: List[str] = []
    skipped = 0
    interval = float(crawler_cfg.get("interval", 0.8))
    max_fail = int(crawler_cfg.get("max_fail", 30))

    with get_session() as db:
        for name in titles:
            if _song_exists(db, name):
                skipped += 1
                continue
            try:
                entity = fetcher.fetch_entity(name)
                if not entity or not (entity.get("introduction") or entity.get("lyrics")):
                    failed.append(name)
                    max_fail -= 1
                    if max_fail <= 0:
                        logger.warning("VCPedia: 连续失败过多, 提前停止")
                        break
                    continue
                db.add(build_song_record(
                    name, entity.get("credits") or {},
                    entity.get("introduction") or "",
                    entity.get("lyrics") or "",
                ))
                db.commit()
                added.append(name)
                max_fail = 30  # 成功一次重置失败计数
            except Exception as e:
                db.rollback()
                logger.warning(f"VCPedia: 入库失败 {name}: {e}")
                failed.append(name)
                max_fail -= 1
                if max_fail <= 0:
                    break
            _time.sleep(interval)
        update_song_stats(db)

    logger.info(f"VCPedia 同步完成: 新增 {len(added)}, 失败 {len(failed)}, 跳过 {skipped}")
    return {"added": added, "failed": failed, "skipped": skipped}


def fetcher_song_titles(fetcher: VCPediaFetcher, crawler_cfg: Dict[str, Any]) -> List[str]:
    """从分类页拉全量歌曲标题(可被测试/直接调用)。"""
    category = crawler_cfg.get("category", "Category:洛天依歌曲")
    return fetch_song_title_list(fetcher.anubis, fetcher.base_url, category=category)