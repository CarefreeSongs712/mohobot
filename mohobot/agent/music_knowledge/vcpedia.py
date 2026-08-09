"""VCPedia 新歌同步 — 移植自 Agent-LuoTianyi (world/get_new_songs/)。

仅手动触发(脚本 scripts/sync_vcpedia.py 或 /sync-songs 命令), 无定时器。
流程: 抓取当年洛天依模板页歌名列表 → 逐首抓取 VCPedia 词条页
(简介/歌词/UP主/演唱) → 写入 songs 表 + 追加关键词 txt。
LLM 摘要默认关闭(use_llm=false, 使用页面原文截断摘要)。
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests
from bs4 import BeautifulSoup
from loguru import logger

from mohobot.agent.music_knowledge.song_database import Song, get_song_session, init_song_db

CURRENT_YEAR = datetime.datetime.now().year
TEMPLATE_URL = f"https://vcpedia.cn/Template:%E6%B4%9B%E5%A4%A9%E4%BE%9D/{CURRENT_YEAR}"


# ── 抓取工具 ──────────────────────────────────────────────────

def _is_bot_challenge(status_code: int, html: str) -> bool:
    if status_code == 403:
        return True
    text = (html or "").lower()
    markers = [
        "making sure you're not a bot",
        "正在确认你是不是机器",
        "within.website",
        "xess.min.css",
        "anubis",
        "techaro",
    ]
    return any(m in text for m in markers)


def _fetch_html(url: str, headers: Dict[str, str], timeout: int = 20) -> str:
    """抓取页面; requests 命中反爬时尝试 curl 兜底。"""
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    if not _is_bot_challenge(r.status_code, r.text):
        r.raise_for_status()
        return r.text

    curl_path = shutil.which("curl") or shutil.which("curl.exe")
    if not curl_path:
        r.raise_for_status()

    logger.warning("requests 命中站点反爬挑战，改用 curl 兜底抓取。")
    result = subprocess.run(
        [curl_path, "-sS", "-L", "--max-time", str(timeout), url],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl fallback failed: {result.stderr.strip()}")
    html = result.stdout or ""
    if not html.strip():
        raise RuntimeError("curl fallback returned empty response")
    if _is_bot_challenge(200, html):
        raise RuntimeError("curl fallback still got anti-bot challenge page")
    return html


def fetch_song_list_from_template(url: str, timeout: int = 20) -> List[str]:
    """从模板页提取歌曲名(过滤分类/模板/分组标题等结构词)。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        )
    }
    html = _fetch_html(url, headers=headers, timeout=timeout)
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find("div", id="mw-content-text") or soup

    bad_exact: Set[str] = {
        "原创曲", "非原创曲", "传说曲", "殿堂曲", "部分", "25万以上", "25万以下",
        "模板文档", "查看", "编辑", "历史", "刷新",
        "简体", "繁體", "大陆简体", "香港繁體", "臺灣正體", "不转换",
        "跳转到导航", "跳转到搜索", "洛天依",
        "bilibili", "ACE Studio", "X studio",
        "VOCALOID中文殿堂曲", "ACE殿堂曲", "文档", "嵌入",
    }
    for y in range(2012, CURRENT_YEAR + 1):
        bad_exact.add(str(y))
    bad_contains = ["Template:", "模板:", "分类:", "Category:", "帮助", "首页",
                    "随机页面", "最近更改", "殿堂曲", "传说曲"]

    seen: Set[str] = set()
    songs: List[str] = []
    for a in content.find_all("a"):
        text = a.get_text(strip=True)
        if not text:
            continue
        if text in bad_exact:
            continue
        if any(x in text for x in bad_contains):
            continue
        if text.isdigit():
            continue
        href = a.get("href", "") or ""
        if not href or href.startswith("#"):
            continue
        if "action=" in href:
            continue
        if "Template:" in href or "Category:" in href or "分类:" in href:
            continue

        text = text.rstrip("*").strip()
        if not text:
            continue
        if text not in seen:
            seen.add(text)
            songs.append(text)

    logger.info(f"从模板页提取到 {len(songs)} 个条目")
    return songs


# ── 词条解析 ──────────────────────────────────────────────────

