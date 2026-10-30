"""歌曲名/歌词模糊匹配索引 — 纯内存、线程安全、不依赖数据库。

实现阈值来自 5580 首真实歌曲库的离线实验(见 docs/DEVELOPMENT.md 歌曲知识一节):

- 歌名候选流水线: 归一化精确 → 去括号核名精确 → (长度 >= 4 时)
  Damerau-Levenshtein 距离 <= 1 且候选唯一。实测 Top1 68.1%、误匹配 0.30%,
  300 句日常聊天误报 0/300; 单纯 difflib ratio@0.86 只有 25.8%, 不采用。
- 歌词: 6-gram 倒排 + 覆盖率/命中数双判据。长歌词(归一化后 >= 60 字)
  cov >= 0.3 且 hits >= 4 → Top1 99.0%、误匹配 0%; 短歌词(< 60 字)
  cov >= 0.4 且 hits >= 5, 或 cov >= 0.5 且 hits >= 7 → Top1 92.5%、
  误匹配 0%。旧的 10 字精确滑窗在短歌词上误匹配 6.7%。
- 性能: 6-gram 倒排单次查询 0.015ms; 5580 首建索引约 2.6s, 16k 首约
  100~150s(GC 关闭后约 100s)。歌名 Damerau 全扫 5.3ms, 按长度分桶 2.3ms,
  2 万首规模可用。

接口约定: 本模块不 import sqlalchemy、不读数据库、不碰文件系统; 调用方
负责从库里取数据后调用 build()。持久化通过 ``to_compact()`` /
``from_compact()`` 暴露**扁平结构**, 由 ``index_cache`` 负责文件格式与
版本管理 —— 索引对象本身不直接 pickle(540 万个单元素 list 的反序列化
成本接近重算)。

加载紧凑索引时可选择两种倒排布局(``from_compact`` 内部用小样本 benchmark
自动选择): ``dict[str, memoryview]`` 查询 O(1) 但建表要 500 万次插入;
有序 ``grams_blob`` + ``bisect`` 查询 O(log n)、加载几乎零成本。两者对
``match()`` 的结果完全一致。

所有公开函数/类均可在多线程下安全调用(内部 threading.RLock); match()
在锁内完成查询, 增量 add()/remove() 与查询互斥。
"""

from __future__ import annotations

import array
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable, Optional

from mohobot.music_knowledge.normalize import (
    alias_keys,
    bracket_core,
    normalize_name,
)

__all__ = [
    "damerau_levenshtein",
    "SongNameIndex",
    "NameCandidate",
    "LyricGramIndex",
    "LyricCandidate",
    "CompactLyricIndex",
]

# 歌名: 允许被 Damerau 纠正的最短归一化长度。
# 实验中的 "长度 >= 4" 门槛: 3 字以下歌名被错 1 字后与大量日常词同形,
# 关掉这一档才能把聊天误报压到 0。
_MIN_TYPO_LEN = 4

# 歌词: 查询归一化后短于该长度直接返回空, 避免 "今天天气" 这类日常词撞 gram。
_MIN_LYRIC_QUERY_LEN = 8

# 歌词: 长/短歌词分界(归一化字数)与两套双判据。
_LONG_LYRIC_LEN = 60

# 长歌词: cov >= 0.3 且 hits >= 4 (实测 Top1 99.0%, 误匹配 0%)
_LONG_MIN_COV = 0.3
_LONG_MIN_HITS = 4

# 短歌词: cov >= 0.4 且 hits >= 5, 或 cov >= 0.5 且 hits >= 7
# (实测 Top1 92.5%, 误匹配 0%)
_SHORT_MIN_COV_A = 0.4
_SHORT_MIN_HITS_A = 5
_SHORT_MIN_COV_B = 0.5
_SHORT_MIN_HITS_B = 7


# ── 编辑距离 ──────────────────────────────────────────────────


