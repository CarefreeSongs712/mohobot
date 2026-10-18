"""歌曲名/歌词模糊匹配索引 — 纯内存、线程安全、不依赖数据库。

实现阈值来自 5580 首真实歌曲库的离线实验(见 docs/DEVELOPMENT.md 歌曲知识一节):

- 歌名候选流水线: 归一化精确 → 去括号核名精确 → (长度 >= 4 时)
  Damerau-Levenshtein 距离 <= 1 且候选唯一。实测 Top1 68.1%、误匹配 0.30%,
  300 句日常聊天误报 0/300; 单纯 difflib ratio@0.86 只有 25.8%, 不采用。
- 歌词: 6-gram 倒排 + 覆盖率/命中数双判据。长歌词(归一化后 >= 60 字)
  cov >= 0.3 且 hits >= 4 → Top1 99.0%、误匹配 0%; 短歌词(< 60 字)
  cov >= 0.4 且 hits >= 5, 或 cov >= 0.5 且 hits >= 7 → Top1 92.5%、
  误匹配 0%。旧的 10 字精确滑窗在短歌词上误匹配 6.7%。
- 性能: 6-gram 倒排单次查询 0.015ms, 5580 首建索引约 2.6s; 歌名 Damerau
  全扫 5.3ms, 按长度分桶 2.3ms, 2 万首规模可用。

接口约定: 本模块不 import sqlalchemy、不读数据库、不写文件; 调用方负责从
库里取数据后调用 build()。不提供 pickle 持久化 —— 全库重建成本低, 而索引
格式一旦落盘就要长期承担版本兼容成本。

所有公开函数/类均可在多线程下安全调用(内部 threading.RLock); match()
在锁内完成查询, 增量 add()/remove() 与查询互斥。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

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

    def build(self, entries: Iterable[tuple[str, str]]) -> int:
        """清空后按 ``(name, uuid)`` 重建索引, 返回载入的曲目数。

        调用方从数据库取出全部歌名后一次性构建; 建索引只做归一化与分桶,
        5580 首约 0.2s, 不需要每次查询重归一化全库。
        """
        with self._lock:
            self._clear_locked()
            count = 0
            for name, uuid in entries:
                if self._add_locked(name, uuid):
                    count += 1
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


class LyricGramIndex:
    """歌词 6-gram 倒排索引: 覆盖率 + 命中数双判据, 纯内存线程安全。

    与旧的 10 字精确滑窗相比, gram 倒排能容忍用户漏字/错字(10 字滑窗
    在短歌词上误匹配 6.7%)。阈值来自 5580 首真实库实验:

    - 长歌词(归一化后 >= 60 字): ``cov >= 0.3 and hits >= 4``,
      Top1 99.0%、误匹配 0;
    - 短歌词(< 60 字): ``cov >= 0.4 and hits >= 5`` 或
      ``cov >= 0.5 and hits >= 7``, Top1 92.5%、误匹配 0;
    - 查询归一化后 < 8 字直接返回空, 300 句日常聊天误报 0。

    内存: 倒排为 ``dict[str, list[int]]``(gram -> 文档整数 id 列表)。
    文档 id 用整数而非 uuid 字符串, 且不额外保存"每首歌的 gram 元组"
    (删除时按已存歌词重算), 5580 首实测常驻约 120MB,
    比 ``dict[str, set[str]]`` 方案省约 4 倍。
    """

    def __init__(self, gram_size: int = 6) -> None:
        if gram_size < 1:
            raise ValueError("gram_size 必须 >= 1")
        self._gram_size = int(gram_size)
        self._lock = threading.RLock()
        self._postings: dict[str, list[int]] = {}
        self._doc_id: dict[str, int] = {}      # uuid -> int
        self._doc_uuid: dict[int, str] = {}    # int -> uuid
        self._doc_name: dict[str, str] = {}    # uuid -> 歌名
        self._doc_lyrics: dict[str, str] = {}  # uuid -> 归一化歌词
        self._doc_len: dict[str, int] = {}     # uuid -> 归一化长度
        self._next_id = 0

    # ── 构建/增量 ─────────────────────────────────────────────

    def build(self, entries: Iterable[tuple[str, str, str]]) -> int:
        """清空后按 ``(uuid, name, lyrics)`` 重建索引, 返回曲目数。

        歌词内部用 ``normalize.normalize_name`` 归一化: 去标点/空白/大小写,
        保留 CJK 与字母数字。建 5580 首索引约 2.6s, 调用方应在启动/同步后
        一次性构建, 不要在每次查询时重归一化全库。
        """
        with self._lock:
            self._clear_locked()
            count = 0
            for uuid, name, lyrics in entries:
                if self._add_locked(uuid, name, lyrics):
                    count += 1
            return count

    def add(self, uuid: str, name: str, lyrics: str) -> bool:
        """增量加入(或更新)一首歌; 同 uuid 重复调用会先移除旧 posting。"""
        with self._lock:
            return self._add_locked(uuid, name, lyrics)

    def _add_locked(self, uuid: str, name: str, lyrics: str) -> bool:
        uid = str(uuid or "").strip()
        if not uid:
            return False
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
        for gram in grams:
            bucket = postings.get(gram)
            if bucket is None:
                postings[gram] = [doc]
            else:
                bucket.append(doc)
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
        norm = self._doc_lyrics.pop(uid, "")
        self._doc_name.pop(uid, None)
        self._doc_len.pop(uid, None)
        self._doc_uuid.pop(doc, None)
        for gram in self._gram_set(norm):
            bucket = self._postings.get(gram)
            if bucket is None:
                continue
            try:
                bucket.remove(doc)
            except ValueError:
                pass
            if not bucket:
                del self._postings[gram]
        return True

    def clear(self) -> None:
        """清空全部索引数据。"""
        with self._lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        self._postings.clear()
        self._doc_id.clear()
        self._doc_uuid.clear()
        self._doc_name.clear()
        self._doc_lyrics.clear()
        self._doc_len.clear()
        self._next_id = 0

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
            return {
                "songs": len(self._doc_id),
                "grams": len(self._postings),
                "postings": sum(len(ids) for ids in self._postings.values()),
                "gram_size": self._gram_size,
            }

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
            for gram in query_grams:
                ids = postings.get(gram)
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
