"""#137: file_store 锁表不再按历史唯一路径无界保留, 互斥语义不变。"""

import asyncio
import gc
import tempfile
from pathlib import Path

from mohobot import file_store


async def test_lock_registry_does_not_retain_historical_paths():
    """大量一次性文件读写后, 已无引用的锁应被回收。"""
    tmp = tempfile.mkdtemp()
    for i in range(300):
        await file_store.json_write(Path(tmp) / f"f{i}.json", {"i": i})
    gc.collect()
    assert len(file_store._file_locks) < 300


async def test_json_update_serializes_concurrent_writers(tmp_path):
    """锁被回收/重建后仍必须串行化读改写, 不能丢更新。"""
    path = tmp_path / "counter.json"
    await file_store.json_write(path, {"n": 0})

    async def bump() -> None:
        for _ in range(10):
            await file_store.json_update(
                path, lambda cur: {"n": (cur or {}).get("n", 0) + 1}, default={},
            )

    await asyncio.gather(*[bump() for _ in range(10)])
    assert await file_store.json_read(path) == {"n": 100}


async def test_open_jsonl_writer_keeps_lock_registered(tmp_path):
    """JSONLWriter 持有锁强引用: 即使其它路径的锁被回收, 该路径仍复用同一把锁。"""
    path = tmp_path / "log.jsonl"
    writer = file_store.JSONLWriter(path)
    await writer.append({"a": 1})
    key = str(path.absolute())

    # 制造大量其它路径的锁并触发回收
    tmp2 = tempfile.mkdtemp()
    for i in range(300):
        await file_store.json_write(Path(tmp2) / f"x{i}.json", {"i": i})
    gc.collect()

    assert key in file_store._file_locks
    lock = file_store._get_lock(key)
    async with lock:
        assert lock.locked()
    await writer.close()
