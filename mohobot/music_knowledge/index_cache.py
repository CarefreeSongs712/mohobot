"""歌曲索引持久化缓存 — DB 指纹 + 紧凑二进制格式。

背景: 全量歌词 6-gram 倒排在 16k 首库上重建约 100~150s。直接把
``dict[str, list[int]]`` pickle/marshal 下来读回仍要 70~100s(540 万个
单元素 list 的分配成本), 因此这里用**扁平 CSR 结构**落盘:

    Header: magic(7B) + version(u32) + header_len(u32) + header_json(utf-8)
    Body:   doc_uuid / doc_name / doc_lyrics(归一化) 换行拼接,
            doc_lyrics_raw 拼接 + raw_off(u64 偏移),
            doc_len(i32), grams_blob(按字典序拼接) + gram_off(u64),
            post_ptr(u64, CSR indptr) + post_doc(i32)

- 索引本体 (``grams_blob``/``gram_off``/``post_ptr``/``post_doc``) 读回后
  可零拷贝构造 ``memoryview`` 或直接二分查找, 不分配 540 万个 list。
- ``doc_lyrics_raw`` 是 ``_find_by_lyrics`` 精确子串匹配用的**原始歌词**:
  该路径只去空白、不去标点, 必须与直接构建的输入完全一致。
- body 用 sha256 校验; 截断/损坏/版本不符一律判为 miss, 不抛异常。
- 写入先临时文件再 ``os.replace()``; ``.lock`` 用 flock/msvcrt 串行化
  多进程写入; 成功后按 ``keep`` 清理更旧的缓存。

缓存目录默认 ``data/cache/song_index``(备份已排除 ``data/cache``),
可整目录删除, 等同强制重建。本模块不 import sqlalchemy, 只负责格式与
文件; 构建由 ``matcher`` 完成, 子进程入口见文件末尾 ``__main__``。
"""

from __future__ import annotations

import array
import hashlib
import io
import json
import os
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

# 序列化格式变更时 +1(旧缓存自动失效)。
INDEX_FORMAT_VERSION = 1
# normalize_name 规则变更时 +1。
NORMALIZE_VERSION = 1

MAGIC = b"MBIDX1\0"
# magic(7s) + version(u32) + header_len(u32)
_HEADER = struct.Struct("<7sII")

# body 各段的顺序(header["sections"] 记录每段 [offset, length])。
_SECTION_NAMES = (
    "doc_uuid",
    "doc_name",
    "doc_lyrics",
    "doc_lyrics_raw",
    "raw_off",
    "doc_len",
    "grams_blob",
    "gram_off",
    "post_ptr",
    "post_doc",
)

_I32 = array.array("i")
_U64 = array.array("Q")


# ── 指纹 ──────────────────────────────────────────────────────


def compute_fingerprint(
    db_path: str,
    *,
    gram_size: int,
    fuzzy: bool,
) -> str:
    """计算索引缓存指纹(16 位 sha256 前缀)。

    SQLite WAL 模式下写入主要改 ``-wal``, 主库 mtime 可能到 checkpoint
    才变化, 因此**必须**把 ``-wal`` 的 mtime_ns/size 一起纳入。格式/归一化
    规则/gram 长度/是否启用 fuzzy 也参与指纹, 任一变化都会让旧缓存失效。
    """
    st = os.stat(db_path)
    wal_path = db_path + "-wal"
    wst = os.stat(wal_path) if os.path.exists(wal_path) else None
    payload = {
        "v": INDEX_FORMAT_VERSION,
        "norm": NORMALIZE_VERSION,
        "gram": int(gram_size),
        "fuzzy": bool(fuzzy),
        "db": [st.st_mtime_ns, st.st_size],
        "wal": [wst.st_mtime_ns, wst.st_size] if wst is not None else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def cache_path(cache_dir: str, fingerprint: str) -> str:
    """某个指纹对应的缓存文件路径。"""
    return os.path.join(cache_dir, f"{fingerprint}.bin")


# ── 缓存数据 ──────────────────────────────────────────────────


@dataclass
class CachedSongIndex:
    """从缓存文件读回的扁平索引数据(供 ``LyricGramIndex.from_compact``)。

    ``doc_*`` 列表按数据库查询顺序排列, 与直接构建时 ``_doc_id`` 的
    整数 id 一一对应(0..docs-1)。
    """

    fingerprint: str
    gram_size: int
    doc_uuid: list[str]
    doc_name: list[str]
    doc_lyrics: list[str]          # 归一化歌词(gram 索引用)
    doc_lyrics_raw: list[str]      # 原始歌词(_find_by_lyrics 精确匹配用)
    doc_len: array.array           # 归一化长度
    raw_off: array.array           # doc_lyrics_raw 的 UTF-8 字节偏移(docs+1)
    grams_blob: bytes
    gram_off: array.array          # bytes 偏移, 长度 grams+1
    post_ptr: array.array          # CSR indptr, 长度 grams+1
    post_doc: array.array          # posting 的 doc id
    created: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def docs(self) -> int:
        return len(self.doc_uuid)

    @property
    def grams(self) -> int:
        return max(0, len(self.gram_off) - 1)

    @property
    def postings(self) -> int:
        return len(self.post_doc)


# ── 文件锁 / 清理 ─────────────────────────────────────────────


class _LockTimeout(Exception):
    pass


@contextmanager
def _file_lock(cache_dir: str):
    """跨进程写锁(``.lock``): POSIX 用 flock, Windows 用 msvcrt。

    拿不到锁不致命(缓存只是优化), 记录后照常写入; 但同一进程内仍用
    模块锁串行化。
    """
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, ".lock")
    try:
        fh = open(path, "a+b")
    except OSError:
        yield
        return
    locked = False
    try:
        try:
            import fcntl  # type: ignore

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = True
        except ImportError:
            try:
                import msvcrt  # type: ignore

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            except OSError:
                # 锁不可用不致命(缓存是可选的加速层); 仍执行一次写操作。
                locked = False
        # 锁内代码的异常必须原样向上抛, 不能在这里重试 with 体。
        yield
    finally:
        if locked:
            try:
                import fcntl  # type: ignore

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except ImportError:
                try:
                    import msvcrt  # type: ignore

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        try:
            fh.close()
        except OSError:
            pass


