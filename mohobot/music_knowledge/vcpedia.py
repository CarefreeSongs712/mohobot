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


def _is_challenge_body(html: str) -> bool:
    """判断某响应体是否为 Anubis 挑战页(DOM 含 anubis_challenge 脚本)。"""
    return bool(html) and f'id="{_CHALLENGE_SCRIPT_ID}"' in html


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
        m = re.search(rf'<script id="{_CHALLENGE_SCRIPT_ID}"\s*[^>]*>(.*?)</script>', html, re.S)
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
        """完整走一遍: GET 挑战页 → 解 PoW → pass-challenge → 取 auth cookie。

        注意: 站点根路径会 301 重定向到 /首页, 挑战页(403 + Anubis)在重定向
        后的 URL 上, 因此这里跟随重定向再抓取挑战。
        """
        sess = requests.Session()
        sess.headers.update({"User-Agent": USER_AGENT})
        sess.headers.update({"Accept": "text/html,application/xhtml+xml,*/*"})
        # 命中挑战页的入口: 主页(跟随重定向到 /首页 或 /wiki/首页, 返回 403)
        target = self.base_url + "/"
        r = sess.get(target, timeout=self.timeout, allow_redirects=True)
        challenge_url = r.url
        if r.status_code != 403 or not _is_challenge_body(r.text):
            # 反爬可能临时关闭: 无挑战, 直接以当前会话请求即可
            # (仍保存 session 用的 cookies —— 但通常无权; 返回让上层重试/失败)
            logger.warning(
                f"VCPedia: 首页返回 {r.status_code}, 未命中 Anubis 挑战(可能反爬关闭)"
            )
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

        # 挑战过程中服务端会下发验证 cookie(Partitioned; SameSite=None), 保留到 pass 请求
        cookies = dict(sess.cookies)
        pass_url = (
            f"{self.base_url}/.within.website/x/cmd/anubis/api/pass-challenge"
            f"?id={quote(ch['id'])}&response=1&nonce={nonce}"
            f"&redir={quote(challenge_url)}&elapsedTime={_time.time() * 1000:.0f}"
        )
        resp = sess.get(
            pass_url, timeout=self.timeout, allow_redirects=False,
            headers={"Referer": challenge_url},
        )
        if resp.status_code not in (200, 302):
            logger.warning(f"VCPedia: pass-challenge 返回 {resp.status_code}")
            sess.close()
            return
        # 从 Set-Cookie 提取 auth cookie(可能在此响应, 也可能在下一次请求带上)
        self._pick_auth_cookie(sess.cookies)
        if self.auth_cookie:
            logger.info("VCPedia: Anubis PoW 解题成功, 已取得 auth cookie")
            self._save_cookie()
        else:
            logger.warning("VCPedia: pass-challenge 后未找到 auth cookie")
        sess.close()

    def _pick_auth_cookie(self, jar) -> None:
        """从 cookie jar 中挑出 auth cookie(域名不限定, 找 anubis-auth 名)。"""
        for name, value in jar.items():
            if "anubis-auth" in name:
                self.cookie_name = name
                self.auth_cookie = value
                return

    # ── 对外请求 ───────────────────────────────────────────────

    def get(self, url: str, **kwargs) -> requests.Response:
        """带 auth cookie 的 GET(402/403 时自动重解一次)。"""
        timeout = kwargs.pop("timeout", self.timeout)
        if not self._load_cookie() and not self.auth_cookie:
            self._fetch_cookie()
            if not self.auth_cookie:
                logger.warning(
                    "VCPedia: 无法取得 Anubis auth cookie, 将按无 cookie 请求"
                    "(若站点反爬未关闭, 请求会 403)"
                )
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
    "lyricist": ["作词", "词作", "作詞", "填词", "填詞"],
    "composer": ["作曲", "曲作", "作曲者"],
    "arranger": ["编曲", "编曲者", "編曲"],
    "mixer": ["混音", "混合", "remix", "混音后期"],
    "tuner": ["调教", "调校", "调声", "調教", "VOCALOID调教"],
    "mastering": ["母带", "母带处理"],
    "pv": ["PV", "视频制作", "映像", "影片", "MV编导", "MV制作"],
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
            m = re.match(r"^([^=：:]{1,16})\s*[=：:]\s*(.+)$", token)
            if m:
                key_raw, val_raw = m.group(1).strip(), m.group(2).strip()
                key = _normalize_credit_key(key_raw)
                if key and key not in credits:
                    val = _clean_credit_values(val_raw)
                    if val and len(val) < 200:
                        credits[key] = val
                        break  # 每行只取第一个命中键(防一行内多个键串扰)
        # 年份: 页面标题/内容中 "2025年X月X日投稿"
        if "year" not in credits:
            m = re.search(r"(20\d{2})\s*年", line)
            if m:
                credits["year"] = m.group(1)
    return credits


