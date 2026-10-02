"""SemanticMapRoutes：WebUI 语义空间可视化。

点的位置是真实嵌入向量经 UMAP 压缩后的 2 维坐标（原始向量、欧氏距离、不归一化）；
距离、密度和淘汰预览全部在 n 维原始向量上计算，与冗余淘汰使用同一套实现。
UMAP 不保留距离尺度，前端按圆心附近的局部比例把 r0 换算成近似圆。
"""

import asyncio
import time
from typing import Any

import numpy as np
from ..web.http import jsonify, request

from ..host import logger

from ..core.maintenance.retention import (
    SECONDS_PER_DAY,
    is_auto_evictable,
    library_counts,
    library_group,
    nonnegative_number,
    redundancy_eviction_candidates,
    usage_rate,
)
from ..core.search.semantic_space import (
    distances_from,
    nearest_neighbor_distances,
    project_2d,
    radius_neighbors,
)


class SemanticMapRoutes:
    SEMANTIC_MAP_MAX_POINTS = 5000

    def _embedding_service(self):
        selector = getattr(self.plugin, "meme_selector", None)
        return getattr(selector, "embedding_service", None)

    def _semantic_state(self) -> dict[str, Any] | None:
        """向量库未变化时复用投影和最近邻结果。"""
        service = self._embedding_service()
        if service is None:
            return None
        paths, matrix = service.matrix_snapshot()
        index = self._get_index()
        if matrix is None:
            return {"index": index, "paths": [], "mat": None}

        cache = getattr(self, "_semantic_cache", None)
        signature = (id(matrix), len(paths), len(index))
        if cache and cache.get("signature") == signature:
            return cache

        keep = [i for i, p in enumerate(paths) if isinstance(index.get(p), dict)]
        keep = keep[: self.SEMANTIC_MAP_MAX_POINTS]
        state = {
            "signature": signature,
            "index": index,
            "paths": [paths[i] for i in keep],
            "mat": np.ascontiguousarray(matrix[keep]) if keep else None,
            "truncated": len(paths) > len(keep) and len(keep) == self.SEMANTIC_MAP_MAX_POINTS,
        }
        self._semantic_cache = state
        return state

    @staticmethod
    def _compute_layout(mat: np.ndarray) -> dict[str, Any]:
        coords, projection = project_2d(mat)
        nn = nearest_neighbor_distances(mat)
        rng = np.random.default_rng(0)
        rows = rng.choice(len(mat), size=min(len(mat), 200), replace=False)
        sample = np.concatenate([distances_from(mat, int(i)) for i in rows])
        sample = sample[sample > 0]
        return {
            "coords": coords,
            "projection": projection,
            "auto_radius": float(np.median(nn)) if nn.size else 0.0,
            "median_pairwise": float(np.median(sample)) if sample.size else 0.0,
        }

    def _point_payload(self, path: str, meta: dict, now: float, grace_days: float) -> dict:
        group = library_group(meta)
        created = nonnegative_number(meta.get("created_at"), now)
        protected = ""
        if group != "general":
            protected = "favorite" if group == "favorites" else "character"
        elif not is_auto_evictable(meta):
            protected = "external"
        elif now - created < grace_days * SECONDS_PER_DAY:
            protected = "grace"
        return {
            "hash": str(meta.get("hash", "") or ""),
            "uses": int(nonnegative_number(meta.get("use_count"))),
            "rate": round(usage_rate(meta, now), 4),
            "age_days": round(max(0.0, now - created) / SECONDS_PER_DAY, 1),
            "protected": protected,
            "desc": str(meta.get("desc", "") or "")[:80],
            "overlay_text": str(meta.get("overlay_text", "") or "")[:40],
            "category": str(meta.get("category", "") or ""),
        }

    def _eviction_settings(self, data: dict | None = None) -> tuple[float, int, float]:
        cfg = self._cfg
        data = data or {}

        def _num(key, default, cast):
            raw = data.get(key)
            if raw is None or raw == "":
                raw = getattr(cfg, key, default)
            try:
                return cast(raw)
            except (TypeError, ValueError):
                return default

        radius = max(0.0, _num("eviction_radius", 0.0, float))
        k = max(1, min(50, _num("eviction_min_neighbors", 3, int)))
        grace = max(0.0, _num("eviction_grace_days", 7, float))
        return radius, k, grace

    async def handle_semantic_map(self):
        try:
            state = self._semantic_state()
            if state is None:
                return jsonify({"success": False, "error": "语义检索服务不可用"})
            index = state["index"]
            counts = library_counts(index)
            radius, k, grace = self._eviction_settings()
            base = {
                "success": True,
                "embedding_enabled": bool(getattr(self._cfg, "enable_embedding_search", False)),
                "library_count": counts["automatic"],
                "cap": int(getattr(self._cfg, "max_reg_num", 0) or 0),
                "total": len(index),
                "radius": radius,
                "min_neighbors": k,
                "grace_days": grace,
            }
            if state["mat"] is None or len(state["paths"]) < 2:
                return jsonify({**base, "points": [], "missing": len(index)})

            if "layout" not in state:
                state["layout"] = await asyncio.to_thread(self._compute_layout, state["mat"])
            layout = state["layout"]
            effective = radius if radius > 0 else layout["auto_radius"]
            neighbors = await asyncio.to_thread(radius_neighbors, state["mat"], effective)

            now = time.time()
            points = []
            for i, path in enumerate(state["paths"]):
                point = self._point_payload(path, index[path], now, grace)
                point["x"] = round(float(layout["coords"][i, 0]), 5)
                point["y"] = round(float(layout["coords"][i, 1]), 5)
                point["density"] = int(len(neighbors[i]))
                points.append(point)

            return jsonify(
                {
                    **base,
                    "points": points,
                    "dim": int(state["mat"].shape[1]),
                    "missing": max(0, len(index) - len(points)),
                    "truncated": bool(state.get("truncated")),
                    "projection": layout["projection"],
                    "auto_radius": round(layout["auto_radius"], 5),
                    "effective_radius": round(effective, 5),
                    "radius_max": round(max(layout["median_pairwise"], layout["auto_radius"] * 4), 5),
                }
            )
        except Exception as e:
            logger.error(f"语义空间数据生成失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_semantic_density(self):
        """给定半径下每个点在 n 维空间中的邻居数（顺序与 semantic-map 的 points 一致）。"""
        try:
            state = self._semantic_state()
            if not state or state.get("mat") is None:
                return jsonify({"success": False, "error": "暂无向量数据"})
            try:
                radius = max(0.0, float(request.args.get("radius", 0)))
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "radius 无效"})
            neighbors = await asyncio.to_thread(radius_neighbors, state["mat"], radius)
            return jsonify({"success": True, "density": [int(len(n)) for n in neighbors]})
        except Exception as e:
            logger.error(f"语义密度计算失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_semantic_distances(self):
        """选中点到全部点的 n 维欧氏距离，前端据此高亮真正落在 r0 内的邻居。"""
        try:
            state = self._semantic_state()
            if not state or state.get("mat") is None:
                return jsonify({"success": False, "error": "暂无向量数据"})
            target = str(request.args.get("hash", "") or "").strip()
            index = state["index"]
            position = next(
                (
                    i
                    for i, p in enumerate(state["paths"])
                    if str(index[p].get("hash", "") or "") == target
                ),
                None,
            )
            if position is None:
                return jsonify({"success": False, "error": "该表情没有向量"})
            dist = distances_from(state["mat"], position)
            return jsonify(
                {"success": True, "distances": [round(float(d), 5) for d in dist]}
            )
        except Exception as e:
            logger.error(f"语义距离计算失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_semantic_eviction_preview(self):
        """按给定 r0 / K 预览现在执行淘汰会删除哪些表情（不实际删除）。"""
        try:
            state = self._semantic_state()
            if not state or state.get("mat") is None:
                return jsonify({"success": False, "error": "暂无向量数据"})
            radius, k, grace = self._eviction_settings(
                {
                    "eviction_radius": request.args.get("radius"),
                    "eviction_min_neighbors": request.args.get("min_neighbors"),
                }
            )
            index = state["index"]
            vectors = {p: state["mat"][i] for i, p in enumerate(state["paths"])}
            removed, used = await asyncio.to_thread(
                redundancy_eviction_candidates,
                index,
                vectors,
                radius=radius,
                min_neighbors=k,
                grace_days=grace,
            )
            counts = library_counts(index)
            cap = int(getattr(self._cfg, "max_reg_num", 0) or 0)
            return jsonify(
                {
                    "success": True,
                    "hashes": [str(index[p].get("hash", "") or "") for p, _ in removed],
                    "radius": round(used, 5),
                    "over_cap": counts["automatic"] > cap,
                }
            )
        except Exception as e:
            logger.error(f"淘汰预览失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_semantic_settings(self):
        """保存 r0 与 K 为冗余淘汰参数。"""
        try:
            data = await request.get_json() or {}
            radius, k, _ = self._eviction_settings(
                {
                    "eviction_radius": data.get("radius"),
                    "eviction_min_neighbors": data.get("min_neighbors"),
                }
            )
            if not self.plugin.update_config(
                {"eviction_radius": radius, "eviction_min_neighbors": k}
            ):
                return jsonify({"success": False, "error": "配置保存失败"})
            return jsonify({"success": True, "radius": radius, "min_neighbors": k})
        except Exception as e:
            logger.error(f"保存淘汰参数失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})