# 同进程内再串行化一次(flock 在部分平台对同进程多线程不互斥)。
_PROCESS_WRITE_LOCK = threading.Lock()


def _cleanup_old(cache_dir: str, keep: int, keep_path: str) -> None:
    """保留最近 ``keep`` 个 ``<fp>.bin``, 删除更旧的与遗留临时文件。"""
    keep = max(1, int(keep))
    try:
        entries = [
            os.path.join(cache_dir, name)
            for name in os.listdir(cache_dir)
            if name.endswith(".bin")
        ]
    except OSError:
        return
    entries.sort(key=lambda p: _safe_mtime(p), reverse=True)
    kept = 0
    for path in entries:
        if os.path.abspath(path) == os.path.abspath(keep_path):
            kept += 1
            continue
        if kept < keep:
            kept += 1
            continue
        try:
            os.remove(path)
        except OSError:
            pass
    # 清理崩溃遗留的临时文件(超过 1 小时)。
    cutoff = time.time() - 3600
    try:
        for name in os.listdir(cache_dir):
            if ".tmp." not in name:
                continue
            path = os.path.join(cache_dir, name)
            if _safe_mtime(path) < cutoff:
                try:
                    os.remove(path)
                except OSError:
                    pass
    except OSError:
        pass


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ── 编码辅助 ──────────────────────────────────────────────────


def _join_lines(items: Sequence[str]) -> bytes:
    return "\n".join(items).encode("utf-8")


def _split_lines(blob: bytes, count: int) -> Optional[list[str]]:
    if count <= 0:
        return [] if not blob else None
    try:
        parts = blob.decode("utf-8").split("\n")
    except UnicodeDecodeError:
        return None
    if len(parts) != count:
        return None
    return parts


def _as_array(typecode: str, data: bytes) -> Optional[array.array]:
    arr = array.array(typecode)
    if len(data) % arr.itemsize:
        return None
    try:
        arr.frombytes(data)
    except (ValueError, TypeError):
        return None
    return arr


# ── 保存 ──────────────────────────────────────────────────────


