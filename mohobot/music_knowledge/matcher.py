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

import array
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from mohobot.music_knowledge.fuzzy import (
    CompactLyricIndex,
    LyricGramIndex,
    SongNameIndex,
)
from mohobot.music_knowledge.index_cache import (
    CachedSongIndex,
    compute_fingerprint,
    load_cache,
    save_cache,
)
from mohobot.music_knowledge.knowledge_service import (
    CREDIT_KEYS,
    _escape_like,
    get_song_detail,
)
from mohobot.music_knowledge.normalize import bracket_core, normalize_name
from mohobot.music_knowledge.pool import ensure_init, get_session
from mohobot.music_knowledge.song_database import Song

# 歌词倒排的 gram 长度(参与缓存指纹; 改变时必须同时递增
# index_cache.INDEX_FORMAT_VERSION / NORMALIZE_VERSION)。
_GRAM_SIZE = 6
# 后台线程构建的默认让出间隔(首)。
_DEFAULT_YIELD_EVERY = 500

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


@dataclass(frozen=True)
class _IndexSnapshot:
    """一次性安装的只读索引快照。

    读路径只取一次引用, 构建线程在 ``self._lock`` 内整体替换; 旧快照由
    引用计数释放, 因此查询过程中不需要与构建互斥。
    """

    index: Tuple[Tuple[str, str], ...]
    name_index: Optional[SongNameIndex]
    lyric_index: Optional[LyricGramIndex]
    source: str = "built"          # cache / built / empty
    fingerprint: str = ""


def _load_song_rows() -> List[Tuple[str, str, str]]:
    """一次查出 (uuid, name, lyrics), 保持数据库返回顺序。

    缓存必须复用同一顺序: ``LyricGramIndex`` 的整数 doc id 按传入顺序
    分配, ``_find_by_lyrics`` 也按此顺序返回第一首命中。
    """
    with get_session() as db:
        rows = db.query(Song.uuid, Song.name, Song.lyrics).all()
    return [
        (str(uuid), name or "", lyrics or "")
        for uuid, name, lyrics in rows
    ]