class VCPediaFetcher:
    """抓取单个词条页: infobox(UP主/演唱) + 简介 + 歌词。"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.base_url = (self.config.get("base_url") or "https://vcpedia.cn").rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0 Safari/537.36"
            )
        })

    def fetch_entity_description(self, entity_name: str) -> Optional[Dict[str, Any]]:
        """抓取并解析词条页, 返回结构化数据; 失败返回 None。"""
        html = self._fetch_page(entity_name)
        if not html:
            return None
        try:
            data = self._parse_page(html, entity_name)
            if data and data.get("type") == "Song":
                return data
        except Exception as e:
            logger.error(f"Error parsing {entity_name}: {e}")
        return None

    def _fetch_page(self, page_name: str) -> Optional[str]:
        from urllib.parse import quote
        url = f"{self.base_url}/{quote(page_name)}"
        try:
            response = self.session.get(url, timeout=10)
            if response.status_code == 200:
                return response.text
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
        return None

    def _get_data_from_infobox(self, infobox_table, single_col: bool = False) -> Dict[str, str]:
        infobox_data: Dict[str, str] = {}
        rows = infobox_table.find_all("tr")
        preserved_title = None

        def get_title(text: str) -> Optional[str]:
            for kw in ["演唱", "作词", "作曲", "编曲", "作编曲", "PV", "UP主", "曲绘"]:
                if kw in text:
                    return kw
            return None

        for row in rows:
            if "display:none" in row.get("style", ""):
                continue
            cols = row.find_all(["th", "td"])
            if len(cols) == 2:
                key = cols[0].get_text(strip=True)
                val_col = cols[1]
                for br in val_col.find_all("br"):
                    br.replace_with(",")
                infobox_data[key] = val_col.get_text(strip=True)
            elif len(cols) == 1 and single_col:
                col = cols[0]
                if "infobox-image-container" in col.get("class", []):
                    continue
                text = col.get_text(strip=True)
                if not text:
                    continue
                if preserved_title is None:
                    preserved_title = get_title(text)
                else:
                    infobox_data[preserved_title] = text
                    preserved_title = None
        return infobox_data

    def _parse_page(self, html: str, title: str) -> Dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        infobox_data: Dict[str, str] = {}
        infobox_table = soup.find("table", class_="moe-infobox infobox")
        if infobox_table:
            infobox_data.update(self._get_data_from_infobox(infobox_table, single_col=True))

        summary: List[str] = []
        short_summary: List[str] = []
        intro_header = []
        lyc_header = None
        for h2 in soup.find_all("h2"):
            text = h2.get_text()
            if "简介" in text or "VOCALOID原创作者" in text:
                intro_header.append(h2)
            elif "歌词" in text:
                lyc_header = h2
        if not intro_header:
            intro_header = [soup.find("h2")] if soup.find("h2") else []

        if intro_header:
            for header in intro_header:
                if header is None:
                    continue
                summary_parts: List[str] = []
                last_was_a = False
                for sibling in header.next_siblings:
                    if getattr(sibling, "name", None) in ("h2", "h3"):
                        break
                    name = getattr(sibling, "name", None)
                    if name in ("p", None, "a"):
                        text = getattr(sibling, "get_text", lambda: "")()
                        text = (text or "").strip()
                        if not text:
                            continue
                        text = re.sub(r"截至[^。！？\n]*?收藏", "", text).strip()
                        if summary_parts and (last_was_a or name == "a"):
                            summary_parts[-1] += text
                        else:
                            summary_parts.append(text)
                        last_was_a = name == "a"
                    elif name in ("ul", "ol"):
                        for li in sibling.find_all("li"):
                            text = li.get_text(strip=True)
                            if text:
                                summary_parts.append(text)
                    elif name == "div":
                        table = sibling.find("table")
                        if table:
                            infobox_data.update(
                                self._get_data_from_infobox(table, single_col=True)
                            )
                summary.append("\n".join(summary_parts))
                short_summary.append("\n".join(summary[-1].split("\n")[:3]))

        song_type = "Song" if lyc_header else "Person"
        lyrics = ""
        if lyc_header:
            poem = None
            for sibling in lyc_header.next_siblings:
                if getattr(sibling, "name", None) == "table":
                    if sibling.get("class", []) == ["navbox"]:
                        break
                    break
                if getattr(sibling, "name", None) == "div":
                    nt = sibling.find("table")
                    if nt and nt.get("class", []) != ["navbox"]:
                        break
            for sibling in lyc_header.next_siblings:
                if getattr(sibling, "name", None) != "div":
                    continue
                if "poem" in sibling.get("class", []):
                    poem = sibling
                    break
                if sibling.get("class", []) in (["Tabs"], ["tabLabelTop"]):
                    poem = sibling.find("div", class_="poem")
                    break
            if poem:
                p_tag = poem.find("p")
                if p_tag:
                    span_tags = p_tag.find_all("span")
                    if span_tags:
                        lyrics = "".join(
                            span.get_text() for span in span_tags
                        )
                    else:
                        lyrics = p_tag.get_text()
                    lyrics = lyrics.replace("\u3000", " ")
                    lyrics = re.sub(r"\[.*?\]|\(.*?\)|（.*?）|【.*?】", "", lyrics, flags=re.S)
                    lyrics = re.sub(r"\s+", " ", lyrics).strip()

        return {
            "name": title,
            "type": song_type,
            "infobox": infobox_data,
            "summary": summary,
            "lyrics": lyrics.strip(),
            "spaced_lyrics": lyrics,
        }


# ── 入库 ──────────────────────────────────────────────────────

def _safe_song_name(name: str) -> str:
    return "".join([c for c in name if c.isalnum() or c in (" ", "-", "_")]).strip()


def _song_exists(db, song_name: str) -> bool:
    safe_name = _safe_song_name(song_name)
    return db.query(Song).filter(
        (Song.name == song_name) | (Song.safe_name == safe_name)
    ).first() is not None


def _extract_song_fields(data: Dict[str, Any]) -> Dict[str, str]:
    infobox = data.get("infobox") or {}
    uploader = infobox.get("UP主") or infobox.get("投稿者") or infobox.get("发布者") or ""
    singers = infobox.get("演唱") or infobox.get("歌手") or infobox.get("演唱者") or ""

    short_summary = data.get("short_summary") or ""
    if isinstance(short_summary, list):
        short_summary = "\n".join(str(x) for x in short_summary if x)
    short_summary = str(short_summary).strip()
    if not short_summary:
        summary = data.get("summary") or []
        if isinstance(summary, list):
            short_summary = "\n".join(str(x) for x in summary if x)[:200].strip()
        else:
            short_summary = str(summary).strip()[:200]

    return {
        "uploader": uploader,
        "singers": singers,
        "introduction": short_summary,
        "lyrics": str(data.get("lyrics") or "").strip(),
        "spaced_lyrics": str(data.get("spaced_lyrics") or ""),
    }


def _split_spaced_lyrics(spaced_lyrics: str) -> List[str]:
    parts = re.split(r"[\n\r\s]+", spaced_lyrics or "")
    ret = []
    for part in parts:
        cleaned = part.strip()
        if 6 <= len(cleaned) <= 50:
            ret.append(cleaned)
    return ret


def do_one_song(db, fetcher: VCPediaFetcher, song_name: str,
                songname_file: Path, lyric_file: Path, update: bool = False) -> bool:
    """抓取一首歌并入库(含关键词 txt 追加)。"""
    if db and _song_exists(db, song_name) and not update:
        logger.info(f"已存在, 跳过: {song_name}")
        return False

    logger.info(f"开始抓取并入库: {song_name}")
    data = fetcher.fetch_entity_description(song_name)
    if not data:
        return False
    fields = _extract_song_fields(data)
    if not fields["introduction"]:
        return False

    if db is not None:
        try:
            if _song_exists(db, song_name):
                db.query(Song).filter(
                    (Song.name == song_name) |
                    (Song.safe_name == _safe_song_name(song_name))
                ).delete()
                db.commit()
            db.add(Song(
                name=song_name,
                safe_name=_safe_song_name(song_name),
                uploader=fields["uploader"],
                singers=fields["singers"],
                introduction=fields["introduction"],
                lyrics=fields["lyrics"],
            ))
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"入库失败 {song_name}: {e}")
            return False

    try:
        songname_file.parent.mkdir(parents=True, exist_ok=True)
        with open(songname_file, "a", encoding="utf-8") as f:
            f.write(f"{song_name}\n")
        lyric_lines = _split_spaced_lyrics(fields["spaced_lyrics"])
        if lyric_lines:
            with open(lyric_file, "a", encoding="utf-8") as f:
                for lyric in lyric_lines:
                    f.write(f"{lyric}=>{lyric}是《{song_name}》的歌词\n")
    except Exception as e:
        logger.error(f"关键词写入失败 {song_name}: {e}")

    return True


def sync_vcpedia_new_songs(song_knowledge_config: Dict[str, Any]) -> Dict[str, List[str]]:
    """手动同步 VCPedia 新歌(当年洛天依模板页)。返回 {added, failed}。"""
    song_db_cfg = song_knowledge_config.get("song_database") or {}
    if not song_db_cfg:
        raise ValueError("缺少 music_knowledge.song_database 配置")

    crawler_cfg = song_knowledge_config.get("crawler") or {}
    data_dir = Path(song_db_cfg.get("db_folder", "./data/song_knowledge"))
    songname_file = data_dir / "song_name_keywords.txt"
    lyric_file = data_dir / "song_lyric_keywords.txt"
    if song_knowledge_config.get("songname_file"):
        songname_file = Path(song_knowledge_config["songname_file"])
    if song_knowledge_config.get("lyric_file"):
        lyric_file = Path(song_knowledge_config["lyric_file"])

    init_song_db(song_db_cfg)
    db = get_song_session()
    added: List[str] = []
    failed: List[str] = []
    try:
        songs = fetch_song_list_from_template(
            crawler_cfg.get("template_url") or TEMPLATE_URL
        )
        fetcher = VCPediaFetcher(crawler_cfg)
        for song_name in songs:
            if do_one_song(db, fetcher, song_name, songname_file, lyric_file):
                added.append(song_name)
            else:
                failed.append(song_name)
            _time.sleep(0.8)
        return {"added": added, "failed": failed}
    finally:
        db.close()