def _clean_credit_values(v: str) -> str:
    """清洗创作人员值: 模板内可能用 <br/> 分隔多人, 归一为顿号分隔。"""
    v = v.replace("<br/>", "、").replace("<br>", "、")
    v = _clean_value(v)
    # 多个值取全部(如 "平安夜的噩梦／H.K.君"), 不要只取第一个
    v = v.replace("／", "/")
    return v


def _normalize_credit_key(raw: str) -> str:
    """把 wikitext 键名规范化到 CREDIT_ALIASES 的标准键。

    真实词条中键常带 <br/> 复合, 如 "作编曲<br/>作词<br/>吉他<br/>混音":
    取第一个 "键" 前的内容; 若含 "编曲" 则归为 arranger, "作词" 归 lyricist。
    """
    raw0 = raw.split("<br")[0].strip()
    for key, aliases in _CREDIT_ALIASES.items():
        for a in aliases:
            if raw0 == a or raw0.startswith(a):
                return key
    # 复合键: 取第一个别名(作编曲→编曲方向)
    for key, aliases in _CREDIT_ALIASES.items():
        for a in aliases:
            if raw.startswith(a):
                return key
    return ""


def _parse_lyrics_from_source(source: str) -> str:
    """从 wikitext 提取完整歌词。

    VCPedia 歌词章节标题并不统一(== 歌词 == / == 普通的歌词 == / ==歌词及人设==),
    歌词多用 <poem>...</poem> 包裹(行内可能有 {{color|样式|歌词}} / {{交叉颜色|...|歌词}}
    等模板), 少数页面(如 九九八十一(乐正绫))用 {{LyricsKai|...|original=...}} 模板。

    只截取到下一个同级标题为止——"== 二次创作 ==" 章节里收录了所有衍生作品的
    歌词, 若一并抓取会把几十首翻唱词灌进原曲(如达拉崩吧 2.2w 字符)。
    """
    # 定位歌词章节(标题含 "歌词")
    m = re.search(r"^={2,4}\s*[^=\n]*歌词[^=\n]*\s*={2,4}\s*$", source, re.M)
    if not m:
        return ""
    level = len(m.group(0)) - len(m.group(0).lstrip("="))
    tail = source[m.end():]
    # 截到下一个同级(或更高级)标题, 如 "== 二次创作 =="
    nxt = re.search(rf"^={{1,{level}}}\s+[^=\n]", tail, re.M)
    if nxt:
        tail = tail[:nxt.start()]
    # 取歌词章节内的 <poem>..</poem>(可能有多个诗节)
    poems = list(re.finditer(r"<poem>(.*?)</poem>", tail, re.S))
    if poems:
        texts = [_unwrap_poem(pm.group(1)) for pm in poems]
        text = "\n\n".join(t for t in texts if t.strip())
    else:
        text = _extract_lyricskai(tail)
    # 去掉注释/引用/残留模板/全角空格/<br>
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = text.replace("\u3000", " ").strip()
    # 合并连续空行(保留单空行分段)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _extract_lyricskai(tail: str) -> str:
    """从 {{LyricsKai|...|original=...}} 模板提取歌词(VCPedia 一部分页面用该模板)。

    九九八十一(乐正绫) 等页面的歌词在 LyricsKai 的 original= 参数里,
    行内是 {{color|black|-{歌词}-}} 形式; 取 original= 到模板结束,
    展开 color 模板并去掉 -{-}/-} 转义括号。
    """
    m = re.search(r"\{\{LyricsKai\b", tail)
    if not m:
        return ""
    # 找模板闭合: depth 从 1 起(计入 LyricsKai 自身的 {{), 忽略 -{-}/-} 花括号对
    depth = 1
    end = len(tail)
    i = m.start() + 2
    while i < len(tail):
        if tail.startswith("{{", i):
            depth += 1
            i += 2
        elif tail.startswith("}}", i):
            depth -= 1
            if depth == 0:
                end = i
                break
            i += 2
        else:
            i += 1
    if depth != 0:
        return ""
    body = tail[m.start() + 2 : end]
    oi = body.find("|original=")
    if oi < 0:
        return ""
    lines = body[oi + len("|original="):].split("\n")
    out: List[str] = []
    for ln in lines:
        # 遇到后续命名参数(如 |translated=)且已有歌词时截断
        if out and re.match(r"^\s*\|[a-zA-Z]+\s*=", ln):
            break
        out.append(ln)
    text = "\n".join(out)
    # 展开行内模板({{color|black|-{歌词}-}}): 逐字符扫描处理嵌套模板
    text = _expand_inline_templates(text)
    text = text.replace("-{", "").replace("}-", "")
    return text