class SongInfoMatcher:
    """全局歌曲匹配器(DB 直查 + 内存索引 + 短时缓存)。

    fuzzy=True 时额外维护歌名/歌词模糊索引(见 fuzzy.py):
    - 歌名: 精确 → 去括号核名 → Damerau 距离 <= 1 且唯一;
    - 歌词: 6-gram 倒排, 容忍漏字/错字, 阈值来自 5580 首实测。
    模糊命中带 confidence, 由调用方按 min_confidence 决定是否注入。

    **索引生命周期**: 快照式(index/name_index/lyric_index 打包为不可变
    对象)。缓存命中则同步加载(约数秒)后安装; 未命中时 ``__init__``
    立即返回, 索引在后台线程/子进程构建, 期间 ``match()`` 返回 None
    (歌曲识别降级为空, 不影响正常聊天)。构建完成后**先写缓存再原子
    替换快照**, 旧快照仍可被在途查询安全读取。

    构造参数向后兼容: 不传 ``build`` 时保持旧的同步构建(测试/脚本);
    ``main.py`` 会显式传入配置(默认 thread)。
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
        index_cache: Optional[Dict[str, Any]] = None,
        build: Optional[Dict[str, Any]] = None,
    ):
        self._db_folder = db_folder
        self._db_file = db_file
        self._db_path = os.path.join(db_folder, db_file)
        self._min_song_name_len = min_song_name_len
        self._lyric_min_len = lyric_min_len
        self._fuzzy_enabled = bool(fuzzy)
        self._min_confidence = float(min_confidence)

        # 缓存配置: 缺省开启; 目录缺省为 <db_folder 的父目录>/cache/song_index
        # (生产 ./data/song_knowledge → ./data/cache/song_index, 备份已排除)。
        ic_cfg = dict(index_cache or {})
        self._index_cache_enabled = bool(ic_cfg.get("enabled", True))
        default_cache_dir = os.path.join(
            os.path.dirname(os.path.abspath(db_folder.rstrip("/\\"))),
            "cache",
            "song_index",
        )
        self._cache_dir = str(ic_cfg.get("dir") or default_cache_dir)
        try:
            self._cache_keep = max(1, int(ic_cfg.get("keep", 2) or 2))
        except (TypeError, ValueError):
            self._cache_keep = 2

        # 构建模式: build=None 表示调用方未接线, 保持旧同步构建(兼容
        # 现有测试/脚本); 显式传入时缺省 thread, 未知值降级为 thread。
        if build is None:
            self._build_mode = "sync"
            self._yield_every = _DEFAULT_YIELD_EVERY
        else:
            bc = dict(build or {})
            mode = str(bc.get("mode", "thread") or "thread").strip().lower()
            self._build_mode = (
                mode if mode in ("thread", "subprocess", "sync") else "thread"
            )
            try:
                self._yield_every = int(
                    bc.get("yield_every", _DEFAULT_YIELD_EVERY) or 0
                )
            except (TypeError, ValueError):
                self._yield_every = _DEFAULT_YIELD_EVERY
            if self._yield_every <= 0:
                self._yield_every = _DEFAULT_YIELD_EVERY

        # 快照原子替换: 读路径只取一次引用, 未就绪时 match() 返回 None。
        self._snapshot: Optional[_IndexSnapshot] = None
        self._snapshot_source = "pending"
        self._ready = threading.Event()
        self._build_thread: Optional[threading.Thread] = None
        self._building = False
        self._dirty = False
        self._build_reason = ""
        self._build_error: Optional[str] = None
        self._index_loaded = False
        self._fingerprint = ""

        # 短时匹配缓存: msg -> SongMatch(避免同文案反复查库)
        self._cache: Dict[str, SongMatch] = {}
        self._cache_size = cache_size
        self._lock = threading.RLock()

        ensure_init(db_folder, db_file)

        if (
            self._build_mode == "sync"
            or not self._index_cache_enabled
            or not self._fuzzy_enabled
        ):
            # 完全回退旧行为: 同步构建。fuzzy=False 时没有 gram 索引,
            # 也不值得为 1.4s 的查询付出缓存版本兼容成本。
            self.reload_index()
            return

        self._startup_load_or_build()

    # ── 索引(歌词匹配用) ───────────────────────────────────────

    def _startup_load_or_build(self) -> None:
        """缓存命中则同步安装快照; 否则立即返回并调度后台构建。"""
        self._fingerprint = self._safe_fingerprint()
        cached = None
        if self._fingerprint:
            cached = load_cache(
                self._cache_dir,
                self._fingerprint,
                expected_gram_size=_GRAM_SIZE,
            )
        if cached is not None:
            try:
                snap = self._snapshot_from_cache(cached)
            except Exception as e:  # 缓存内容异常 → 视为 miss
                logger.warning(f"歌曲索引缓存解析失败, 改为后台重建: {e}")
                snap = None
            if snap is not None:
                self._install(snap, source="cache")
                stats = (
                    snap.lyric_index.last_load_stats
                    if snap.lyric_index is not None
                    else {}
                )
                logger.info(
                    f"歌曲索引缓存命中: {len(snap.index)} 首, "
                    f"布局 {stats.get('layout', '?')} "
                    f"(dict 预计 {stats.get('dict_setup_ms', '?')}ms, "
                    f"bisect 查询 {stats.get('bisect_query_us', '?')}us)"
                )
                return
        logger.info(
            "歌曲索引缓存未命中或已失效, 后台构建中"
            "(构建完成前歌曲识别降级为空, 不影响聊天)"
        )
        self._schedule_build(reason="startup")

    def reload_index(self) -> None:
        """同步重建全部索引(测试/工具/同步模式使用)。

        正常启动路径不会调用这里(会阻塞事件循环); ``request_rebuild()``
        才是运行时入口。缓存开启时会顺带写入缓存文件。
        """
        try:
            rows = _load_song_rows()
            fingerprint = self._safe_fingerprint()
            snap = self._build_snapshot_from_rows(
                rows,
                fingerprint=fingerprint,
                gc_control=False,
                yield_every=0,
            )
            if self._index_cache_enabled and self._build_mode != "sync":
                self._write_cache(snap, rows, fingerprint)
            self._install(snap, source="built")
            logger.info(
                f"歌曲歌词索引已加载: {len(snap.index)} 首 "
                f"(fuzzy={self._fuzzy_enabled})"
            )
        except Exception as e:
            logger.warning(f"歌曲歌词索引加载失败: {e}")
            self._install(
                _IndexSnapshot((), None, None, "empty"), source="empty"
            )

    def _build_snapshot_from_rows(
        self,
        rows: List[Tuple[str, str, str]],
        *,
        fingerprint: str,
        gc_control: bool,
        yield_every: int,
    ) -> _IndexSnapshot:
        index = tuple((name, lyrics) for _uuid, name, lyrics in rows)
        name_index: Optional[SongNameIndex] = None
        lyric_index: Optional[LyricGramIndex] = None
        if self._fuzzy_enabled:
            name_index = SongNameIndex()
            name_index.build(
                [(name, uuid) for uuid, name, _lyrics in rows],
                yield_every=yield_every,
            )
            lyric_index = LyricGramIndex(gram_size=_GRAM_SIZE)
            progress = None
            if yield_every:
                state = {"next": 10}

                def progress(done: int, total: int) -> None:  # noqa: F811
                    if total <= 0:
                        return
                    pct = int(done * 100 / total)
                    if pct >= state["next"]:
                        logger.info(
                            f"歌曲索引构建进度 {pct}% ({done}/{total})"
                        )
                        state["next"] = (pct // 10 + 1) * 10

            lyric_index.build(
                [(uuid, name, lyrics) for uuid, name, lyrics in rows],
                gc_control=gc_control,
                yield_every=yield_every,
                progress=progress,
            )
        return _IndexSnapshot(
            index=index,
            name_index=name_index,
            lyric_index=lyric_index,
            source="built",
            fingerprint=fingerprint,
        )

    def _snapshot_from_cache(self, cached: CachedSongIndex) -> _IndexSnapshot:
        compact = CompactLyricIndex(
            gram_size=cached.gram_size,
            doc_uuid=cached.doc_uuid,
            doc_name=cached.doc_name,
            doc_lyrics=cached.doc_lyrics,
            doc_len=cached.doc_len,
            grams_blob=cached.grams_blob,
            gram_off=cached.gram_off,
            post_ptr=cached.post_ptr,
            post_doc=cached.post_doc,
        )
        lyric_index = LyricGramIndex.from_compact(compact)
        name_index = SongNameIndex(zip(cached.doc_name, cached.doc_uuid))
        index = tuple(zip(cached.doc_name, cached.doc_lyrics_raw))
        return _IndexSnapshot(
            index=index,
            name_index=name_index,
            lyric_index=lyric_index,
            source="cache",
            fingerprint=cached.fingerprint,
        )

    def _write_cache(
        self,
        snap: _IndexSnapshot,
        rows: List[Tuple[str, str, str]],
        fingerprint: str,
    ) -> Optional[str]:
        if not fingerprint or snap.lyric_index is None:
            return None
        try:
            compact = snap.lyric_index.to_compact()
            raw_lyrics = [lyrics for _uuid, _name, lyrics in rows]
            raw_off = array.array("Q", [0])
            total = 0
            for text in raw_lyrics:
                total += len(text.encode("utf-8"))
                raw_off.append(total)
            data = CachedSongIndex(
                fingerprint=fingerprint,
                gram_size=compact.gram_size,
                doc_uuid=compact.doc_uuid,
                doc_name=compact.doc_name,
                doc_lyrics=compact.doc_lyrics,
                doc_lyrics_raw=raw_lyrics,
                doc_len=compact.doc_len,
                raw_off=raw_off,
                grams_blob=compact.grams_blob,
                gram_off=compact.gram_off,
                post_ptr=compact.post_ptr,
                post_doc=compact.post_doc,
                meta={
                    "docs": len(raw_lyrics),
                    "grams": len(compact.gram_off) - 1,
                    "layout": "compact",
                },
            )
            path = save_cache(self._cache_dir, data, keep=self._cache_keep)
            if path:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                logger.info(
                    f"歌曲索引缓存已写入: {path} ({size / 1024 / 1024:.1f}MB)"
                )
            else:
                logger.warning("歌曲索引缓存写入失败(不影响识别)")
            return path
        except Exception as e:
            logger.warning(f"歌曲索引缓存写入异常(不影响识别): {e}")
            return None

    # ── 快照安装 / 后台构建 ─────────────────────────────────────

    def _install(self, snap: _IndexSnapshot, *, source: str) -> None:
        """在锁内原子替换快照; 旧快照由在途查询自行读完释放。"""
        with self._lock:
            self._snapshot = snap
            self._snapshot_source = source
            self._build_error = None
            self._cache.clear()
            self._index_loaded = True
            self._ready.set()

    def _safe_fingerprint(self) -> str:
        try:
            return compute_fingerprint(
                self._db_path,
                gram_size=_GRAM_SIZE,
                fuzzy=self._fuzzy_enabled,
            )
        except OSError:
            return ""

    def _schedule_build(self, *, reason: str) -> None:
        with self._lock:
            if self._building:
                # 正在构建: 标记脏, 结束后再跑一轮(不重复起线程)。
                self._dirty = True
                logger.info(f"歌曲索引构建中, 已排队下一轮(reason={reason})")
                return
            self._building = True
            self._dirty = False
            self._build_reason = reason
            thread = threading.Thread(
                target=self._build_worker,
                args=(reason,),
                name=f"song-index-build-{reason}",
                daemon=True,
            )
            self._build_thread = thread
        logger.info(
            f"歌曲索引后台构建已调度(reason={reason}, mode={self._build_mode}, "
            f"yield_every={self._yield_every})"
        )
        thread.start()

    def request_rebuild(self, *, reason: str = "manual") -> None:
        """非阻塞地请求重建索引。

        - 缓存/后台构建开启: 立即返回, 构建完成后自动替换快照;
        - ``build.mode=sync`` 或 ``index_cache.enabled=false``: 退化为
          旧的同步 ``reload_index()``(调用方应了解会阻塞)。
        """
        if (
            self._build_mode == "sync"
            or not self._index_cache_enabled
            or not self._fuzzy_enabled
        ):
            self.reload_index()
            return
        self._schedule_build(reason=reason)

    def is_ready(self) -> bool:
        """索引快照是否已安装(缓存命中/构建完成)。"""
        return self._snapshot is not None

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """等待索引就绪(测试/运维脚本用, 不要在主循环里阻塞等待)。"""
        return self._ready.wait(timeout)

    @property
    def index_source(self) -> str:
        """当前快照来源: ``cache`` / ``built`` / ``empty`` / ``pending``。"""
        return self._snapshot_source

    @property
    def build_mode(self) -> str:
        return self._build_mode

    def song_count(self) -> int:
        snap = self._snapshot
        return len(snap.index) if snap is not None else 0

    def _build_worker(self, reason: str) -> None:
        current = reason
        while True:
            try:
                self._build_once(current)
            except Exception as e:  # 后台构建失败不影响已有快照/聊天
                self._build_error = str(e)
                logger.warning(f"歌曲索引后台构建失败(reason={current}): {e}")
            with self._lock:
                if not self._dirty:
                    self._building = False
                    self._build_thread = None
                    return
                self._dirty = False
                current = "dirty"

    def _build_once(self, reason: str) -> None:
        started = time.time()
        if self._build_mode == "subprocess":
            if self._build_via_subprocess():
                fingerprint = self._safe_fingerprint()
                cached = (
                    load_cache(
                        self._cache_dir,
                        fingerprint,
                        expected_gram_size=_GRAM_SIZE,
                    )
                    if fingerprint
                    else None
                )
                if cached is not None:
                    snap = self._snapshot_from_cache(cached)
                    self._install(snap, source="cache")
                    logger.info(
                        f"歌曲索引子进程构建完成(reason={reason}): "
                        f"{len(snap.index)} 首, 用时 {time.time() - started:.1f}s"
                    )
                    return
                logger.warning("歌曲索引子进程未产出可加载缓存, 回退线程构建")
            else:
                logger.warning("歌曲索引子进程不可用, 回退线程构建")

        rows = _load_song_rows()
        fingerprint = self._safe_fingerprint()
        snap = self._build_snapshot_from_rows(
            rows,
            fingerprint=fingerprint,
            gc_control=True,
            yield_every=self._yield_every,
        )
        # 先写缓存再替换: 下次启动可直接命中。
        if fingerprint:
            self._write_cache(snap, rows, fingerprint)
        self._fingerprint = fingerprint
        self._install(snap, source="built")
        logger.info(
            f"歌曲索引后台构建完成(reason={reason}): {len(snap.index)} 首, "
            f"用时 {time.time() - started:.1f}s"
        )

    def _build_via_subprocess(self) -> bool:
        import subprocess
        import sys

        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        cmd = [
            sys.executable,
            "-m",
            "mohobot.music_knowledge.index_cache",
            "build",
            "--db-folder", os.path.abspath(self._db_folder),
            "--db-file", self._db_file,
            "--cache-dir", os.path.abspath(self._cache_dir),
            "--gram-size", str(_GRAM_SIZE),
            "--keep", str(self._cache_keep),
            "--yield-every", str(self._yield_every),
        ]
        if self._fuzzy_enabled:
            cmd.append("--fuzzy")
        env = os.environ.copy()
        env["PYTHONPATH"] = (
            root + os.pathsep + env["PYTHONPATH"]
            if env.get("PYTHONPATH")
            else root
        )
        logger.info("歌曲索引子进程构建启动(不占用主进程 GIL)")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,
                cwd=root,
                env=env,
            )
        except Exception as e:
            logger.warning(f"歌曲索引子进程启动失败: {e}")
            return False
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            logger.warning(
                f"歌曲索引子进程失败(rc={proc.returncode}): {' | '.join(tail)}"
            )
            return False
        return True

    @property
    def _index(self) -> Tuple[Tuple[str, str], ...]:
        """兼容旧代码的只读索引视图(未就绪时为空)。"""
        snap = self._snapshot
        return snap.index if snap is not None else ()

    # ── 识别 ───────────────────────────────────────────────────

    def match(self, text: str | None) -> Optional[SongMatch]:
        """识别消息中的歌曲并返回行内命中结果(未命中返回 None)。

        索引未就绪(缓存 miss 且后台仍在构建)时直接返回 None, **绝不**
        触发同步构建 —— 不能因为一条带歌词的消息卡住事件循环。
        """
        if not text:
            return None
        snap = self._snapshot
        if snap is None:
            return None
        with self._lock:
            cached = self._cache.get(text)
        if cached is not None:
            return cached
        result = self._match_uncached(text, snap)
        if result is not None:
            self._put_cache(text, result)
        return result

    def _match_uncached(
        self, text: str, snap: Optional[_IndexSnapshot] = None
    ) -> Optional[SongMatch]:
        if snap is None:
            snap = self._snapshot
        if snap is None:
            return None
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
                    matched = self._match_name_fuzzy(
                        name, allow_core=True, snap=snap
                    )
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
            fuzzy = self._match_name_fuzzy(
                candidate, allow_core=True, snap=snap
            )
            if fuzzy is not None:
                return fuzzy

        # 3. 歌词片段匹配
        hit = self._find_by_lyrics(clean, snap=snap)
        if hit:
            detail = self._query_detail(hit)
            if detail:
                return SongMatch(detail["name"], detail, confidence=0.8, reason="lyric",
                                 lyric_span=clean)

        # 4. 模糊歌词(6-gram 倒排): 容忍漏字/错字
        lyric_match = self._find_by_lyrics_fuzzy(clean, snap=snap)
        if lyric_match is not None:
            return lyric_match

        return None

    # ── 模糊匹配 ───────────────────────────────────────────────

    def _match_name_fuzzy(
        self,
        text: str,
        *,
        allow_core: bool = True,
        snap: Optional[_IndexSnapshot] = None,
    ) -> Optional[SongMatch]:
        """歌名模糊匹配: 归一化精确 → 核名精确 → Damerau<=1 且唯一。"""
        if not self._fuzzy_enabled or not text:
            return None
        if snap is None:
            snap = self._snapshot
        index = snap.name_index if snap is not None else None
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

    def _find_by_lyrics_fuzzy(
        self, text: str, snap: Optional[_IndexSnapshot] = None
    ) -> Optional[SongMatch]:
        """歌词 6-gram 模糊匹配(只在精确歌词未命中时调用)。"""
        if not self._fuzzy_enabled or len(normalize_name(text)) < 10:
            return None
        if snap is None:
            snap = self._snapshot
        index = snap.lyric_index if snap is not None else None
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

    def _find_by_lyrics(
        self, text: str, snap: Optional[_IndexSnapshot] = None
    ) -> Optional[str]:
        """按归一化文本查歌词片段, 支持后缀疑问句和用户省略标点。

        只接受一段较长的连续歌词(至少 16 字), 或两段独立的短歌词同时命中,
        避免普通聊天中的十字短语在数千首歌词中偶然撞中。

        注意: 这里**不做** ``_ensure_index()`` 式的同步重建。索引未就绪时
        (缓存 miss + 后台构建中)直接返回 None, 否则第一条带歌词的消息
        会把事件循环卡在 100s 的全量构建上。
        """
        if snap is None:
            snap = self._snapshot
        if snap is None:
            return None
        index = snap.index
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


def build_song_index_cache(
    db_folder: str,
    db_file: str,
    cache_dir: str,
    *,
    gram_size: int = _GRAM_SIZE,
    fuzzy: bool = True,
    keep: int = 2,
    yield_every: int = _DEFAULT_YIELD_EVERY,
) -> Optional[str]:
    """构建歌词索引并写入缓存文件(子进程模式 / 运维脚本入口)。

    不构造 ``SongInfoMatcher``, 也不接触 WS/事件循环; 失败返回 None。
    主进程随后用 ``load_cache()`` + ``LyricGramIndex.from_compact()`` 安装。
    """
    if not fuzzy:
        # 没有 gram 索引时缓存收益很低, 与 matcher 的同步降级保持一致。
        return None
    ensure_init(db_folder, db_file)
    rows = _load_song_rows()
    db_path = os.path.join(db_folder, db_file)
    fingerprint = compute_fingerprint(
        db_path, gram_size=gram_size, fuzzy=fuzzy
    )
    lyric_index = LyricGramIndex(gram_size=gram_size)

    state = {"next": 10}

    def _progress(done: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(done * 100 / total)
        if pct >= state["next"]:
            logger.info(f"歌曲索引子进程构建进度 {pct}% ({done}/{total})")
            state["next"] = (pct // 10 + 1) * 10

    lyric_index.build(
        [(uuid, name, lyrics) for uuid, name, lyrics in rows],
        gc_control=True,
        yield_every=yield_every,
        progress=_progress,
    )
    compact = lyric_index.to_compact()

    raw_lyrics = [lyrics for _uuid, _name, lyrics in rows]
    raw_off = array.array("Q", [0])
    total = 0
    for text in raw_lyrics:
        total += len(text.encode("utf-8"))
        raw_off.append(total)
    data = CachedSongIndex(
        fingerprint=fingerprint,
        gram_size=compact.gram_size,
        doc_uuid=compact.doc_uuid,
        doc_name=compact.doc_name,
        doc_lyrics=compact.doc_lyrics,
        doc_lyrics_raw=raw_lyrics,
        doc_len=compact.doc_len,
        raw_off=raw_off,
        grams_blob=compact.grams_blob,
        gram_off=compact.gram_off,
        post_ptr=compact.post_ptr,
        post_doc=compact.post_doc,
        meta={
            "docs": len(raw_lyrics),
            "grams": len(compact.gram_off) - 1,
            "builder": "subprocess",
        },
    )
    return save_cache(cache_dir, data, keep=keep)