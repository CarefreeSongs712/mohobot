"""mohobot.file_store.jsonl_read_tail 反向分块读测试(issue #136)。

旧实现 `await f.read()` 整文件读入, 15MB 群归档单次 ~128ms/59MB 且在事件
循环内阻塞; 新实现按 64KiB 反向分块, 语义必须与旧实现完全一致:
  - 只回尾部 n 个非空行;
  - 空行跳过、坏 JSON 行跳过;
  - 最后一行无换行也照常返回;
  - 多字节 UTF-8 被分块边界切断不影响结果;
  - 文件不存在 / n<=0 返回 []。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from mohobot.file_store import jsonl_read_tail


def _run(coro):
    return asyncio.run(coro)


def _write(path: Path, lines: list[str], trailing_newline: bool = True) -> None:
    text = "\n".join(lines)
    if trailing_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def test_tail_reads_last_lines_from_large_file():
    """大文件(跨多个 64KiB 块)只回尾部 n 行, 内容与全量读一致。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "big.jsonl"
        records = [{"i": i, "text": "x" * 1000} for i in range(300)]
        _write(path, [json.dumps(r) for r in records])

        got = _run(jsonl_read_tail(path, n=5))
        assert got == records[-5:]


def test_tail_keeps_lines_when_boundary_splits_multibyte_char():
    """分块边界切到多字节字符中间时, 尾部行仍必须完整解码。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "cn.jsonl"
        # 单行超过 64KiB, 且中文让关键字符正好可能跨块; 最后再跟一行哨兵。
        big = {"text": "中文内容" * 20000}
        records = [{"i": 0, "text": "前" * 10}, big, {"i": 2, "text": "尾部哨兵"}]
        _write(path, [json.dumps(r, ensure_ascii=False) for r in records])

        got = _run(jsonl_read_tail(path, n=1))
        assert got == [records[-1]]

        got2 = _run(jsonl_read_tail(path, n=2))
        assert got2 == records[-2:]
        assert got2[0]["text"] == big["text"]


def test_tail_without_trailing_newline_returns_last_line():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "noeol.jsonl"
        _write(path, [json.dumps({"i": 0}), json.dumps({"i": 1})],
               trailing_newline=False)
        assert _run(jsonl_read_tail(path, n=1)) == [{"i": 1}]
        assert _run(jsonl_read_tail(path, n=10)) == [{"i": 0}, {"i": 1}]


def test_tail_skips_blank_and_malformed_lines():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "bad.jsonl"
        _write(path, [
            json.dumps({"i": 0}),
            "",
            "   ",
            "{not json",
            json.dumps({"i": 1}),
            "",
        ])
        got = _run(jsonl_read_tail(path, n=3))
        # 尾部 3 个非空行 = {"i":0} + 坏行 + {"i":1}; 坏行被跳过
        assert got == [{"i": 0}, {"i": 1}]


def test_tail_returns_all_lines_when_n_exceeds_file():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "small.jsonl"
        records = [{"i": i} for i in range(3)]
        _write(path, [json.dumps(r) for r in records])
        assert _run(jsonl_read_tail(path, n=100)) == records


def test_tail_missing_file_and_non_positive_n():
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "missing.jsonl"
        assert _run(jsonl_read_tail(missing, n=3)) == []

        path = Path(td) / "small.jsonl"
        _write(path, [json.dumps({"i": 0})])
        assert _run(jsonl_read_tail(path, n=0)) == []
        assert _run(jsonl_read_tail(path, n=-5)) == []


def test_tail_does_not_load_whole_file():
    """读取量必须与 n 成正比: 15MB 文件读 n=3 不能把整文件读进内存。

    通过替换 aiofiles.open 包装记录累计 read 字节数来验证; 不用 mock 数据源,
    仍读真实临时文件。
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "huge.jsonl"
        line = json.dumps({"text": "y" * 500})
        _write(path, [line] * 30000)  # ~15.5MB
        size = path.stat().st_size
        assert size > 10 * 1024 * 1024

        import aiofiles

        real_open = aiofiles.open
        counter = {"bytes": 0}

        class _Reader:
            def __init__(self, fh):
                self._fh = fh

            async def __aenter__(self):
                await self._fh.__aenter__()
                return self

            async def __aexit__(self, *exc):
                return await self._fh.__aexit__(*exc)

            async def seek(self, *args):
                return await self._fh.seek(*args)

            async def tell(self):
                return await self._fh.tell()

            async def read(self, *args):
                data = await self._fh.read(*args)
                counter["bytes"] += len(data)
                return data

        class _Opener:
            def __init__(self, *args, **kwargs):
                self._cm = real_open(*args, **kwargs)

            async def __aenter__(self):
                return _Reader(await self._cm.__aenter__())

            async def __aexit__(self, *exc):
                return await self._cm.__aexit__(*exc)

        def fake_open(*args, **kwargs):
            return _Opener(*args, **kwargs)

        import mohobot.file_store as fs

        original = fs.aiofiles.open
        fs.aiofiles.open = fake_open
        try:
            got = _run(jsonl_read_tail(path, n=3))
        finally:
            fs.aiofiles.open = original

        assert got == [{"text": "y" * 500}] * 3
        # 允许跨块多读一些, 但绝不能接近整个文件
        assert counter["bytes"] < 1024 * 1024, counter["bytes"]
