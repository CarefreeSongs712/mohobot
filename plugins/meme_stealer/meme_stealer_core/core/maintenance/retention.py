"""Library ownership, soft capacity limits and semantic-redundancy eviction."""

import math
import time

import numpy as np

from ..search.semantic_space import radius_neighbors, resolve_radius, stack_vectors

SECONDS_PER_DAY = 86400


def library_group(meta: dict) -> str:
    """Favorites take precedence without changing the stored character."""
    if meta.get("is_favorite"):
        return "favorites"
    if str(meta.get("character") or "").strip():
        return "characters"
    return "general"


def is_auto_evictable(meta: object) -> bool:
    return (
        isinstance(meta, dict)
        and library_group(meta) == "general"
        and str(meta.get("retention_class") or "native").strip()
        not in {"external", "pinned"}
    )


def library_counts(index: dict) -> dict[str, int]:
    counts = dict(general=0, favorites=0, characters=0, automatic=0)
    for meta in index.values():
        if isinstance(meta, dict):
            counts[library_group(meta)] += 1
            counts["automatic"] += int(is_auto_evictable(meta))
    return counts


def nonnegative_number(value: object, default: float = 0) -> float:
    try:
        number = float(value)
        return max(0, number) if math.isfinite(number) else default
    except (ValueError, TypeError, OverflowError):
        return default


def soft_limit_chance(
    base: float, count: int, cap: int, exponent: float = 2.0, floor: float = 0.02
) -> float:
    """超过上限后按 base·(cap/count)^k 衰减，且不低于 min(base, floor)。"""
    base = max(0.0, float(base))
    if cap <= 0 or count <= cap or base == 0:
        return base
    exponent = max(0.0, float(exponent))
    decayed = base * (cap / count) ** exponent
    return max(min(base, max(0.0, float(floor))), decayed)


def usage_rate(meta: dict, now: float) -> float:
    """使用率 = 使用次数 / 入库天数（不足一天按一天计）。"""
    uses = nonnegative_number(meta.get("use_count"))
    created = nonnegative_number(meta.get("created_at"), now)
    age_days = max(1.0, (now - created) / SECONDS_PER_DAY)
    return uses / age_days


def redundancy_eviction_candidates(
    index: dict,
    vectors: dict[str, np.ndarray],
    *,
    radius: float = 0.0,
    min_neighbors: int = 3,
    grace_days: float = 7,
    now: float | None = None,
) -> tuple[list[tuple[str, int]], float]:
    """语义冗余淘汰：只删使用率低、且 n 维半径 r0 内邻居足够多的表情。

    - 邻居统计覆盖全库有向量的表情（收藏、角色、外部导入也占据语义空间），
      但只有可自动淘汰的通用表情会被删除。
    - 低使用率：不高于可淘汰集合的使用率中位数；入库未满 grace_days 天的不参与。
    - 依次处理使用率最低、密度最高的候选；每删除一张，其邻居的密度减一，
      密度降到 K 以下的不再删除，因此一簇相似表情里使用最多的会被留下。

    Returns:
        (待删除 [(path, created_at)], 实际使用的半径 r0)
    """
    now = time.time() if now is None else float(now)
    paths, mat = stack_vectors(
        {p: v for p, v in vectors.items() if isinstance(index.get(p), dict)}
    )
    if len(paths) < 2:
        return [], 0.0
    r0 = resolve_radius(radius, mat)
    k = max(1, int(min_neighbors))
    neighbors = radius_neighbors(mat, r0)
    density = np.array([len(n) for n in neighbors], dtype=np.int64)

    grace_seconds = max(0.0, float(grace_days)) * SECONDS_PER_DAY
    eligible: list[int] = []
    rates: dict[int, float] = {}
    for i, path in enumerate(paths):
        meta = index[path]
        if not is_auto_evictable(meta):
            continue
        created = nonnegative_number(meta.get("created_at"), now)
        if now - created < grace_seconds:
            continue
        eligible.append(i)
        rates[i] = usage_rate(meta, now)
    if not eligible:
        return [], r0

    median_rate = float(np.median([rates[i] for i in eligible]))
    order = sorted(
        (i for i in eligible if density[i] >= k and rates[i] <= median_rate),
        key=lambda i: (
            rates[i],
            -int(density[i]),
            nonnegative_number(index[paths[i]].get("created_at")),
            paths[i],
        ),
    )
    alive = np.ones(len(paths), dtype=bool)
    removed: list[tuple[str, int]] = []
    for i in order:
        if density[i] < k:
            continue
        alive[i] = False
        for j in neighbors[i]:
            if alive[j]:
                density[j] -= 1
        removed.append(
            (paths[i], int(nonnegative_number(index[paths[i]].get("created_at"))))
        )
    return removed, r0


def eviction_candidates(index: dict, limit: int, usage_weight: float = 0.7) -> list[tuple[str, int]]:
    """无向量时的兜底：按低使用次数与入库时间加权，删到上限为止。

    Only eligible entries contribute to either normalization or the quota.
    Equal scores prefer lower usage, then older creation, then stable path.
    """
    if limit <= 0:
        return []
    items = [
        (path, nonnegative_number(meta.get("use_count")),
         int(nonnegative_number(meta.get("created_at"))))
        for path, meta in index.items() if is_auto_evictable(meta)
    ]
    overflow = len(items) - limit
    if overflow <= 0:
        return []
    weight = min(1, nonnegative_number(usage_weight, 0.7))
    min_usage = min(item[1] for item in items)
    usage_span = max(item[1] for item in items) - min_usage
    min_created = min(item[2] for item in items)
    age_span = max(item[2] for item in items) - min_created

    def sort_key(item):
        path, usage, created = item
        low_usage = 1 - (usage - min_usage) / usage_span if usage_span else 0
        old_age = 1 - (created - min_created) / age_span if age_span else 0
        score = weight * low_usage + (1 - weight) * old_age
        return -score, usage, created, path

    items.sort(key=sort_key)
    return [(path, created) for path, _, created in items[:overflow]]
