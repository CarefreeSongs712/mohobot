"""语义空间工具：n 维向量的半径邻域、自动半径与 2 维投影。

冗余淘汰与 WebUI 可视化共用这里的实现，保证两边看到的是同一组数字。
距离一律使用原始向量的欧氏距离，不做归一化。
"""

from __future__ import annotations

import numpy as np

from ...host import logger

UMAP_NEIGHBORS = 15
# 较大的 min_dist 让同一簇里的点彼此分开，WebUI 里每个点都能单独悬停查看。
UMAP_MIN_DIST = 0.5

DISTANCE_CHUNK_ROWS = 512


def stack_vectors(vector_map: dict[str, np.ndarray]) -> tuple[list[str], np.ndarray]:
    """把 {path: 向量} 堆成矩阵；维度不一致的少数派向量被丢弃。"""
    if not vector_map:
        return [], np.zeros((0, 0), dtype=np.float32)
    dims: dict[int, int] = {}
    for vec in vector_map.values():
        dims[len(vec)] = dims.get(len(vec), 0) + 1
    dim = max(dims, key=dims.get)
    paths = [path for path, vec in vector_map.items() if len(vec) == dim]
    mat = np.stack([np.asarray(vector_map[p], dtype=np.float32) for p in paths])
    return paths, mat


def _distance_rows(mat: np.ndarray, start: int, stop: int, sq_norms: np.ndarray) -> np.ndarray:
    block = mat[start:stop]
    sq = sq_norms[start:stop, None] + sq_norms[None, :] - 2.0 * (block @ mat.T)
    np.maximum(sq, 0.0, out=sq)
    return np.sqrt(sq, dtype=np.float32)


def distances_from(mat: np.ndarray, index: int) -> np.ndarray:
    """单个点到全部点的欧氏距离。"""
    if mat.size == 0:
        return np.zeros(0, dtype=np.float32)
    diff = mat - mat[index]
    return np.sqrt(np.einsum("ij,ij->i", diff, diff), dtype=np.float32)


def radius_neighbors(mat: np.ndarray, radius: float) -> list[np.ndarray]:
    """每个点在半径 radius（不含边界外、含自身之外）内的邻居下标。"""
    n = len(mat)
    if n == 0 or radius <= 0:
        return [np.zeros(0, dtype=np.int64) for _ in range(n)]
    sq_norms = np.einsum("ij,ij->i", mat, mat)
    result: list[np.ndarray] = []
    for start in range(0, n, DISTANCE_CHUNK_ROWS):
        stop = min(n, start + DISTANCE_CHUNK_ROWS)
        dist = _distance_rows(mat, start, stop, sq_norms)
        for offset, row in enumerate(dist):
            hits = np.flatnonzero(row <= radius)
            result.append(hits[hits != start + offset])
    return result


def nearest_neighbor_distances(mat: np.ndarray) -> np.ndarray:
    """每个点到最近其他点的距离；只有一个点时为空数组。"""
    n = len(mat)
    if n < 2:
        return np.zeros(0, dtype=np.float32)
    sq_norms = np.einsum("ij,ij->i", mat, mat)
    out = np.empty(n, dtype=np.float32)
    for start in range(0, n, DISTANCE_CHUNK_ROWS):
        stop = min(n, start + DISTANCE_CHUNK_ROWS)
        dist = _distance_rows(mat, start, stop, sq_norms)
        for offset in range(stop - start):
            dist[offset, start + offset] = np.inf
        out[start:stop] = dist.min(axis=1)
    return out


def auto_radius(mat: np.ndarray) -> float:
    """r0 的自动值：全库最近邻距离的中位数。"""
    nn = nearest_neighbor_distances(mat)
    return float(np.median(nn)) if nn.size else 0.0


def resolve_radius(configured: float, mat: np.ndarray) -> float:
    try:
        value = float(configured)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else auto_radius(mat)


def umap_2d(mat: np.ndarray) -> np.ndarray:
    """UMAP 压缩到 2 维：直接使用原始向量与欧氏距离，不做归一化。

    UMAP 保留的是邻近关系而不是距离尺度，图上的坐标单位是任意的。
    """
    import umap

    n = len(mat)
    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=max(2, min(UMAP_NEIGHBORS, n - 1)),
        min_dist=UMAP_MIN_DIST,
        metric="euclidean",
    )
    return np.asarray(reducer.fit_transform(mat), dtype=np.float32)


def project_2d(mat: np.ndarray) -> tuple[np.ndarray, str]:
    """WebUI 用的 2 维投影：优先 UMAP，点太少或 UMAP 不可用时退回 PCA。

    Returns:
        (coords[N, 2], "umap" | "pca")
    """
    if len(mat) >= 4:
        try:
            return umap_2d(mat), "umap"
        except Exception as e:
            logger.warning(f"[语义空间] UMAP 投影失败，改用 PCA: {e}")
    coords, _ = pca_2d(mat)
    return coords, "pca"


def pca_2d(mat: np.ndarray, *, iterations: int = 30, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """中心化后投影到前两个主成分。

    只平移不缩放：主成分是正交单位向量，因此 2 维距离不会超过 n 维真实距离，
    n 维中落在 r0 内的点在图上一定落在同半径的圆内（圆内也可能混入更远的点）。

    Returns:
        (coords[N, 2], explained_variance_ratio[2])
    """
    n = len(mat)
    if n == 0:
        return np.zeros((0, 2), dtype=np.float32), np.zeros(2, dtype=np.float32)
    centered = (mat - mat.mean(axis=0)).astype(np.float64)
    total_var = float(np.einsum("ij,ij->", centered, centered))
    if n < 3 or total_var <= 0:
        coords = np.zeros((n, 2), dtype=np.float32)
        k = min(2, centered.shape[1])
        coords[:, :k] = centered[:, :k]
        return coords, np.zeros(2, dtype=np.float32)

    # 子空间迭代求前两个主成分，避免对大矩阵做完整 SVD。
    rng = np.random.default_rng(seed)
    basis = rng.standard_normal((centered.shape[1], 2))
    basis, _ = np.linalg.qr(basis)
    for _ in range(iterations):
        basis = centered.T @ (centered @ basis)
        basis, _ = np.linalg.qr(basis)
    projected = centered @ basis
    # 在二维子空间内再对角化一次，让第一轴对应最大方差。
    eigvals, eigvecs = np.linalg.eigh(projected.T @ projected)
    order = np.argsort(eigvals)[::-1]
    projected = projected @ eigvecs[:, order]
    ratio = np.clip(eigvals[order] / total_var, 0.0, 1.0)
    return projected.astype(np.float32), ratio.astype(np.float32)
