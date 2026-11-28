"""图片缓存 LRU 淘汰回归测试 — Issue #137 的 image_cache 子项。

原实现用 list 保存 `removed` 并在 json_update 里做 `k not in removed`,
10k 键淘汰 9700 个时成员判断是 O(n²)。改为 set 后结果集合必须完全不变。
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.file_store import json_write
from mohobot.image_cache import ImageCache

MB = 1024 * 1024


def _make_entries(tmp: Path, count: int, size: int = MB) -> dict:
    entries = {}
    for index in range(count):
        filepath = tmp / f"img_{index}.jpg"
        filepath.write_bytes(b"x" * 16)
        url = f"https://example.com/{index}.jpg"
        entries[url] = {
            "path": str(filepath),
            "size": size,
            "cached_at": float(index),
        }
    return entries


async def test_eviction_removes_oldest_and_updates_map():
    from mohobot.file_store import json_read as read_map

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cache = ImageCache(cache_dir=str(tmp / "cache"))
        cache._max_size = 5 * MB
        entries = _make_entries(tmp, 10)
        await json_write(cache._map_path, entries)

        await cache._evict_if_needed(entries)

        remaining = await read_map(cache._map_path)
        expected_kept = {f"https://example.com/{i}.jpg" for i in range(5, 10)}
        assert set(remaining) == expected_kept, (set(remaining), expected_kept)
        for index in range(10):
            exists = (tmp / f"img_{index}.jpg").exists()
            assert exists is (index >= 5), (index, exists)


async def test_eviction_noop_when_under_limit():
    from mohobot.file_store import json_read as read_map

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cache = ImageCache(cache_dir=str(tmp / "cache"))
        cache._max_size = 100 * MB
        entries = _make_entries(tmp, 5)
        await json_write(cache._map_path, entries)

        await cache._evict_if_needed(entries)

        remaining = await read_map(cache._map_path)
        assert set(remaining) == set(entries)
        assert all((tmp / f"img_{index}.jpg").exists() for index in range(5))


def main() -> None:
    import asyncio

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if asyncio.iscoroutinefunction(t):
            asyncio.run(t())
        else:
            t()
        print(f"PASS {t.__name__}")
    print("\nALL IMAGE CACHE EVICTION TESTS PASSED")


if __name__ == "__main__":
    main()
