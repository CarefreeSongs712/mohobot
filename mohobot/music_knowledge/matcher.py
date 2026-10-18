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

from mohobot.music_knowledge.fuzzy import LyricGramIndex, SongNameIndex
from mohobot.music_knowledge.knowledge_service import (
    CREDIT_KEYS,
    _escape_like,
    get_song_detail,
)
from mohobot.music_knowledge.normalize import bracket_core, normalize_name
from mohobot.music_knowledge.pool import ensure_init, get_session
from mohobot.music_knowledge.song_database import Song

# 歌曲语境词(裸文本歌名匹配时要求命中其一, 防日常对话误伤)
_SONG_CONTEXT_WORDS = (
    "歌", "唱", "听", "点", "循环", "安利", "写", "作曲", "编曲", "调教", "歌词", "在放", "好喜欢", "来", "首", "知道", "哪首",
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
    """一次歌曲识别结果。

    confidence/reason 区分"确定命中"与"模糊命中":
    - 1.0  exact  书名号/歌名精确
    - 0.95 core   去括号核名精确
    - 0.9  typo   歌名编辑距离 <= 1 且候选唯一
    - 0.8  lyric  歌词片段命中
    调用方按阈值决定是否注入, 保证"宁可漏报"的保守取舍。
    """

    def __init__(self, name: str, detail: Dict[str, str], *,
                 confidence: float = 1.0, reason: str = "exact",
                 lyric_span: str = "", ambiguous: bool = False):
        self.name = name
        self.detail = detail
        self.confidence = confidence
        self.reason = reason
        # 命中的歌词片段(注入时只给这几句附近)
        self.lyric_span = lyric_span
        self.ambiguous = ambiguous

    def build_annotation(self, *, max_chars: int = 0, lyric_context_lines: int = 4) -> str:
        """格式化为【歌曲信息】注解段(贴在用户消息下方发给 LLM)。

        max_chars > 0 时启用预算控制: 介绍按比例截断, 歌词只给命中片段
        前后若干行, 并提示可用 `song_get_lyrics` 取完整歌词。
        max_chars = 0 保持旧行为(完整歌词), 供工具与旧测试使用。
        """
        d = self.detail
        lines: List[str] = [f"【歌曲信息】消息中提到歌曲《{d.get('name') or self.name}》"]
        if self.reason != "exact":
            lines.append(f"识别方式：{self.reason}（置信度 {self.confidence:.2f}）")
        intro = (d.get("introduction") or "").strip()
        if intro:
            if max_chars > 0:
                intro = _truncate(intro, max(80, max_chars // 3))
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
            if max_chars > 0:
                excerpt = _lyric_excerpt(lyrics, self.lyric_span, lyric_context_lines)
                if excerpt:
                    lines.append("歌词片段（命中处附近）：")
                    lines.append(excerpt)
                else:
                    lines.append("歌词片段（开头）：")
                    lines.append(_truncate(lyrics, max(60, max_chars // 3)))
                lines.append("（需要完整歌词可调用 song_get_lyrics 工具）")
            else:
                lines.append("完整歌词：")
                lines.append(lyrics)

        text = "\n".join(lines)
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n…（歌曲资料已按长度预算截断）"
        return text


def _truncate(text: str, limit: int) -> str:
    """按字符截断, 尽量落在句末。"""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in ("。", "！", "？", "\n"):
        pos = cut.rfind(mark)
        if pos >= limit * 0.6:
            return cut[:pos + 1].rstrip()
    return cut.rstrip() + "…"


def _lyric_excerpt(lyrics: str, span: str, context_lines: int) -> str:
    """在完整歌词中定位命中片段, 返回其前后各 N 行。

    定位失败(用户输入与库内歌词存在错字/漏字)时退回按字符重叠找最相似行。
    """
    lines = (lyrics or "").splitlines()
    if not lines:
        return ""
    needle = "".join((span or "").split())
    hit_index = -1
    if needle:
        for index, line in enumerate(lines):
            if needle and needle in "".join(line.split()):
                hit_index = index
                break
    if hit_index < 0 and needle and len(needle) >= 4:
        best_score, best_index = 0.0, -1
        chars = set(needle)
        for index, line in enumerate(lines):
            flat = "".join(line.split())
            if not flat:
                continue
            score = len(chars & set(flat)) / max(1, len(chars))
            if score > best_score:
                best_score, best_index = score, index
        if best_score >= 0.6:
            hit_index = best_index
    if hit_index < 0:
        return ""
    start = max(0, hit_index - max(0, int(context_lines)))
    end = min(len(lines), hit_index + max(0, int(context_lines)) + 1)
    excerpt = "\n".join(line for line in lines[start:end] if line.strip())
    if start > 0:
        excerpt = "…\n" + excerpt
    if end < len(lines):
        excerpt = excerpt + "\n…"
    return excerpt


class SongInfoMatcher:
    """全局歌曲匹配器(DB 直查 + 内存索引 + 短时缓存)。

    fuzzy=True 时额外维护歌名/歌词模糊索引(见 fuzzy.py):
    - 歌名: 精确 → 去括号核名 → Damerau 距离 <= 1 且唯一;
    - 歌词: 6-gram 倒排, 容忍漏字/错字, 阈值来自 5580 首实测。
    模糊命中带 confidence, 由调用方按 min_confidence 决定是否注入。
    """

    def __init__(
        self,
        db_folder: str = "./data/song_knowledge",
        db_file: str = "knowledge_db.db",
        *,
        min_song_name_len: int = 3,
        lyric_min_len: int = 8,
        cache_size: int = 256,
        fuzzy: bool = True,
        min_confidence: float = 0.8,
    ):
        self._db_folder = db_folder
        self._db_file = db_file
        self._min_song_name_len = min_song_name_len
        self._lyric_min_len = lyric_min_len
        self._fuzzy_enabled = bool(fuzzy)
        self._min_confidence = float(min_confidence)
        ensure_init(db_folder, db_file)
        # 内存歌词索引: [(name, lyrics), ...](懒加载, 同步后 reload)
        self._index: List[Tuple[str, str]] = []
        self._index_loaded = False
        # 模糊索引(uuid 维度, 与 songs 表一致)
        self._name_index: Optional[SongNameIndex] = None
        self._lyric_index: Optional[LyricGramIndex] = None
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
                rows = db.query(Song.uuid, Song.name, Song.lyrics).all()
            new_index = [(name, lyrics or "") for _uuid, name, lyrics in rows]
            name_index = None
            lyric_index = None
            if self._fuzzy_enabled:
                name_index = SongNameIndex([(name, uuid) for uuid, name, _lyrics in rows])
                lyric_index = LyricGramIndex()
                lyric_index.build([(uuid, name, lyrics or "") for uuid, name, lyrics in rows])
            with self._lock:
                self._index = new_index
                self._name_index = name_index
                self._lyric_index = lyric_index
                self._index_loaded = True
                self._cache.clear()
                count = len(self._index)
            logger.info(f"歌曲歌词索引已加载: {count} 首 (fuzzy={self._fuzzy_enabled})")
        except Exception as e:
            logger.warning(f"歌曲歌词索引加载失败: {e}")
            with self._lock:
                self._index = []
                self._name_index = None
                self._lyric_index = None
                self._index_loaded = True
                self._cache.clear()

    def _ensure_index(self) -> None:
        if not self._index_loaded:
            self.reload_index()

    # ── 识别 ───────────────────────────────────────────────────

    def match(self, text: str | None) -> Optional[SongMatch]:
        """识别消息中的歌曲并返回行内命中结果(未命中返回 None)。"""
        if not text:
            return None
        with self._lock:
            cached = self._cache.get(text)
        if cached is not None:
            return cached
        result = self._match_uncached(text)
        if result is not None:
            self._put_cache(text, result)
        return result

    def _match_uncached(self, text: str) -> Optional[SongMatch]:
        clean = _strip_cq_codes(text).strip()
        clean = re.sub(r"[！？!?。，、；;：:,.，？]+", " ", clean)
        if not clean:
            return None

        # 1. 书名号歌名 → 最高置信
        m = re.search(r"《([^》]+)》", clean)
        if m:
            name = m.group(1).strip()
            if name:
                detail = self._query_detail(name)
                if detail:
                    matched = SongMatch(detail["name"], detail)
                else:
                    matched = self._match_name_fuzzy(name, allow_core=True)
                    if matched is None:
                        matched = None
                if matched is not None:
                    return matched

        # 2. 裸文本歌名(需歌曲语境词): 消息中去掉"唱一首/点一首/来一首"等
        #    点歌前缀后再做包含匹配(如 "唱一首九九八十一" → "九九八十一")。
        if any(w in clean for w in _SONG_CONTEXT_WORDS):
            candidate = _strip_play_prefix(clean)
            name = self._find_by_name(candidate)
            if name:
                detail = self._query_detail(name)
                if detail:
                    return SongMatch(detail["name"], detail)
            # 自然句中歌名可能夹在疑问/助词之间(如"你知道白鸟过河滩吗"),
            # 对库内歌名做最长优先的包含扫描, 避免把整句当成歌名查询。
            # 也覆盖"库里只有《X(歌手)》而用户只说《X》"的核名情况。
            name = self._find_name_inside(clean)
            if name:
                detail = self._query_detail(name)
                if detail:
                    return SongMatch(detail["name"], detail)
            # 模糊: 整句按"点歌前缀剥离后的候选词"做编辑距离匹配
            # (抓漏字/错别字, 例如"达拉崩巴"); 仅在语境词存在时启用,
            # 避免把日常对话当歌名。
            fuzzy = self._match_name_fuzzy(candidate, allow_core=True)
            if fuzzy is not None:
                return fuzzy

        # 3. 歌词片段匹配
        hit = self._find_by_lyrics(clean)
        if hit:
            detail = self._query_detail(hit)
            if detail:
                return SongMatch(detail["name"], detail, confidence=0.8, reason="lyric",
                                 lyric_span=clean)

        # 4. 模糊歌词(6-gram 倒排): 容忍漏字/错字
        lyric_match = self._find_by_lyrics_fuzzy(clean)
        if lyric_match is not None:
            return lyric_match

        return None

    # ── 模糊匹配 ───────────────────────────────────────────────

    def _match_name_fuzzy(self, text: str, *, allow_core: bool = True) -> Optional[SongMatch]:
        """歌名模糊匹配: 归一化精确 → 核名精确 → Damerau<=1 且唯一。"""
        if not self._fuzzy_enabled or not text:
            return None
        with self._lock:
            index = self._name_index
        if index is None:
            return None
        candidates = index.match(text)
        if not candidates:
            return None
        best = candidates[0]
        if best.ambiguous or best.score < self._min_confidence:
            logger.debug(f"歌曲模糊匹配放弃(歧义/低置信): {text!r} -> {best.name} {best.score}")
            return None
        if best.reason == "core" and not allow_core:
            return None
        detail = self._query_detail(best.name)
        if not detail:
            return None
        return SongMatch(detail["name"], detail, confidence=best.score, reason=best.reason)

    def _find_by_lyrics_fuzzy(self, text: str) -> Optional[SongMatch]:
        """歌词 6-gram 模糊匹配(只在精确歌词未命中时调用)。"""
        if not self._fuzzy_enabled or len(normalize_name(text)) < 10:
            return None
        with self._lock:
            index = self._lyric_index
        if index is None:
            return None
        candidates = index.match(text)
        if not candidates:
            return None
        best = candidates[0]
        if best.ambiguous:
            logger.debug(f"歌词模糊匹配歧义: {text[:20]!r} -> {best.name}")
            return None
        detail = self._query_detail(best.name)
        if not detail:
            return None
        return SongMatch(detail["name"], detail, confidence=0.8, reason="lyric",
                         lyric_span=text)

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
                # 精确命中允许 2 字歌名(如 赤伶/卷!/逃!, 常见于传说曲),
                # 1 字歌名(如《歌》)仍拦截以防"唱一首歌"类误报;
                # 模糊包含保持 min_song_name_len 阈值。
                if exact is not None and len(exact[0]) >= 2:
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

    def _find_name_inside(self, text: str) -> Optional[str]:
        """在自然语言消息中找库内歌名, 按歌名长度倒序避免短名抢先。

        同时比较歌名的"去括号核名": 库里存《九九八十一(乐正绫)》时,
        用户只说《九九八十一》也应命中; 但核名与原文同长时优先原文。
        """
        try:
            with get_session() as db:
                from sqlalchemy import func

                rows = (
                    db.query(Song.name)
                    .filter(func.length(Song.name) >= self._min_song_name_len)
                    .all()
                )
                hits: list[tuple[int, int, str]] = []
                for (name,) in rows:
                    if not name:
                        continue
                    core = bracket_core(name).strip()
                    if name in text:
                        hits.append((len(name), 1, name))
                    elif core and len(core) >= 2 and core in text:
                        hits.append((len(core), 0, name))
                if not hits:
                    return None
                # 命中片段越长越可信; 同长时优先"原名整段出现在消息里"
                hits.sort(key=lambda item: (item[0], item[1]), reverse=True)
                return hits[0][2]
        except Exception as e:
            logger.debug(f"消息内歌名匹配失败: {e}")
            return None

    def _find_by_lyrics(self, text: str) -> Optional[str]:
        """按归一化文本查歌词片段, 支持后缀疑问句和用户省略标点。

        只接受一段较长的连续歌词(至少 16 字), 或两段独立的短歌词同时命中,
        避免普通聊天中的十字短语在数千首歌词中偶然撞中。
        """
        self._ensure_index()
        with self._lock:
            index = tuple(self._index)
        if not index:
            return None
        message = re.sub(r"[\s！？!?。，、；;：:,.，？]+", "", text or "")
        # 歌词识别常带“这是哪首/出自哪首歌”等尾部问题, 先剥离问句再比对。
        message = re.sub(r"(?:这|这句|这段)?是(?:哪一?首|什么)的?(?:歌|歌曲)?$", "", message)
        message = re.sub(r"(?:出自|来自)(?:哪一?首|什么)(?:歌|歌曲)?$", "", message)
        if len(message) < 10:
            return None
        # 较长连续片段优先; 允许用户在歌词后追加问题。
        long_size = 10
        if len(message) >= long_size:
            snippets = {message[i:i + long_size] for i in range(len(message) - long_size + 1)}
            for name, lyrics in index:
                normalized = re.sub(r"\s+", "", lyrics or "")
                if any(snippet in normalized for snippet in snippets):
                    return name
        # 只有两段不同的 10 字片段都命中同一首歌时才放宽阈值。
        short_size = 10
        snippets = {message[i:i + short_size] for i in range(len(message) - short_size + 1)}
        for name, lyrics in index:
            normalized = re.sub(r"\s+", "", lyrics or "")
            hits = sum(1 for snippet in snippets if snippet in normalized)
            if hits >= 2:
                return name
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