def save_cache(
    cache_dir: str,
    data: CachedSongIndex,
    *,
    keep: int = 2,
    meta: Optional[dict] = None,
) -> Optional[str]:
    """把扁平索引写入 ``<fingerprint>.bin``, 成功返回路径, 失败返回 None。

    写入顺序: 组装 body → sha256 → 临时文件 → ``os.replace`` → 清理旧文件。
    任何异常都不向上抛(缓存是可选的加速层)。
    """
    try:
        docs = data.docs
        grams = data.grams
        if len(data.doc_name) != docs or len(data.doc_lyrics) != docs:
            raise ValueError("doc 列表长度不一致")
        if len(data.doc_lyrics_raw) != docs or len(data.doc_len) != docs:
            raise ValueError("doc 列表长度不一致")
        if len(data.raw_off) != docs + 1:
            raise ValueError("raw_off 长度不符")
        if len(data.gram_off) != grams + 1 or len(data.post_ptr) != grams + 1:
            raise ValueError("偏移数组长度不符")
        if int(data.gram_off[-1]) != len(data.grams_blob):
            raise ValueError("gram_off 末项与 grams_blob 长度不符")
        if int(data.post_ptr[-1]) != len(data.post_doc):
            raise ValueError("post_ptr 末项与 post_doc 长度不符")
        # uuid/歌名/归一化歌词用换行拼接; 真出现换行会让读回行数错位,
        # 此时宁可不写缓存(下次重建), 也不要每次启动都判 miss。
        if any(
            "\n" in text
            for text in (*data.doc_uuid, *data.doc_name, *data.doc_lyrics)
        ):
            raise ValueError("doc 字段含换行, 无法用换行分隔格式缓存")

        body = io.BytesIO()
        sections: dict[str, list[int]] = {}

        def put(name: str, payload: bytes) -> None:
            offset = body.tell()
            body.write(payload)
            sections[name] = [offset, len(payload)]

        put("doc_uuid", _join_lines(data.doc_uuid))
        put("doc_name", _join_lines(data.doc_name))
        put("doc_lyrics", _join_lines(data.doc_lyrics))
        put("doc_lyrics_raw", "".join(data.doc_lyrics_raw).encode("utf-8"))
        put("raw_off", data.raw_off.tobytes())
        put("doc_len", data.doc_len.tobytes())
        put("grams_blob", data.grams_blob)
        put("gram_off", data.gram_off.tobytes())
        put("post_ptr", data.post_ptr.tobytes())
        put("post_doc", data.post_doc.tobytes())
        body_bytes = body.getvalue()

        header = {
            "fingerprint": data.fingerprint,
            "format": INDEX_FORMAT_VERSION,
            "normalize": NORMALIZE_VERSION,
            "gram_size": int(data.gram_size),
            "docs": docs,
            "grams": grams,
            "postings": len(data.post_doc),
            "sha256": hashlib.sha256(body_bytes).hexdigest(),
            "sections": sections,
            "created": time.time(),
            "meta": dict(meta or data.meta or {}),
        }
        header_bytes = json.dumps(
            header, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        prefix = _HEADER.pack(MAGIC, INDEX_FORMAT_VERSION, len(header_bytes))
        payload_bytes = prefix + header_bytes + body_bytes

        os.makedirs(cache_dir, exist_ok=True)
        path = cache_path(cache_dir, data.fingerprint)
        with _PROCESS_WRITE_LOCK:
            with _file_lock(cache_dir):
                tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
                try:
                    with open(tmp, "wb") as fh:
                        fh.write(payload_bytes)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, path)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                _cleanup_old(cache_dir, keep, path)
        return path
    except Exception:
        return None