def damerau_levenshtein(a: str, b: str, max_distance: int = 2) -> int:
    """计算 Damerau-Levenshtein 距离(相邻字符换位算 1 次编辑)。

    带提前剪枝, 超过 ``max_distance`` 时统一返回 ``max_distance + 1``:

    - 长度差大于 ``max_distance`` 直接剪枝;
    - 动态规划只计算带宽 ``|i - j| <= max_distance`` 的格子;
    - 某一行的最小值都超过 ``max_distance`` 时立即返回。

    歌名纠正只用 ``max_distance=1``(5580 首实测: 距离 <= 1 且候选唯一时
    Top1 68.1%、误匹配 0.30%; 放到 2 会显著抬升误匹配), 所以性能敏感
    路径不必依赖第三方 rapidfuzz。

    Args:
        a: 待比较字符串(调用方应已归一化)。
        b: 待比较字符串。
        max_distance: 允许的最大编辑数, 必须 >= 0。

    Returns:
        最小编辑数; 超过 ``max_distance`` 时返回 ``max_distance + 1``。
    """
    if max_distance < 0:
        raise ValueError("max_distance 必须 >= 0")
    a = a or ""
    b = b or ""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    inf = max_distance + 1
    if abs(la - lb) > max_distance:
        return inf
    if la == 0 or lb == 0:
        return min(max(la, lb), inf)

    # prev2/prev 是 i-2/i-1 行的滚动数组; 带宽外的格子保持 inf。
    prev2: Optional[list[int]] = None
    prev = [min(j, inf) for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [inf] * (lb + 1)
        cur[0] = min(i, inf)
        lo = max(1, i - max_distance)
        hi = min(lb, i + max_distance)
        a_i = a[i - 1]
        row_min = inf
        for j in range(lo, hi + 1):
            cost = 0 if a_i == b[j - 1] else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (
                prev2 is not None
                and i > 1
                and j > 1
                and a_i == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                # 相邻换位: "ab" -> "ba" 记 1 次编辑。
                value = min(value, prev2[j - 2] + 1)
            if value > inf:
                value = inf
            cur[j] = value
            if value < row_min:
                row_min = value
        if row_min > max_distance:
            return inf
        prev2, prev = prev, cur
    return prev[lb] if prev[lb] <= max_distance else inf


def _within_one_edit(a: str, b: str) -> bool:
    """判断两串的 Damerau-Levenshtein 距离是否 <= 1(专用快速路径)。

    只处理距离 0/1 的三种形态, 全部用 C 级字符串比较完成:

    - 等长且仅一处不同 → 1 次替换;
    - 等长且仅相邻两位互换 → 1 次换位;
    - 长度差 1 且删/插一位后相同 → 1 次增删。

    其它情况一律返回 False(真实距离 >= 2)。歌名匹配默认只允许距离 1,
    这条路径把 5580 首的桶内扫描从 6ms 压到约 2ms。
    """
    la, lb = len(a), len(b)
    if a == b:
        return True
    if la == lb:
        first = -1
        for i in range(la):
            if a[i] != b[i]:
                first = i
                break
        if first < 0:
            return True
        last = first
        for i in range(la - 1, first, -1):
            if a[i] != b[i]:
                last = i
                break
        if first == last:
            return True
        return (
            last == first + 1
            and a[first] == b[last]
            and a[last] == b[first]
        )
    if abs(la - lb) != 1:
        return False
    short, long = (a, b) if la < lb else (b, a)
    # 找到第一处不同后, 长串跳过一位应与短串剩余部分完全一致。
    for i in range(len(short)):
        if short[i] != long[i]:
            return short[i:] == long[i + 1 :]
    return True


# ── 歌名索引 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class NameCandidate:
    """一次歌名匹配的候选。

    reason 取值:
    - ``exact``: 归一化后与库内主名完全相同(置信度 1.0);
    - ``core``: 去掉输入尾部括号后与主名/核名相同(置信度 0.95);
    - ``typo``: Damerau 距离 <= 1 且候选唯一(置信度 0.9)。

    多个 uuid 同时命中时 score 统一降为 0.5 且 ``ambiguous=True``,
    调用方应视为不确定、不要自动注入注解。
    """

    uuid: str
    name: str
    score: float
    reason: str
    ambiguous: bool = False


class SongNameIndex:
    """歌名候选索引: 精确 → 核名 → Damerau 纠正, 全部在内存中完成。

    结构:
    - ``_primary``: 主名归一化键 -> uuid 集合(精确命中);
    - ``_keys``: 主名 + 去括号核名 -> uuid 集合(核名命中, 双向覆盖
      "库里有括号、用户不带" 与 "库里没括号、用户带" 两种情况);
    - ``_buckets``: 归一化长度 -> 键集合, 把 Damerau 扫描限制在
      ``L-1 .. L+1`` 三个桶(5580 首实测 2.3ms, 全扫 5.3ms)。

    阈值来自 5580 首真实库实验: 精确/核名/Damerau<=1 唯一 的流水线
    Top1 68.1%、误匹配 0.30%; 300 句日常聊天误报 0。因此这里不做
    模糊 ratio 兜底(difflib ratio@0.86 仅 25.8%)。

    Args:
        entries: 可选的 ``(name, uuid)`` 迭代器, 等价于构造函数内 build。
    """

    def __init__(self, entries: Optional[Iterable[tuple[str, str]]] = None) -> None:
        self._lock = threading.RLock()
        self._primary: dict[str, set[str]] = {}
        self._keys: dict[str, set[str]] = {}
        self._buckets: dict[int, set[str]] = {}
        self._uuid_name: dict[str, str] = {}
        self._uuid_keys: dict[str, set[str]] = {}
        if entries is not None:
            self.build(entries)

    # ── 构建/增量 ─────────────────────────────────────────────

    def build(
        self,
        entries: Iterable[tuple[str, str]],
        *,
        yield_every: int = 0,
    ) -> int:
        """清空后按 ``(name, uuid)`` 重建索引, 返回载入的曲目数。

        调用方从数据库取出全部歌名后一次性构建; 建索引只做归一化与分桶,
        5580 首约 0.2s, 不需要每次查询重归一化全库。``yield_every > 0``
        时每 N 条 ``time.sleep(0)`` 让出 GIL(后台线程构建用, 避免 16k
        首时把事件循环卡住 1~2s)。
        """
        with self._lock:
            self._clear_locked()
            count = 0
            for index, (name, uuid) in enumerate(entries, 1):
                if self._add_locked(name, uuid):
                    count += 1
                if yield_every and index % yield_every == 0:
                    time.sleep(0)
            return count

    def add(self, name: str, uuid: str) -> bool:
        """增量加入(或更新)一首歌; uuid 已存在时先移除旧键再重建。

        返回是否成功加入(uuid 为空时返回 False)。同名多 uuid 会形成
        歧义候选, 这是预期行为(本地库存在 28 组同名不同曲)。
        """
        with self._lock:
            return self._add_locked(name, uuid)

    def _add_locked(self, name: str, uuid: str) -> bool:
        uid = str(uuid or "").strip()
        if not uid:
            return False
        self._remove_locked(uid)  # upsert: 同 uuid 重复 add 不残留旧键
        display = name or ""
        self._uuid_name[uid] = display
        keys = alias_keys(display)
        self._uuid_keys[uid] = keys
        if not keys:
            return True
        primary = normalize_name(display)
        for key in keys:
            self._keys.setdefault(key, set()).add(uid)
            self._buckets.setdefault(len(key), set()).add(key)
            if key == primary:
                self._primary.setdefault(key, set()).add(uid)
        return True

    def _remove_locked(self, uuid: str) -> bool:
        keys = self._uuid_keys.pop(uuid, None)
        if keys is None:
            self._uuid_name.pop(uuid, None)
            return False
        for key in keys:
            ids = self._keys.get(key)
            if ids is not None:
                ids.discard(uuid)
                if not ids:
                    del self._keys[key]
            pids = self._primary.get(key)
            if pids is not None:
                pids.discard(uuid)
                if not pids:
                    del self._primary[key]
            bucket = self._buckets.get(len(key))
            if bucket is not None and key not in self._keys:
                # 只在没有任何 uuid 再引用该键时才从长度桶移除。
                bucket.discard(key)
                if not bucket:
                    del self._buckets[len(key)]
        self._uuid_name.pop(uuid, None)
        return True

    def clear(self) -> None:
        """清空全部索引数据(不改变对象本身, 之后可继续 add)。"""
        with self._lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        self._primary.clear()
        self._keys.clear()
        self._buckets.clear()
        self._uuid_name.clear()
        self._uuid_keys.clear()

    def __len__(self) -> int:
        """当前收录的曲目数。"""
        with self._lock:
            return len(self._uuid_name)

    # ── 查询 ──────────────────────────────────────────────────

    def match(
        self,
        text: str,
        *,
        max_distance: int = 1,
        min_len: int = _MIN_TYPO_LEN,
    ) -> list[NameCandidate]:
        """按推荐流水线返回歌名候选(可能为空列表)。

        依次尝试:

        1. 归一化精确: 唯一 uuid → score 1.0, reason ``exact``;
        2. 去括号核名精确: 唯一 uuid → score 0.95, reason ``core``;
        3. 归一化长度够长时做 Damerau-Levenshtein <= ``max_distance``
           (默认 1)的桶内扫描, 唯一 uuid → score 0.9, reason ``typo``。

        第 3 步的门槛作用在"库内键长 >= ``min_len``"上(默认 4, 即实验里
        的推荐规则); 查询本身只要不短于 ``min_len - max_distance`` 就会
        进入扫描 —— 4 字歌名漏 1 字(剩 3 字)仍能救回, 而 2 字日常词
        不会撞上 3 字歌名。

        任何一步命中多个 uuid 都返回全部候选, score 降为 0.5 且
        ``ambiguous=True``; 精确/核名命中后不再继续往下找, 避免更宽松
        的规则覆盖更可信的结果。

        Args:
            text: 用户消息或待匹配歌名(内部自行归一化)。
            max_distance: Damerau 最大编辑数, 实测只用 1。
            min_len: 库内归一化键参与 Damerau 扫描的最短长度, 默认 4。
        """
        raw = text or ""
        norm = normalize_name(raw)
        if not norm:
            return []
        with self._lock:
            # 1. 归一化精确
            exact_ids = self._primary.get(norm)
            if exact_ids:
                unique = len(exact_ids) == 1
                return self._make_candidates(
                    exact_ids, 1.0 if unique else 0.5, "exact", ambiguous=not unique
                )

            # 2. 去括号核名精确(输入带后缀或库里带后缀都能覆盖)
            core = normalize_name(bracket_core(raw))
            if core:
                core_ids = self._keys.get(core)
                if core_ids:
                    unique = len(core_ids) == 1
                    return self._make_candidates(
                        core_ids,
                        0.95 if unique else 0.5,
                        "core",
                        ambiguous=not unique,
                    )

            # 3. Damerau <= max_distance, 只在长度相邻桶里找
            #    门槛作用在"库内键长"上(≥ min_len), 查询本身只需够到
            #    min_len - max_distance(4 字歌名漏 1 字仍可救回, 但 2 字
            #    查询不进入, 否则日常短词会撞上 3 字歌名)。
            if len(norm) < min_len - max_distance:
                return []
            matched: set[str] = set()
            length = len(norm)
            for bucket_len in range(
                length - max_distance, length + max_distance + 1
            ):
                if bucket_len < min_len:
                    continue
                for key in self._buckets.get(bucket_len, ()):
                    if max_distance == 1:
                        ok = _within_one_edit(norm, key)
                    else:
                        ok = (
                            damerau_levenshtein(norm, key, max_distance)
                            <= max_distance
                        )
                    if ok:
                        matched.update(self._keys.get(key, ()))
            if not matched:
                return []
            unique = len(matched) == 1
            return self._make_candidates(
                matched, 0.9 if unique else 0.5, "typo", ambiguous=not unique
            )

    def _make_candidates(
        self,
        uuids: Iterable[str],
        score: float,
        reason: str,
        *,
        ambiguous: bool,
    ) -> list[NameCandidate]:
        """把 uuid 集合整理成按 uuid 排序的候选列表(锁内调用)。"""
        return [
            NameCandidate(
                uuid=uid,
                name=self._uuid_name.get(uid, ""),
                score=score,
                reason=reason,
                ambiguous=ambiguous,
            )
            for uid in sorted(uuids)
        ]


# ── 歌词索引 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LyricCandidate:
    """一次歌词片段匹配的候选。

    ``hits`` 是命中的唯一 gram 数, ``coverage = hits / 查询 gram 数``。
    ``ambiguous`` 表示第一名与第二名 hits 持平且 hits < 6 —— 片段太短
    或歌词重复度高, 调用方不应直接确定唯一歌曲。
    """

    uuid: str
    name: str
    hits: int
    coverage: float
    ambiguous: bool = False


@dataclass(frozen=True)
class CompactLyricIndex:
    """``LyricGramIndex`` 的扁平快照(可直接落盘)。

    字段语义与 :class:`mohobot.music_knowledge.index_cache.CachedSongIndex`
    一致; 这里用独立类型避免 fuzzy 依赖 index_cache。``doc_*`` 按 doc id
    (0..n-1)排序, ``grams_blob`` 按 gram 字典序拼接, ``gram_off`` /
    ``post_ptr`` 是长度为 grams+1 的偏移数组(CSR)。
    """

    gram_size: int
    doc_uuid: list[str]
    doc_name: list[str]
    doc_lyrics: list[str]           # 归一化歌词
    doc_len: array.array
    grams_blob: bytes
    gram_off: array.array
    post_ptr: array.array
    post_doc: array.array


class LyricGramIndex:
    """歌词 6-gram 倒排索引: 覆盖率 + 命中数双判据, 纯内存线程安全。

    与旧的 10 字精确滑窗相比, gram 倒排能容忍用户漏字/错字(10 字滑窗
    在短歌词上误匹配 6.7%)。阈值来自 5580 首真实库实验:

    - 长歌词(归一化后 >= 60 字): ``cov >= 0.3 and hits >= 4``,
      Top1 99.0%、误匹配 0;
    - 短歌词(< 60 字): ``cov >= 0.4 and hits >= 5`` 或
      ``cov >= 0.5 and hits >= 7``, Top1 92.5%、误匹配 0;
    - 查询归一化后 < 8 字直接返回空, 300 句日常聊天误报 0。

    倒排有两种等价布局:

    - ``dict[str, list[int]]`` / ``dict[str, memoryview]``: O(1) 查询,
      全量构建/加载要逐 gram 插入(5.4M 次);
    - 有序 ``grams_blob`` + ``bisect``: 加载零成本、内存更低, 查询
      O(log grams) 且每次比较切片 6 字节。两者 ``match()`` 结果一致,
      ``from_compact()`` 用小样本 benchmark 自动选择, 见 ``last_load_stats``。

    文档 id 用整数而非 uuid 字符串, 且不额外保存"每首歌的 gram 元组"
    (删除时按已存歌词重算), 5580 首 dict 布局实测常驻约 120MB。
    """

    def __init__(self, gram_size: int = 6) -> None:
        if gram_size < 1:
            raise ValueError("gram_size 必须 >= 1")
        self._gram_size = int(gram_size)
        self._lock = threading.RLock()
        self._postings: Optional[dict[str, list[int]]] = {}
        self._doc_id: dict[str, int] = {}      # uuid -> int
        self._doc_uuid: dict[int, str] = {}    # int -> uuid
        self._doc_name: dict[str, str] = {}    # uuid -> 歌名
        self._doc_lyrics: dict[str, str] = {}  # uuid -> 归一化歌词
        self._doc_len: dict[str, int] = {}     # uuid -> 归一化长度
        self._next_id = 0
        # 紧凑布局(方案 B: bisect)的底层数组; dict 布局下为 None。
        self._compact_source: Optional[CompactLyricIndex] = None
        self._grams_blob: Optional[bytes] = None
        self._gram_off: Optional[array.array] = None
        self._post_ptr: Optional[array.array] = None
        self._post_doc: Optional[array.array] = None
        self._post_doc_mv: Optional[memoryview] = None
        self._gram_count = 0
        # 最近一次 from_compact 的布局 benchmark 结果(便于日志/测试)。
        self.last_load_stats: dict[str, object] = {}

    # ── 构建/增量 ─────────────────────────────────────────────

    def build(
        self,
        entries: Iterable[tuple[str, str, str]],
        *,
        gc_control: bool = False,
        yield_every: int = 0,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """清空后按 ``(uuid, name, lyrics)`` 重建索引, 返回曲目数。

        歌词内部用 ``normalize.normalize_name`` 归一化: 去标点/空白/大小写,
        保留 CJK 与字母数字。建 16k 首索引约 100~150s, 只在启动后台构建
        或同步后执行, 不要在每次查询时重归一化全库。

        Args:
            entries: ``(uuid, name, lyrics)`` 迭代器; 传列表时可用进度。
            gc_control: True 时构建期间 ``gc.disable()``(实测省约 60s),
                结束后恢复原状态。仅后台线程构建使用。
            yield_every: 每处理 N 首 ``time.sleep(0)`` 让出 GIL(避免事件
                循环被纯 Python 循环长时间饿死)。
            progress: 可选回调 ``progress(done, total)``, 每 yield 一次。
        """
        import gc

        rows = entries if isinstance(entries, (list, tuple)) else list(entries)
        total = len(rows)
        was_enabled = gc.isenabled()
        with self._lock:
            self._clear_locked()
            count = 0
            if gc_control:
                gc.disable()
            try:
                for index, (uuid, name, lyrics) in enumerate(rows, 1):
                    if self._add_locked(uuid, name, lyrics):
                        count += 1
                    if yield_every and index % yield_every == 0:
                        time.sleep(0)
                        if progress is not None:
                            try:
                                progress(index, total)
                            except Exception:
                                pass
                if progress is not None and total:
                    try:
                        progress(total, total)
                    except Exception:
                        pass
            finally:
                if gc_control and was_enabled:
                    gc.enable()
            return count

    def add(self, uuid: str, name: str, lyrics: str) -> bool:
        """增量加入(或更新)一首歌; 同 uuid 重复调用会先移除旧 posting。"""
        with self._lock:
            return self._add_locked(uuid, name, lyrics)

    def _add_locked(self, uuid: str, name: str, lyrics: str) -> bool:
        uid = str(uuid or "").strip()
        if not uid:
            return False
        self._ensure_postings_dict()
        self._remove_locked(uid)
        norm = normalize_name(lyrics or "")
        grams = self._gram_set(norm)
        doc = self._next_id
        self._next_id += 1
        self._doc_id[uid] = doc
        self._doc_uuid[doc] = uid
        self._doc_name[uid] = name or ""
        self._doc_lyrics[uid] = norm
        self._doc_len[uid] = len(norm)
        postings = self._postings
        assert postings is not None
        for gram in grams:
            bucket = postings.get(gram)
            if bucket is None:
                postings[gram] = [doc]
            else:
                if not isinstance(bucket, list):
                    # 缓存加载后的 memoryview 切片不可 append, 先转 list。
                    bucket = list(bucket)
                    postings[gram] = bucket
                bucket.append(doc)
        self._compact_source = None
        return True

    def remove(self, uuid: str) -> bool:
        """移除一首歌及其全部 posting; 不存在时返回 False。"""
        with self._lock:
            return self._remove_locked(uuid)

    def _remove_locked(self, uuid: str) -> bool:
        uid = str(uuid or "").strip()
        doc = self._doc_id.pop(uid, None)
        if doc is None:
            return False
        self._ensure_postings_dict()
        postings = self._postings
        assert postings is not None
        norm = self._doc_lyrics.pop(uid, "")
        self._doc_name.pop(uid, None)
        self._doc_len.pop(uid, None)
        self._doc_uuid.pop(doc, None)
        for gram in self._gram_set(norm):
            bucket = postings.get(gram)
            if bucket is None:
                continue
            if not isinstance(bucket, list):
                bucket = list(bucket)
                postings[gram] = bucket
            try:
                bucket.remove(doc)
            except ValueError:
                pass
            if not bucket:
                del postings[gram]
        self._compact_source = None
        return True

    def clear(self) -> None:
        """清空全部索引数据。"""
        with self._lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        self._postings = {}
        self._doc_id.clear()
        self._doc_uuid.clear()
        self._doc_name.clear()
        self._doc_lyrics.clear()
        self._doc_len.clear()
        self._next_id = 0
        self._compact_source = None
        self._grams_blob = None
        self._gram_off = None
        self._post_ptr = None
        self._post_doc = None
        self._post_doc_mv = None
        self._gram_count = 0

    def size(self) -> int:
        """当前索引中的曲目数。"""
        with self._lock:
            return len(self._doc_id)

    def __len__(self) -> int:
        """当前索引中的曲目数(同 size())。"""
        with self._lock:
            return len(self._doc_id)

    def stats(self) -> dict[str, int]:
        """返回索引规模: 曲目数、唯一 gram 数、posting 总数、gram 长度。"""
        with self._lock:
            if self._postings is None:
                return {
                    "songs": len(self._doc_id),
                    "grams": self._gram_count,
                    "postings": len(self._post_doc or ()),
                    "gram_size": self._gram_size,
                }
            return {
                "songs": len(self._doc_id),
                "grams": len(self._postings),
                "postings": sum(len(ids) for ids in self._postings.values()),
                "gram_size": self._gram_size,
            }

    # ── 紧凑序列化 ────────────────────────────────────────────

    @property
    def layout(self) -> str:
        """当前倒排布局: ``dict`` 或 ``bisect``。"""
        return "dict" if self._postings is not None else "bisect"

    def to_compact(self) -> CompactLyricIndex:
        """导出扁平快照(供 ``index_cache`` 落盘)。

        排序规则固定: doc 按整数 id 升序, gram 按字符串字典序升序。
        从缓存加载且未做增量修改时直接返回原快照(零拷贝)。
        """
        with self._lock:
            if self._compact_source is not None:
                return self._compact_source
            if self._postings is None:
                self._materialize_postings_dict()
            postings = self._postings
            assert postings is not None

            doc_ids = sorted(self._doc_uuid)
            remap = {old: new for new, old in enumerate(doc_ids)}
            doc_uuid = [self._doc_uuid[old] for old in doc_ids]
            doc_name = [self._doc_name.get(uid, "") for uid in doc_uuid]
            doc_lyrics = [self._doc_lyrics.get(uid, "") for uid in doc_uuid]
            doc_len = array.array(
                "i", (len(text) for text in doc_lyrics)
            )

            grams = sorted(postings)
            grams_blob = "".join(grams).encode("utf-8")
            gram_off = array.array("Q", [0])
            post_ptr = array.array("Q", [0])
            post_doc = array.array("i")
            offset = 0
            for gram in grams:
                offset += len(gram.encode("utf-8"))
                gram_off.append(offset)
                for doc in postings[gram]:
                    post_doc.append(remap.get(doc, doc))
                post_ptr.append(len(post_doc))
            return CompactLyricIndex(
                gram_size=self._gram_size,
                doc_uuid=doc_uuid,
                doc_name=doc_name,
                doc_lyrics=doc_lyrics,
                doc_len=doc_len,
                grams_blob=grams_blob,
                gram_off=gram_off,
                post_ptr=post_ptr,
                post_doc=post_doc,
            )

    @classmethod
    def from_compact(
        cls,
        data: CompactLyricIndex,
        *,
        layout: Optional[str] = None,
        benchmark: bool = True,
    ) -> "LyricGramIndex":
        """从扁平快照恢复索引。

        Args:
            data: :meth:`to_compact` 的产物。
            layout: ``dict`` / ``bisect``; 省略时按小样本 benchmark 自动选。
            benchmark: 是否运行布局 benchmark(测试可关掉以固定行为)。
        """
        if data.gram_size < 1:
            raise ValueError("gram_size 必须 >= 1")
        obj = cls(gram_size=data.gram_size)
        docs = len(data.doc_uuid)
        if (
            len(data.doc_name) != docs
            or len(data.doc_lyrics) != docs
            or len(data.doc_len) != docs
        ):
            raise ValueError("紧凑索引 doc 列表长度不一致")
        if (
            len(data.gram_off) != len(data.post_ptr)
        ):
            raise ValueError("紧凑索引偏移数组长度不一致")

        with obj._lock:
            obj._doc_uuid = dict(enumerate(data.doc_uuid))
            obj._doc_id = {uid: i for i, uid in enumerate(data.doc_uuid)}
            obj._doc_name = {
                uid: data.doc_name[i] for i, uid in enumerate(data.doc_uuid)
            }
            obj._doc_lyrics = {
                uid: data.doc_lyrics[i] for i, uid in enumerate(data.doc_uuid)
            }
            obj._doc_len = {
                uid: int(data.doc_len[i]) for i, uid in enumerate(data.doc_uuid)
            }
            obj._next_id = docs
            obj._grams_blob = data.grams_blob
            obj._gram_off = data.gram_off
            obj._post_ptr = data.post_ptr
            obj._post_doc = data.post_doc
            obj._post_doc_mv = memoryview(data.post_doc)
            obj._gram_count = len(data.gram_off) - 1
            obj._compact_source = data
            # 先置空 dict, 否则 _materialize_postings_dict() 会误以为已展开。
            obj._postings = None
            chosen = layout
            if chosen is None:
                chosen = obj._choose_layout(benchmark=benchmark)
            if chosen == "dict":
                obj._materialize_postings_dict()
            obj.last_load_stats["layout"] = chosen
        return obj

    def _choose_layout(self, *, benchmark: bool = True) -> str:
        """在小样本上 benchmark 方案 A(dict) 与方案 B(bisect), 返回胜者。

        估计值 = 加载耗时 + 假定 200 次 gram 查询的查询耗时。dict 的加载
        成本与 gram 数线性相关(5.4M 次插入约数秒), bisect 加载几乎为零
        但每次查询多 ~23 次 6 字节切片比较。小库上 dict 往往胜出, 16k
        大库上 bisect 明显更快; 两种布局 ``match()`` 结果相同。
        """
        grams = self._gram_count
        blob = self._grams_blob or b""
        off = self._gram_off
        ptr = self._post_ptr
        mv = self._post_doc_mv
        if not benchmark or grams == 0 or off is None or ptr is None or mv is None:
            chosen = "bisect"
            self.last_load_stats.update(
                {"layout": chosen, "reason": "empty", "grams": grams}
            )
            return chosen

        sample_target = min(grams, 20000)
        stride = max(1, grams // sample_target)
        indices = list(range(0, grams, stride))[:sample_target]
        sample: dict[str, memoryview] = {}
        sample_keys: list[str] = []
        start = time.perf_counter()
        for i in indices:
            # gram 的字符数固定为 gs, 但 UTF-8 字节长度随字符变化,
            # 必须用 gram_off[i+1] 取真实结束位置。
            key = blob[off[i]:off[i + 1]].decode("utf-8")
            sample[key] = mv[ptr[i]:ptr[i + 1]]
            if len(sample_keys) < 200:
                sample_keys.append(key)
        sample_time = time.perf_counter() - start
        sample_n = max(1, len(sample))
        est_dict_setup = sample_time * grams / sample_n

        # dict 查询
        start = time.perf_counter()
        for key in sample_keys:
            ids = sample.get(key)
            if ids:
                for _doc in ids:
                    pass
        dict_query = (time.perf_counter() - start) / max(1, len(sample_keys))

        # bisect 查询(直接用完整数组)
        start = time.perf_counter()
        for key in sample_keys:
            ids = self._lookup_compact(key.encode("utf-8"))
            if ids:
                for _doc in ids:
                    pass
        bisect_query = (time.perf_counter() - start) / max(1, len(sample_keys))

        assumed_queries = 200
        dict_total = est_dict_setup + assumed_queries * dict_query
        bisect_total = 0.0005 + assumed_queries * bisect_query
        chosen = "dict" if dict_total < bisect_total else "bisect"
        self.last_load_stats.update(
            {
                "layout": chosen,
                "grams": grams,
                "sample_grams": sample_n,
                "dict_setup_ms": round(est_dict_setup * 1000, 1),
                "bisect_setup_ms": 0.5,
                "dict_query_us": round(dict_query * 1e6, 1),
                "bisect_query_us": round(bisect_query * 1e6, 1),
                "assumed_queries": assumed_queries,
            }
        )
        return chosen

    def _materialize_postings_dict(self) -> None:
        """把 CSR 数组展开成 ``{gram: memoryview}``(方案 A)。"""
        if self._postings is not None:
            return
        blob = self._grams_blob or b""
        off = self._gram_off
        ptr = self._post_ptr
        mv = self._post_doc_mv
        if off is None or ptr is None or mv is None:
            self._postings = {}
            return
        postings: dict[str, memoryview] = {}
        for i in range(self._gram_count):
            key = blob[off[i]:off[i + 1]].decode("utf-8")
            postings[key] = mv[ptr[i]:ptr[i + 1]]
        self._postings = postings

    def _ensure_postings_dict(self) -> None:
        if self._postings is None:
            self._materialize_postings_dict()

    def _lookup_compact(self, target: bytes):
        """在有序 grams_blob 上二分, 返回 ``post_doc`` 的 memoryview 切片。"""
        blob = self._grams_blob
        off = self._gram_off
        ptr = self._post_ptr
        mv = self._post_doc_mv
        if blob is None or off is None or ptr is None or mv is None:
            return None
        lo, hi = 0, self._gram_count
        while lo < hi:
            mid = (lo + hi) // 2
            if blob[off[mid]:off[mid + 1]] < target:
                lo = mid + 1
            else:
                hi = mid
        if lo < self._gram_count:
            if blob[off[lo]:off[lo + 1]] == target:
                return mv[ptr[lo]:ptr[lo + 1]]
        return None

    # ── 查询 ──────────────────────────────────────────────────

    def match(self, text: str) -> list[LyricCandidate]:
        """匹配歌词片段, 按 ``(hits, coverage)`` 降序返回候选。

        规则(阈值全部来自 5580 首真实库实验):

        - 查询归一化后 < 8 字 → 返回空(避免日常词误报);
        - 长歌词(库内归一化长度 >= 60): ``cov >= 0.3 and hits >= 4``;
        - 短歌词: ``(cov >= 0.4 and hits >= 5) or (cov >= 0.5 and hits >= 7)``;
        - 第一名与第二名 hits 持平且 hits < 6 时, 并列候选全部标记
          ``ambiguous=True``, 调用方不应据此认定唯一歌曲。

        Args:
            text: 用户消息/歌词片段, 内部自行归一化并切 gram。
        """
        norm = normalize_name(text or "")
        if len(norm) < _MIN_LYRIC_QUERY_LEN:
            return []
        query_grams = self._gram_set(norm)
        total = len(query_grams)
        if total == 0:
            return []
        with self._lock:
            counts: dict[int, int] = {}
            postings = self._postings
            if postings is not None:
                for gram in query_grams:
                    ids = postings.get(gram)
                    if not ids:
                        continue
                    for doc in ids:
                        counts[doc] = counts.get(doc, 0) + 1
            else:
                # 方案 B: 有序 grams_blob 上二分, 保持与 dict 布局相同结果。
                for gram in query_grams:
                    ids = self._lookup_compact(gram.encode("utf-8"))
                    if not ids:
                        continue
                    for doc in ids:
                        counts[doc] = counts.get(doc, 0) + 1

            passed: list[tuple[int, float, str]] = []
            for doc, hits in counts.items():
                uid = self._doc_uuid.get(doc)
                if uid is None:
                    continue
                coverage = hits / total
                lyric_len = self._doc_len.get(uid, 0)
                if lyric_len >= _LONG_LYRIC_LEN:
                    ok = coverage >= _LONG_MIN_COV and hits >= _LONG_MIN_HITS
                else:
                    ok = (
                        coverage >= _SHORT_MIN_COV_A and hits >= _SHORT_MIN_HITS_A
                    ) or (
                        coverage >= _SHORT_MIN_COV_B and hits >= _SHORT_MIN_HITS_B
                    )
                if ok:
                    passed.append((hits, coverage, uid))

            if not passed:
                return []
            passed.sort(key=lambda item: (-item[0], -item[1], item[2]))
            top_hits = passed[0][0]
            tie_ambiguous = (
                len(passed) >= 2 and top_hits - passed[1][0] < 1 and top_hits < 6
            )
            return [
                LyricCandidate(
                    uuid=uid,
                    name=self._doc_name.get(uid, ""),
                    hits=hits,
                    coverage=coverage,
                    ambiguous=bool(tie_ambiguous and hits == top_hits),
                )
                for hits, coverage, uid in passed
            ]

    def _gram_set(self, norm: str) -> set[str]:
        """把归一化文本切成去重 gram 集合(不足 gram_size 时为空)。"""
        size = self._gram_size
        if len(norm) < size:
            return set()
        return {norm[i : i + size] for i in range(len(norm) - size + 1)}