def _expand_inline_templates(text: str) -> str:
    """把歌词里的行内模板 {{color|样式|正文}} / {{ruby|...|正文}} 展开为正文。

    逐字符扫描并处理嵌套(如 {{color|black|-{歌词}-}} 内含 -{ }- 花括号),
    模板正文取第一个非参数段之后的全部内容, 去掉 -{-}/-} 转义括号。
    """
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("{{", i):
            j = i + 2
            depth = 1
            while j < n and depth:
                if text.startswith("{{", j):
                    depth += 1
                    j += 2
                elif text.startswith("}}", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth != 0:
                break
            body = text[i + 2 : j - 2]
            out.append(_template_tail(body))
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _unwrap_poem(inner: str) -> str:
    """展开 <poem> 内部内容: 处理 {{color|样式|正文}} / {{交叉颜色|a|b|正文}} 等。

    策略: 按括号深度拆块; 只有 **模板外** 的 '|' 才是歌词段落分隔;
    模板内的 '|' 由 _template_tail 统一处理(取样式参数后的正文)。
    """
    out: List[str] = []
    buf = ""
    depth = 0
    i = 0
    while i < len(inner):
        if inner.startswith("{{", i):
            depth += 1
            buf += "{{"
            i += 2
            continue
        if inner.startswith("}}", i):
            depth -= 1
            buf += "}}"
            i += 2
            if depth == 0:
                out.append(_template_tail(buf))
                buf = ""
            continue
        if depth == 0 and inner[i] == "|":
            # 模板外 '|' = 歌词段落分隔
            if buf.strip():
                out.append(buf)
            buf = ""
            i += 1
            continue
        buf += inner[i]
        i += 1
    if buf.strip():
        out.append(buf)
    # 拼接: 每个缓冲为一段; 丢弃样式残留
    merged: List[str] = []
    for piece in out:
        if not piece:
            continue
        for seg in piece.split("\n"):
            seg = seg.strip()
            if not seg:
                continue
            if _is_template_junk(seg):
                continue
            merged.append(seg)
    text = "\n".join(merged)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _is_template_junk(seg: str) -> bool:
    """判断歌词段落是否为模板样式残留(应丢弃)。

    - 残留的模板名(交叉颜色 / color)与样式参数形如 c1=#66ccff、#66ccff、
      text-shadow:0 0 4px、transparent 等(不含中文且长度有限)。
    """
    s = seg.strip()
    if not s:
        return True
    if s in ("交叉颜色", "color", "颜色", "ruby", "ps"):
        return True
    if re.match(r"^[#\w;:,.()\- ]+$", s) and len(s) <= 60 and not re.search(r"[\u4e00-\u9fff]", s):
        return True
    return False


def _template_tail(tpl: str) -> str:
    """把 {{color|样式|正文}} / {{交叉颜色|c1=..|c2=..|正文}} 里的正文取出。

    策略(针对实际 VCPedia 形态):
    - {{color|样式|正文}}           → 正文 = 第 3 段(样式在第 2 段)
    - {{交叉颜色|c1=|c2=|正文...}} → 正文 = 第 4 段及之后(前 3 段是参数)
    模板名称本身(第 1 段)总是跳过; 被跳过的参数段为纯样式/含 '='/空。
    """
    body = tpl[2:-2]  # 去掉 {{ }}
    if "|" not in body:
        return ""
    parts = body.split("|")

    def is_param(p: str) -> bool:
        p = p.strip()
        if not p:
            return True
        if "=" in p:
            return True
        # 纯色值/样式/数字(不含中文与非样式符号)
        if re.match(r"^[#\w;:,.()\- ]+$", p) and not re.search(r"[\u4e00-\u9fff]", p) \
                and len(p) <= 80:
            return True
        return False

    # 跳过模板名(parts[0])
    start = 1
    # 再跳过参数段(纯样式/含 '=' / 空)
    while start < len(parts) and is_param(parts[start]):
        start += 1
    if start >= len(parts):
        return ""
    return "|".join(parts[start:]).strip(" \n|")


def _parse_introduction_from_source(source: str) -> str:
    """从 wikitext 提取简介(简介章节, 样式化标题如 普通的简介 也能命中)。"""
    m = re.search(r"^={2,4}\s*[^=\n]*简介[^=\n]*\s*={2,4}\s*$", source, re.M)
    if not m:
        return ""
    tail = source[m.end():]
    lines: List[str] = []
    for line in tail.splitlines():
        if re.match(r"^\s*={2,4}\s*[^=\n]*\s*={2,4}\s*$", line):
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
        """优先走 api.php prop=revisions 取 wikitext(rest.php 在该站被禁)。"""
        from urllib.parse import quote as _q
        url = (
            f"{self.base_url}/api.php?action=query&prop=revisions&rvprop=content"
            f"&rvslots=main&format=json&titles={_q(entity_name, safe='')}"
        )
        try:
            resp = self.anubis.get(url)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    pages = ((data.get("query") or {}).get("pages") or {})
                    for page in pages.values():
                        revs = page.get("revisions") or []
                        if revs:
                            slot = revs[0].get("slots", {}).get("main", {})
                            return {"source": slot.get("*", "")}
                except Exception as e:
                    logger.debug(f"wikitext JSON 解析失败 {entity_name}: {e}")
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