# ── 加载 ──────────────────────────────────────────────────────


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def load_cache(
    cache_dir: str,
    fingerprint: str,
    *,
    expected_gram_size: Optional[int] = None,
) -> Optional[CachedSongIndex]:
    """读取并校验缓存; 任何不匹配/损坏都返回 None, 绝不抛异常。

    损坏(截断/校验失败/magic 不符/版本不符)的文件会被删除, 下一次构建
    会重新写入; 指纹不匹配只返回 None(可能是另一份库的旧缓存)。
    """
    path = cache_path(cache_dir, fingerprint)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None

    try:
        if len(raw) < _HEADER.size:
            _discard(path)
            return None
        magic, version, header_len = _HEADER.unpack_from(raw, 0)
        if magic != MAGIC:
            _discard(path)
            return None
        if version != INDEX_FORMAT_VERSION:
            # 旧/新格式都无法复用, 删除避免反复读取。
            _discard(path)
            return None
        body_start = _HEADER.size + header_len
        if header_len <= 0 or body_start > len(raw):
            _discard(path)
            return None
        try:
            header = json.loads(raw[_HEADER.size:body_start].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _discard(path)
            return None
        if not isinstance(header, dict):
            _discard(path)
            return None
        if header.get("format") != INDEX_FORMAT_VERSION:
            _discard(path)
            return None
        if header.get("fingerprint") != fingerprint:
            return None
        if expected_gram_size is not None and int(
            header.get("gram_size", 0)
        ) != int(expected_gram_size):
            return None

        body = raw[body_start:]
        digest = header.get("sha256") or ""
        if hashlib.sha256(body).hexdigest() != digest:
            _discard(path)
            return None

        sections = header.get("sections") or {}
        parts: dict[str, bytes] = {}
        for name in _SECTION_NAMES:
            item = sections.get(name)
            if not isinstance(item, list) or len(item) != 2:
                _discard(path)
                return None
            offset, length = int(item[0]), int(item[1])
            if offset < 0 or length < 0 or offset + length > len(body):
                _discard(path)
                return None
            parts[name] = body[offset:offset + length]

        docs = int(header.get("docs", -1))
        grams = int(header.get("grams", -1))
        postings = int(header.get("postings", -1))
        if docs < 0 or grams < 0 or postings < 0:
            _discard(path)
            return None

        doc_uuid = _split_lines(parts["doc_uuid"], docs)
        doc_name = _split_lines(parts["doc_name"], docs)
        doc_lyrics = _split_lines(parts["doc_lyrics"], docs)
        if doc_uuid is None or doc_name is None or doc_lyrics is None:
            _discard(path)
            return None

        raw_off = _as_array("Q", parts["raw_off"])
        doc_len = _as_array("i", parts["doc_len"])
        gram_off = _as_array("Q", parts["gram_off"])
        post_ptr = _as_array("Q", parts["post_ptr"])
        post_doc = _as_array("i", parts["post_doc"])
        if None in (raw_off, doc_len, gram_off, post_ptr, post_doc):
            _discard(path)
            return None
        assert raw_off is not None and doc_len is not None
        assert gram_off is not None and post_ptr is not None
        assert post_doc is not None

        if (
            len(raw_off) != docs + 1
            or len(doc_len) != docs
            or len(gram_off) != grams + 1
            or len(post_ptr) != grams + 1
            or len(post_doc) != postings
        ):
            _discard(path)
            return None
        raw_blob = parts["doc_lyrics_raw"]
        if len(raw_blob) != int(raw_off[-1]):
            _discard(path)
            return None
        if int(gram_off[-1]) != len(parts["grams_blob"]):
            _discard(path)
            return None
        if int(post_ptr[-1]) != postings:
            _discard(path)
            return None

        doc_lyrics_raw: list[str] = []
        for i in range(docs):
            start, end = int(raw_off[i]), int(raw_off[i + 1])
            if end < start or end > len(raw_blob):
                _discard(path)
                return None
            try:
                doc_lyrics_raw.append(raw_blob[start:end].decode("utf-8"))
            except UnicodeDecodeError:
                _discard(path)
                return None

        return CachedSongIndex(
            fingerprint=fingerprint,
            gram_size=int(header.get("gram_size", 6)),
            doc_uuid=doc_uuid,
            doc_name=doc_name,
            doc_lyrics=doc_lyrics,
            doc_lyrics_raw=doc_lyrics_raw,
            doc_len=doc_len,
            raw_off=raw_off,
            grams_blob=parts["grams_blob"],
            gram_off=gram_off,
            post_ptr=post_ptr,
            post_doc=post_doc,
            created=float(header.get("created", 0.0) or 0.0),
            meta=dict(header.get("meta") or {}),
        )
    except Exception:
        # 任何未预期异常都按 miss 处理, 不能让缓存影响启动。
        _discard(path)
        return None


# ── 兼容旧调用 ────────────────────────────────────────────────

# 供类型检查/外部工具引用, 避免 import array 的细节扩散。
ArrayLike = Iterable[int]

__all__ = [
    "INDEX_FORMAT_VERSION",
    "NORMALIZE_VERSION",
    "CachedSongIndex",
    "cache_path",
    "compute_fingerprint",
    "load_cache",
    "save_cache",
]


# ── 子进程构建入口 ────────────────────────────────────────────


def _build_cli(argv: Optional[list[str]] = None) -> int:
    """``python -m mohobot.music_knowledge.index_cache build ...``

    子进程只负责建索引 + 写缓存文件, 主进程轮询/等待后加载。
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m mohobot.music_knowledge.index_cache",
        description="构建并写入歌曲索引缓存(子进程模式)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build", help="查询歌曲库并写缓存")
    build.add_argument("--db-folder", required=True)
    build.add_argument("--db-file", required=True)
    build.add_argument("--cache-dir", required=True)
    build.add_argument("--gram-size", type=int, default=6)
    build.add_argument("--keep", type=int, default=2)
    build.add_argument("--yield-every", type=int, default=500)
    build.add_argument("--fuzzy", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd != "build":
        return 2

    # 延迟导入: matcher 会 import 本模块, 不能在顶层互相 import。
    from mohobot.music_knowledge.matcher import build_song_index_cache

    path = build_song_index_cache(
        args.db_folder,
        args.db_file,
        args.cache_dir,
        gram_size=args.gram_size,
        fuzzy=args.fuzzy,
        keep=args.keep,
        yield_every=args.yield_every,
    )
    return 0 if path else 1


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(_build_cli())
