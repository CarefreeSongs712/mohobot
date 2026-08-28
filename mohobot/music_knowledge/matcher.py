"""全局歌曲信息匹配器 — 歌名/歌词识别 + LLM 前注入注解格式化。

在消息交给 LLM 之前调用(私聊+群聊, legacy 与 agent 两条路径共用):
1. 歌名匹配(优先, 高置信):
   - 《歌名》(书名号) → 直接查库
   - 裸文本含歌名(长度>=min_song_name_len)且消息中含歌曲语境词 → 查库
2. 歌词匹配(歌名未命中): 取消息最长连续文本段, 均匀采样 2-3 个
   12-20 字子串, 对内存中 (name, lyrics) 列表做 substring 包含检测
   (直接和爬取库比对, 不再依赖 song_lyric_keywords.txt)。
3. 命中后从 SQLite 取完整详情, 格式化为【歌曲信息】注解段
   (歌曲介绍 + 演唱/UP主 + 词/曲/混/调等 + 完整歌词)。

不写入 context 文件(仅请求级注入); 库为空/匹配失败 → 返回 None, 正常走 LLM。
"""

from __future__ import annotations

import re
import threading
from typing import Dict, List, Optional, Tuple

from loguru import logger

from mohobot.music_knowledge.knowledge_service import (
    CREDIT_KEYS,
    _escape_like,
    get_song_detail,
)
from mohobot.music_knowledge.pool import ensure_init, get_session
from mohobot.music_knowledge.song_database import Song

# 歌曲语境词(裸文本歌名匹配时要求命中其一, 防日常对话误伤)
_SONG_CONTEXT_WORDS = (
    "歌", "唱", "听", "点", "循环", "安利", "写", "作曲", "编曲", "调教", "歌词", "在放", "好喜欢", "来", "首",
)


def _strip_cq_codes(text: str) -> str:
    """去掉 CQ 码(CQ:image,xxx 等), 避免把文件路径当正文。"""
    return re.sub(r"\[CQ:[^\]]*\]", "", text or "")


def _strip_play_prefix(text: str) -> str:
    """去掉"唱一首/点一首/来一首"等点歌前缀(便于裸歌名包含匹配)。

    仅去除走势明显的动词语气前缀, 保留正文; 同时处理前缀与歌名之间的
    "唱一首达拉崩吧、唱首xxx" 等(前缀本身有停用词, 去掉后剩余即歌名)。
    若无前缀则原样返回。
    """
    s = text.strip()
    for prefix in ("帮我唱一首", "给我唱一首", "帮我唱个", "帮我唱", "唱一首", "点一首", "唱个", "来一首", "点个", "唱首", "来首"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    # 去掉"这首歌/再来首"等尾部停用词("唱一首这首歌啥啥" → 歌名前还有"这首歌")
    for trailing in ("这首歌", "这首歌名", "这个歌曲"):
        if s.endswith(trailing):
            s = s[:-len(trailing)].strip()
            break
    return s


def _sample_substrings(text: str, min_len: int = 8, max_len: int = 20, count: int = 3) -> List[str]:
    """从纯文本中取最长连续段, 均匀采样 count 个子串(保留换行, 歌词断行也命中)。"""
    clean = re.sub(r"[ \t]+", " ", text or "").strip()
    # 用整段做滑窗采样(不抹掉换行; 歌词行以换行分隔, 采样可覆盖行内片段)
    if len(clean) <= max_len:
        return [clean] if len(clean) >= min_len else []
    step = max(1, (len(clean) - max_len) // max(1, count - 1))
    samples: List[str] = []
    seen: set[str] = set()
    for i in range(count):
        start = min(i * step, len(clean) - max_len)
        piece = clean[start:start + max_len]
        if piece not in seen:
            seen.add(piece)
            samples.append(piece)
    return samples


class SongMatch:
    """一次歌曲识别结果。"""

    def __init__(self, name: str, detail: Dict[str, str]):
        self.name = name
        self.detail = detail

    def build_annotation(self) -> str:
        """格式化为【歌曲信息】注解段(贴用户消息下方发送给 LLM)。"""
        d = self.detail
        lines: List[str] = [f"【歌曲信息】消息中提到歌曲《{d.get('name') or self.name}》"]
        intro = (d.get("introduction") or "").strip()
        if intro:
            lines.append(f"歌曲介绍：{intro}")

        staff: List[str] = []
        if d.get("singers"):
            staff.append(f"演唱：{d['singers']}")
        if d.get("uploader"):
            staff.append(f"UP主：{d['uploader']}")
        # 词/曲/编/混/调等创作人员(有值才列)
        for key in CREDIT_KEYS:
            label = {
                "lyricist": "作词", "composer": "作曲", "arranger": "编曲",
                "mixer": "混音", "tuner": "调教", "mastering": "母带",
                "pv": "PV", "illustrator": "曲绘",
            }[key]
            val = (d.get(key) or "").strip()
            if val:
                staff.append(f"{label}：{val}")
        if staff:
            lines.append(" | ".join(staff))

        lyrics = (d.get("lyrics") or "").strip()
        if lyrics:
            lines.append("完整歌词：")
            lines.append(lyrics)
        return "\n".join(lines)


class SongInfoMatcher:
    """全局歌曲匹配器(DB 直查 + 内存歌词索引 + 短时缓存)。"""

    def __init__(
        self,
        db_folder: str = "./data/song_knowledge",
        db_file: str = "knowledge_db.db",
        *,
        min_song_name_len: int = 3,
        lyric_min_len: int = 8,
        cache_size: int = 256,
    ):
        self._db_folder = db_folder
        self._db_file = db_file
        self._min_song_name_len = min_song_name_len
        self._lyric_min_len = lyric_min_len
        ensure_init(db_folder, db_file)
        # 内存歌词索引: [(name, lyrics), ...](懒加载, 同步后 reload)
        self._index: List[Tuple[str, str]] = []
        self._index_loaded = False
        # 短时匹配缓存: msg -> SongMatch(避免同文案反复查库)
        self._cache: Dict[str, SongMatch] = {}
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._ensure_index()

    # ── 索引(歌词匹配用) ───────────────────────────────────────

    def reload_index(self) -> None:
        """重新加载全部 (歌名, 歌词) 到内存(同步/迁移后调用)。"""
        try:
            with get_session() as db:
                rows = db.query(Song.name, Song.lyrics).all()
                self._index = [(name, lyrics or "") for name, lyrics in rows]
            self._index_loaded = True
            logger.info(f"歌曲歌词索引已加载: {len(self._index)} 首")
        except Exception as e:
            logger.warning(f"歌曲歌词索引加载失败: {e}")
            self._index = []
            self._index_loaded = True

    def _ensure_index(self) -> None:
        if not self._index_loaded:
            self.reload_index()

    # ── 识别 ───────────────────────────────────────────────────

    def match(self, text: str | None) -> Optional[SongMatch]:
        """识别消息中的歌曲并返回行内命中结果(未命中返回 None)。"""
        if not text:
            return None
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        result = self._match_uncached(text)
        if result is not None:
            self._put_cache(text, result)
        return result

    def _match_uncached(self, text: str) -> Optional[SongMatch]:
        clean = _strip_cq_codes(text).strip()
        if not clean:
            return None

        # 1. 书名号歌名 → 最高置信
        m = re.search(r"《([^》]+)》", clean)
        if m:
            name = m.group(1).strip()
            if name:
                detail = self._query_detail(name)
                if detail:
                    return SongMatch(detail["name"], detail)

        # 2. 裸文本歌名(需歌曲语境词): 消息中去掉"唱一首/点一首/来一首"等
        #    点歌前缀后再做包含匹配(如 "唱一首九九八十一" → "九九八十一")。
        if any(w in clean for w in _SONG_CONTEXT_WORDS):
            candidate = _strip_play_prefix(clean)
            name = self._find_by_name(candidate)
            if name:
                detail = self._query_detail(name)
                if detail:
                    return SongMatch(detail["name"], detail)

        # 3. 歌词片段匹配
        hit = self._find_by_lyrics(clean)
        if hit:
            detail = self._query_detail(hit)
            if detail:
                return SongMatch(detail["name"], detail)

        return None

    def _find_by_name(self, text: str) -> Optional[str]:
        """在库内按歌名包含匹配(先精确后包含, 过滤过短歌名)。

        注意: 歌名里含 '.' / '·' / ':' 等符号时, 消息文本可能以不同写法出现,
        因此这里同时用 safe_name(去符号)做匹配; 但 SQL LIKE 的 ESCAPE 语义
        与部分站点字符需谨慎, 因此先做不区分大小写的全等, 再做包含。
        """
        try:
            with get_session() as db:
                from mohobot.music_knowledge.song_database import Song
                # 先精确(全等), 再包含; 分开查保证精确优先, 避免长名抢先
                exact = (
                    db.query(Song.name)
                    .filter((Song.name == text) | (Song.safe_name == text))
                    .first()
                )
                if exact is not None and len(exact[0]) >= self._min_song_name_len:
                    return exact[0]
                matched = (
                    db.query(Song.name)
                    .filter(Song.name.like(f"%{_escape_like(text)}%"))
                    .order_by(Song.name != text, Song.name)
                    .limit(5)
                    .all()
                )
                for (name,) in matched:
                    if len(name) >= self._min_song_name_len:
                        return name
        except Exception as e:
            logger.debug(f"歌名匹配失败: {e}")
        return None

    def _find_by_lyrics(self, text: str) -> Optional[str]:
        """歌词片段匹配: 内存索引 substring 检测(直接和爬取库比对)。

        歌词库中可能夹杂换行/空白(如 "刚擒住了几个妖\\n又降住了几个魔"),
        因此采样片段先按空白切词后, 逐段做包含检测(连续短句也能命中)。
        """
        self._ensure_index()
        if not self._index:
            return None
        for snippet in _sample_substrings(text, min_len=self._lyric_min_len):
            # 按空白切段: 对每个片段统计命中歌词的累计票数(片段越多越可信)
            parts = sorted(
                {p for p in re.split(r"\s+", snippet) if p},
                key=len, reverse=True,
            )
            if not parts:
                continue
            best: Optional[Tuple[str, int]] = None
            for name, lyrics in self._index:
                votes = sum(
                    1 for p in parts if len(p) >= 3 and p in lyrics
                )
                if votes and (best is None or votes > best[1]):
                    best = (name, votes)
            if best is not None and best[1] >= 2:
                return best[0]
        return None

    def _query_detail(self, name: str) -> Dict[str, str]:
        try:
            with get_session() as db:
                return get_song_detail(db, name)
        except Exception as e:
            logger.debug(f"歌曲详情查询失败 {name}: {e}")
            return {}

    # ── 缓存 ───────────────────────────────────────────────────

    def _put_cache(self, text: str, result: SongMatch) -> None:
        with self._lock:
            if len(self._cache) >= self._cache_size:
                # 简单淘汰: 清空重来(命中缓存暗示重复消息, 成本低)
                self._cache.clear()
            self._cache[text] = result

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()