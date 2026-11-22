"""mohobot 数据加载(只读) — data/history 为唯一数据源。

对 mohobot 的 data/ 目录只读:
  history/group/{群号}.jsonl                          群聊合并归档(跨 bot 去重,
                                                      行内 bot_id 标注归属)
  history/{bot_id}/private/{chat_id}.jsonl            私聊事件流(按 bot 分目录)
  cache/image_cache_map.json                         VLM 图片概括缓存
  bots/{bot_id}/config.json                          bot 昵称与 QQ

history JSONL 每行一个事件, 只增不删:
  post_type="message"       收到的消息(群里所有成员的消息都在)
  post_type="message_sent"  bot 自己发送的消息(WSServer 出站层归档)

审核范围(面板侧过滤, 归档保持完整):
  - 私聊: 全部消息(按 bot 独立会话)
  - 群聊(单文件会话, key 的 bot 段固定为 "_merged"):
    只留 bot 的发言(message_sent, bot 取行内 bot_id)与用户 @ 某只 bot
    或引用某只 bot 发言的消息(命中归属到该 bot)。
    引用过滤需要每只 bot 的发言 mid 集合(bot_mids_by_bot)与 self_id,
    解析时随行构建。

身份: 优先 message_id("mid:<id>", history 只增不删 → 审核结论永不失联);
无 message_id 时退回内容指纹(sha256(session_key|kind|time|content))。

性能:
  - 每个文件增量解析(记录上次读取 offset, 只解析新增字节), mtime/size
    失效; 群聊过滤在解析时按行判定(bot message_id 集合随解析同步构建,
    引用消息必然晚于被引用的 bot 发言入档)。
  - 解析结果持久化到 sidecar 缓存(review/data/loader_cache.json, 传
    cache_path 才启用): 重启直接加载, 不再冷解析全量 history(生产
    1.5GB/219 万行, 冷解析会把整个面板卡住几分钟)。
  - 加载器方法为同步纯函数, 由调用方(run_in_executor)决定执行线程;
    内部用线程锁保护缓存, 允许并发调用。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

SCAN_TTL = 5.0  # 会话列表扫描的短 TTL(秒)

# 群聊合并会话的 bot 段固定值(session_key = "_merged/group/{群号}")
MERGED_BOT_ID = "_merged"

_ROW_FIELDS = ("kind", "mid", "uid", "nick", "text", "image_url", "time", "bot",
               "source")


def session_key(bot_id: str, chat_type: str, chat_id: str) -> str:
    return f"{bot_id}/{chat_type}/{chat_id}"


def parse_session_key(key: str) -> tuple[str, str, str]:
    parts = key.split("/")
    if len(parts) != 3:
        raise ValueError(f"bad session key: {key!r}")
    return parts[0], parts[1], parts[2]


def content_fingerprint(sk: str, kind: str, timestamp: Any, content: str) -> str:
    """无 message_id 时的兜底身份(内容指纹)。"""
    raw = f"{sk}|{kind}|{timestamp}|{content}"
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def entry_identity(mid: str, fallback: str) -> str:
    """审核条目身份: message_id 优先。"""
    return f"mid:{mid}" if mid else fallback


def user_id_of_role(role: str) -> str:
    """从 "1070813311-次瓦音匀" 提取 QQ 号; 非该形态返回空。"""
    if not role or role in ("user", "assistant", "bot", "summary", "system"):
        return ""
    head = role.split("-", 1)[0].strip()
    return head if head.isdigit() else ""


def format_ts(ts: Any) -> str:
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d %H:%M:%S")


# 非文本段的展示占位
_SEG_PLACEHOLDERS = {
    "record": "[语音]",
    "video": "[视频]",
    "face": "[表情]",
    "forward": "[合并转发]",
    "json": "[卡片消息]",
    "file": "[文件]",
}

# Windows NTFS ChangeTime 查询(issue #133): st_ctime 在 Windows 上是创建时间,
# 保持 mtime 的等长就地重写没有任何 stat 信号; ChangeTime 在数据写入与元数据
# 修改时更新且不会因 os.utime 恢复 mtime 而回退, 可作为文件身份的一项。
# 非 Windows 平台返回 -1, 同样的职责由 POSIX st_ctime_ns 承担。
if os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes

        class _FileBasicInfo(ctypes.Structure):
            _fields_ = [
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
                ("_pad", wintypes.DWORD),
            ]

        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.CreateFileW.restype = wintypes.HANDLE
        _k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        _GetFileInformationByHandleEx = _k32.GetFileInformationByHandleEx
        _GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        _GetFileInformationByHandleEx.restype = wintypes.BOOL
        _k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _k32.CloseHandle.restype = wintypes.BOOL
        _WIN_CHANGE_TIME_OK = True
    except Exception:  # pragma: no cover - 受限环境无法加载 kernel32
        _WIN_CHANGE_TIME_OK = False
else:
    _WIN_CHANGE_TIME_OK = False


def _file_change_time_ns(path: str | Path) -> int:
    """文件的变更时间(ns 级); 非 Windows 或查询失败返回 -1。

    Windows 上返回 NTFS ChangeTime(100ns ticks since 1601); 该值在写入
    内容或修改元数据时变化, 用于发现"等长就地重写 + 恢复 mtime"这种没有
    其它 stat 信号的非追加更新(issue #133)。
    """
    if not _WIN_CHANGE_TIME_OK:
        return -1
    handle = _k32.CreateFileW(str(path), 0, 0x7, None, 3, 0x80, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return -1
    try:
        info = _FileBasicInfo()
        ok = _GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info),
        )
        return int(info.ChangeTime) if ok else -1
    except Exception:
        return -1
    finally:
        _k32.CloseHandle(handle)


def _render_message(message: Any) -> tuple[str, str]:
    """把消息段渲染为 (展示文本, 第一张照片 url)。

    - 文本段拼接; 图片段: NapCat summary(如 "[动画表情]")直接用,
      照片用 "[图片]" 占位并返回第一张照片的 url(供展示 + VLM 概括);
    - at/reply 段不进内容(它们只表达"对谁说/引用谁");
    - 其它段走占位表; 不把段列表 repr 或 base64 塞进内容。
    """
    if isinstance(message, str):
        text = message.strip()
        return (text, "")
    if not isinstance(message, list):
        return ("", "")
    text = ""
    markers: list[str] = []
    image_url = ""
    for seg in message:
        if not isinstance(seg, dict):
            continue
        stype = seg.get("type")
        data = seg.get("data") or {}
        if stype == "text":
            text += str(data.get("text", "") or "")
        elif stype in ("at", "reply"):
            continue
        elif stype == "image":
            summary = str(data.get("summary", "") or "").strip()
            if summary:
                markers.append(summary if summary.startswith("[") else f"[{summary}]")
            else:
                markers.append("[图片]")
                if not image_url:
                    image_url = str(data.get("url", "") or "")
        else:
            markers.append(_SEG_PLACEHOLDERS.get(stype, "[消息]"))
    body = " ".join(m for m in markers if m)
    combined = (text + (" " + body if body else "")).strip()
    return (combined, image_url)


class _FileIndex:
    """单个 history JSONL 的增量解析缓存。"""

    __slots__ = ("mtime", "mtime_ns", "ctime_ns", "change_ns", "generation",
                 "tail_anchor", "head_anchor", "verified_identity", "size",
                 "offset", "inode", "rows", "bot_mids", "seen_mids",
                 "bot_mids_by_bot", "bot_self_ids", "bots")

    def __init__(self) -> None:
        self.mtime: float = -1.0
        # 高精度文件身份(issue #133): float mtime 在部分文件系统上精度不足,
        # 等长替换/就地重写可能保持 (mtime, size) 不变。identity() 同时纳入
        # mtime_ns/size/inode/ctime_ns, 派生缓存一律按 identity 失效。
        self.mtime_ns: int = -1
        self.ctime_ns: int = -1
        # Windows NTFS ChangeTime(其它平台 -1): 发现保持 mtime 的就地改写。
        self.change_ns: int = -1
        # 内容代次: 每次实际解析(追加/重读)自增, 供派生缓存做二级失效。
        self.generation: int = 0
        # 已消费字节的尾部锚点(最多 _TAIL_ANCHOR_BYTES 字节): 文件变大但
        # 前缀被改写时(非追加更新)用少量回读发现, 避免漏掉整段替换。
        self.tail_anchor: bytes = b""
        # 头/尾锚点最近一次复核通过时的文件身份(issue #136 review N4):
        # 身份完全一致时复用复核结论, 避免每次读索引都重新打开文件 seek;
        # 大文件另行跳过锚点(见 MohobotData._ANCHOR_VERIFY_MAX_BYTES)。
        self.verified_identity: tuple[int, int, int, int, int] | None = None
        # 头/尾锚点各只覆盖 _TAIL_ANCHOR_BYTES(64) 字节: 文件身份(含
        # ChangeTime/ctime)不变时用它复核首尾内容; 落在两者之间的"中段
        # 等长改写"发现不了, 需依赖 change_ns/ctime 的推进(见 identity())。
        self.head_anchor: bytes = b""
        self.size: int = -1
        # 文件身份(inode): 等长替换 + 保守 mtime 时仅靠 (mtime,size) 无法发现,
        # 会继续复用旧解析结果导致漏审(issue #8 第 2 点)。
        self.inode: int = 0
        self.offset: int = 0
        # 过滤后的行(仅入审内容): {kind, mid, uid, nick, text, image_url, time, bot}
        # 同一 message_id 重复推送(断线重推/好友请求重发)在解析时去重, 只留首条
        self.rows: list[dict[str, Any]] = []
        # 该会话内 bot 发言的 message_id 集合(引用过滤用; 含未过过滤的转发)
        self.bot_mids: set[str] = set()
        # 已见 message_id(去重用; 与 rows 同生命周期)
        self.seen_mids: set[str] = set()
        # 群合并文件专用: {bot_id: 该 bot 发言的 mid 集合}(引用过滤按 bot 归属判定)
        self.bot_mids_by_bot: dict[str, set[str]] = {}
        # 群合并文件专用: {bot_id: self_id(QQ)}(来自 message_sent 行, @ 过滤兜底)
        self.bot_self_ids: dict[str, str] = {}
        # 群合并文件专用: 出现过的 bot_id(会话列表的 bots 字段)
        self.bots: list[str] = []

    # ── sidecar 缓存反序列化(rows 为紧凑列表, 字段见 _ROW_FIELDS) ─────

    @classmethod
    def from_cache(cls, d: dict[str, Any]) -> "_FileIndex":
        idx = cls()
        idx.mtime = float(d.get("mtime") or 0)
        idx.size = int(d.get("size") or 0)
        idx.offset = int(d.get("offset") or 0)
        idx.inode = int(d.get("inode") or 0)
        idx.bot_mids = {str(x) for x in (d.get("bot_mids") or [])}
        idx.bot_mids_by_bot = {
            str(k): {str(x) for x in (v or [])}
            for k, v in (d.get("bot_mids_by_bot") or {}).items()
        }
        idx.bot_self_ids = {
            str(k): str(v) for k, v in (d.get("bot_self_ids") or {}).items()
        }
        idx.bots = [str(x) for x in (d.get("bots") or [])]
        idx.mtime_ns = int(d.get("mtime_ns") or -1)
        idx.ctime_ns = int(d.get("ctime_ns") or -1)
        idx.change_ns = int(d.get("change_ns") or -1)
        anchor = d.get("tail_anchor") or ""
        try:
            idx.tail_anchor = anchor.encode("latin-1") if isinstance(anchor, str) else b""
        except UnicodeEncodeError:
            idx.tail_anchor = b""
        head = d.get("head_anchor") or ""
        try:
            idx.head_anchor = head.encode("latin-1") if isinstance(head, str) else b""
        except UnicodeEncodeError:
            idx.head_anchor = b""
        idx.rows = [dict(zip(_ROW_FIELDS, r)) for r in (d.get("rows") or [])]
        idx.seen_mids = {r["mid"] for r in idx.rows if r["mid"]}
        return idx

    def identity(self) -> tuple[int, int, int, int, int]:
        """文件身份: (mtime_ns, size, inode, ctime_ns, change_ns)。

        任一项变化 → 派生缓存必须失效(issue #133)。ctime 在 POSIX 上随
        inode 元数据变化; Windows 的 st_ctime 是创建时间, 改由 NTFS
        ChangeTime(change_ns) 发现"保持 mtime 的等长就地重写"。头/尾锚点
        只覆盖首尾各 64B, 仅作为首尾内容微改的兜底, 不覆盖中段等长改写
        (需靠 change_ns/ctime 推进发现)。
        """
        return (self.mtime_ns, self.size, self.inode, self.ctime_ns,
                self.change_ns)

    def reset(self) -> None:
        """丢弃已解析行, 回到全量重读状态(文件被截断/替换/改写)。"""
        self.rows = []
        self.bot_mids = set()
        self.seen_mids = set()
        self.bot_mids_by_bot = {}
        self.bot_self_ids = {}
        self.bots = []
        self.offset = 0
        self.tail_anchor = b""
        self.head_anchor = b""
        self.verified_identity = None
        self.generation += 1


class MohobotData:
    """只读访问 mohobot 数据目录(history 增量解析 + mtime 失效缓存)。

    方法均为同步纯函数; 内部线程锁保护, 允许调用方用线程池并发执行。
    cache_path 传入时启用 sidecar 持久化(重启免冷解析)。
    """

    # v6: bot 发言增加 source 标记(llm/auto), 机械内容不参与审核; 旧缓存作废
    # v8: offset 改为字节偏移; v7 缓存里存的是字符偏移(含中文时错位), 必须作废
    # v9: 文件身份加入高精度 mtime_ns/ctime_ns + 尾部锚点(issue #133 缓存失效)
    _CACHE_VERSION = 9
    _SAVE_MIN_INTERVAL = 30.0  # sidecar 保存节流(秒)
    _TAIL_ANCHOR_BYTES = 64    # 已消费字节的尾部锚点长度(非追加更新探测)
    # 超过该大小的文件不再做锚点内容复核(issue #136 review N4): 大归档每次
    # 请求都多一次 open+seek, 而元数据(ChangeTime/ctime)已覆盖真实写入路径;
    # 该兜底只针对"同 tick 内等长改写", 对 1MB+ 文件收益不成比例。
    _ANCHOR_VERIFY_MAX_BYTES = 1 << 20

    def __init__(self, data_dir: str | Path, cache_path: str | Path | None = None,
                 exempt_uids=None):
        self.data_dir = Path(data_dir)
        self._lock = threading.RLock()
        # 管理员免检: 这些 QQ 发出的用户消息不计入待审(enrich 时标 exempt),
        # 已被判定(normal/abnormal)的结论不受影响; LLM 回复照常审。
        self._exempt_uids = {str(u) for u in (exempt_uids or [])}
        self._file_cache: dict[str, _FileIndex] = {}     # path -> _FileIndex
        self._meta_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._imgmap_cache: tuple[float, dict] | None = None
        self._scan_cache: tuple[float, list[dict]] | None = None
        # sidecar 持久化缓存(可选)
        self._cache_path = Path(cache_path) if cache_path else None
        self._cache_loaded = False
        self._last_save = 0.0
        self._dirty = False
        self._saving = False
        self._save_thread: threading.Thread | None = None
        # 群聊合并行缓存: group_id -> ((文件, 身份, 代次), 排序去重后的行)
        # 身份包含 inode/ctime(issue #133), 等长替换也能让缓存失效。
        self._merged_cache: dict[str, tuple[tuple, list[dict[str, Any]]]] = {}
        # 待审计数缓存: 键 = (文件身份快照, 审核状态快照, store 修订号) ->
        # {session_key: 待审条数}。避免每次列表请求都逐条扫描全部历史行。
        self._pending_cache: tuple[tuple, dict[str, int]] | None = None

    @property
    def exempt_uids(self) -> set[str]:
        """免检 QQ 集合(管理员名单, 只读副本)。"""
        return set(self._exempt_uids)

    # ── sidecar 缓存 ─────────────────────────────────────────

    def _ensure_cache_loaded(self) -> None:
        """首次使用时加载 sidecar(损坏/版本不符则当无缓存, 冷解析兜底)。"""
        if self._cache_loaded:
            return
        self._cache_loaded = True
        if self._cache_path is None:
            return
        try:
            if not self._cache_path.exists():
                return
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if data.get("version") != self._CACHE_VERSION:
                return
            for rel, d in (data.get("files") or {}).items():
                try:
                    self._file_cache[str(self.data_dir / rel)] = _FileIndex.from_cache(d)
                except Exception:
                    continue
        except Exception:
            pass  # 缓存文件损坏 → 忽略, 走冷解析

    def flush_cache(self) -> None:
        """立即落盘(进程退出/测试收尾用): 等后台保存线程结束, 再补一次同步保存。"""
        thread = self._save_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=30.0)
        self._save_cache(force=True)

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _maybe_save_cache(self) -> None:
        """解析有增量时按节流间隔保存 sidecar(后台线程, 不占用请求线程)。"""
        if self._cache_path is None or not self._dirty:
            return
        now = time.time()
        if now - self._last_save < self._SAVE_MIN_INTERVAL:
            return
        self._last_save = now  # 先占位, 防并发重复触发
        self._spawn_save()

    def _spawn_save(self) -> None:
        """后台落盘(10MB 量级的序列化+写盘, 不让请求线程等)。"""
        with self._lock:
            if self._saving:
                return
            self._saving = True

        def _run() -> None:
            try:
                self._save_cache(force=True)
            finally:
                with self._lock:
                    self._saving = False

        thread = threading.Thread(
            target=_run, name="review-loader-save", daemon=True,
        )
        self._save_thread = thread
        thread.start()

    def _save_cache(self, force: bool = False) -> None:
        """落盘 sidecar: 锁内只做轻量快照(引用), 序列化与写盘在锁外。"""
        if self._cache_path is None:
            return
        with self._lock:
            if not self._dirty:
                return
            now = time.time()
            if not force and now - self._last_save < self._SAVE_MIN_INTERVAL:
                return
            snapshot = [
                (path, idx.mtime, idx.mtime_ns, idx.ctime_ns, idx.change_ns,
                 idx.size, idx.offset, idx.inode, idx.tail_anchor, idx.head_anchor,
                 sorted(idx.bot_mids), list(idx.rows),
                 {k: sorted(v) for k, v in idx.bot_mids_by_bot.items()},
                 dict(idx.bot_self_ids), list(idx.bots))
                for path, idx in self._file_cache.items()
            ]
            self._dirty = False
            self._last_save = now

        files: dict[str, Any] = {}
        for (path, mtime, mtime_ns, ctime_ns, change_ns, size, offset, inode,
             tail_anchor, head_anchor, bot_mids, rows, bot_mids_by_bot,
             bot_self_ids, bots) in snapshot:
            try:
                if not Path(path).exists():
                    continue  # 文件已删除 → 不再缓存
                rel = Path(path).relative_to(self.data_dir).as_posix()
            except (OSError, ValueError):
                continue
            files[rel] = {
                "mtime": mtime,
                "mtime_ns": mtime_ns,
                "ctime_ns": ctime_ns,
                "change_ns": change_ns,
                "size": size,
                "offset": offset,
                "inode": inode,
                "tail_anchor": tail_anchor.decode("latin-1"),
                "head_anchor": head_anchor.decode("latin-1"),
                "bot_mids": bot_mids,
                "bot_mids_by_bot": bot_mids_by_bot,
                "bot_self_ids": bot_self_ids,
                "bots": bots,
                "rows": [[r[f] for f in _ROW_FIELDS] for r in rows],
            }
        payload = {"version": self._CACHE_VERSION, "files": files}
        tmp = self._cache_path.with_suffix(".json.tmp")
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._cache_path)  # 原子替换
        except Exception:
            with self._lock:
                self._dirty = True  # 写失败 → 置脏, 下次增量时重试
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # ── 目录工具 ─────────────────────────────────────────────

    def _hist_base(self) -> Path:
        return self.data_dir / "history"

    def history_path(self, sk: str) -> Path:
        bot_id, chat_type, chat_id = parse_session_key(sk)
        if bot_id == MERGED_BOT_ID and chat_type == "group":
            # 群聊合并会话: 单文件 data/history/group/{群号}.jsonl
            return self._hist_base() / "group" / f"{chat_id}.jsonl"
        return self._hist_base() / bot_id / chat_type / f"{chat_id}.jsonl"

    def _group_file(self, group_id: str) -> Path:
        """群聊合并归档文件 data/history/group/{群号}.jsonl。"""
        return self._hist_base() / "group" / f"{group_id}.jsonl"

    # ── bot 元信息 ───────────────────────────────────────────

    def _bot_meta(self) -> dict[str, dict[str, Any]]:
        """{bot_id: {"nickname":…, "qq":…}} — 从 data/bots/{id}/config.json 读。"""
        now = time.time()
        with self._lock:
            if self._meta_cache and now - self._meta_cache[0] < SCAN_TTL:
                return self._meta_cache[1]
            result: dict[str, dict[str, Any]] = {}
            bots_dir = self.data_dir / "bots"
            if bots_dir.exists():
                for d in sorted(bots_dir.iterdir()):
                    cfg_file = d / "config.json"
                    if not cfg_file.is_file():
                        continue
                    try:
                        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    result[d.name] = {
                        "nickname": str(cfg.get("nickname") or "") or d.name,
                        "qq": str(cfg.get("qq") or ""),
                    }
            self._meta_cache = (now, result)
            return result

    def bot_nicknames(self) -> dict[str, str]:
        return {b: m["nickname"] for b, m in self._bot_meta().items()}

    def bot_self_id(self, bot_id: str) -> str:
        """bot 的 QQ 号(群聊 @/引用过滤的 self_id 兜底)。"""
        return self._bot_meta().get(bot_id, {}).get("qq", "")

    # ── history 增量解析 ─────────────────────────────────────

    @staticmethod
    def _anchors_match(path: Path, idx: _FileIndex) -> bool:
        """身份元数据完全相同时, 用头/尾小锚点复核内容是否真的没变。"""
        head, tail = idx.head_anchor, idx.tail_anchor
        if not head and not tail:
            return True
        try:
            with open(path, "rb") as fh:
                if head and fh.read(len(head)) != head:
                    return False
                if tail:
                    fh.seek(max(0, idx.offset - len(tail)))
                    if fh.read(len(tail)) != tail:
                        return False
        except OSError:
            return False
        return True

    def _content_unchanged(self, path: Path, idx: _FileIndex) -> bool:
        """身份未变时确认内容未变(锚点复核 + 按身份缓存, 大文件跳过)。

        同一身份(含 ChangeTime)下重复读索引时直接复用上次复核结论, 不再
        为每个请求打开文件 seek 首尾; 超过 _ANCHOR_VERIFY_MAX_BYTES 的文件
        不做锚点复核。缓存随 reset()/identity 变化失效(issue #136 review N4)。
        """
        ident = idx.identity()
        if idx.verified_identity == ident:
            return True
        if idx.size > self._ANCHOR_VERIFY_MAX_BYTES:
            idx.verified_identity = ident
            return True
        if self._anchors_match(path, idx):
            idx.verified_identity = ident
            return True
        return False

    def _is_append_only(
        self, idx: _FileIndex, path: Path, size: int, inode: int,
        mtime_ns: int, ctime_ns: int,
    ) -> bool:
        """新文件状态是否只是在已解析前缀后"纯追加"。

        返回 False → 必须全量重读, 否则复用旧 rows 会漏审(issue #133):
          - 截断/变短: 前缀可能已变;
          - inode 变化: os.replace/rename 等长替换;
          - 长度不变但 mtime/ctime 变化: 就地等长重写;
          - 长度变长但尾部锚点不匹配: 前缀被改写(伪追加)。
        """
        if idx.mtime_ns < 0 or idx.size < 0:
            return False  # 从未解析过
        if size < idx.size or size < idx.offset:
            return False  # 截断/变短
        if idx.inode and inode and idx.inode != inode:
            return False  # 替换为另一个文件, 即使长度相同
        if size == idx.size:
            # 身份前面已确认不同; 长度不变 → 内容/元数据被改写。
            return False
        if not idx.tail_anchor:
            # 没有锚点(空文件开始)时, 长度增加只可能是追加。
            return idx.offset == 0
        start = idx.offset - len(idx.tail_anchor)
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                if fh.read(len(idx.tail_anchor)) != idx.tail_anchor:
                    return False  # 已消费前缀被改写 → 非追加更新
        except OSError:
            return False
        return True

    @staticmethod
    def _same_identity(
        idx: _FileIndex, mtime_ns: int, size: int, inode: int,
        ctime_ns: int, change_ns: int,
    ) -> bool:
        """身份是否未变; ChangeTime 不可得(非 Windows/权限受限)时忽略该项。"""
        if (idx.mtime_ns, idx.size, idx.inode, idx.ctime_ns) != (
            mtime_ns, size, inode, ctime_ns,
        ):
            return False
        if idx.change_ns >= 0 and change_ns >= 0:
            return idx.change_ns == change_ns
        return True

    def _read_file_index(self, path: Path) -> _FileIndex | None:
        """读一个 history 文件的过滤行(增量: 只解析新增字节)。损坏返回 None。

        首次调用会先加载 sidecar 缓存; 有增量时标脏并按节流落盘。
        文件身份 = (mtime_ns, size, inode, ctime_ns), 只有确认"纯追加"时才
        增量解析; 替换/等长重写/截断/前缀改写一律全量重读, 防止 rows 与
        派生缓存继续复用旧内容(issue #133)。
        """
        self._ensure_cache_loaded()
        try:
            st = path.stat()
        except OSError:
            return None
        key = str(path)
        with self._lock:
            idx = self._file_cache.get(key)
            if idx is None:
                idx = _FileIndex()
                self._file_cache[key] = idx
            mtime = st.st_mtime
            mtime_ns = int(st.st_mtime_ns)
            ctime_ns = int(st.st_ctime_ns)
            change_ns = _file_change_time_ns(path)
            size = int(st.st_size)
            inode = int(st.st_ino)
            if idx.mtime_ns >= 0 and self._same_identity(
                idx, mtime_ns, size, inode, ctime_ns, change_ns,
            ):
                # 元数据完全一致时仍用头/尾锚点复核内容: Windows 上保持 mtime
                # 的等长就地重写没有元数据信号, 只有内容锚点能发现(issue #133)。
                # 复核结论按身份缓存, 大文件跳过(issue #136 review N4)。
                if self._content_unchanged(path, idx):
                    return idx

            rewrite = not self._is_append_only(
                idx, path, size, inode, mtime_ns, ctime_ns,
            )
            if rewrite:
                # 文件被截断/替换/就地改写 → 旧解析结果作废, 全量重读
                idx.reset()
            new_rows: list[dict[str, Any]] = []
            try:
                # offset 是字节偏移: 必须二进制读、按字节切分再解码。文本模式下
                # 用字符下标累加 offset 会在含中文(多字节 UTF-8)的文件上 seek
                # 到字符中间, 下次读取 UnicodeDecodeError, 整个会话列表 500。
                with open(path, "rb") as fh:
                    start_offset = idx.offset
                    if start_offset:
                        fh.seek(start_offset)
                    new_bytes = fh.read()
                # 只消费到最后一个换行: 跨分块写入的半行留到下次(文件增长后)
                # 再解析, 避免"丢弃半行 + 已推进 offset"导致该行永久漏审
                # (issue #8 第 5 点/半行问题)。
                cut = new_bytes.rfind(b"\n")
                if cut >= 0:
                    new_bytes = new_bytes[: cut + 1]
                    idx.offset = start_offset + cut + 1
                    if not idx.head_anchor and start_offset == 0:
                        idx.head_anchor = new_bytes[: self._TAIL_ANCHOR_BYTES]
                    idx.tail_anchor = (
                        idx.tail_anchor + new_bytes
                    )[-self._TAIL_ANCHOR_BYTES:]
                else:
                    new_bytes = b""
                    idx.offset = start_offset
                idx.mtime = mtime
                idx.mtime_ns = mtime_ns
                idx.ctime_ns = ctime_ns
                idx.change_ns = change_ns
                idx.size = size
                idx.inode = inode
            except OSError:
                return None
            # 换行符在 UTF-8 中不会出现在多字节序列内, 切分点总在字符边界;
            # 个别坏字节只影响所在行(替换后该行 JSON 解析失败被跳过)。
            new_text = new_bytes.decode("utf-8", errors="replace")
            # 布局判定: history/group/{群号}.jsonl 为群合并文件(倒数第三段是
            # "history"); 其余为 per-bot 文件 {bot_id}/{private|group}/{id}.jsonl
            merged_group = (
                len(path.parts) >= 3
                and path.parts[-3] == "history" and path.parts[-2] == "group"
            )
            if merged_group:
                chat_type = "group"
            else:
                chat_type = path.parts[-2]
                self_id = self.bot_self_id(path.parts[-3])
            for line in new_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if merged_group:
                    row = self._parse_merged_line(line, idx)
                else:
                    row = self._parse_line(
                        line, chat_type, self_id, idx.bot_mids, path.parts[-3],
                    )
                if row is None:
                    continue
                if row["mid"] and row["mid"] in idx.seen_mids:
                    continue  # 同一消息重复推送(重连重推/好友请求重发) → 只留首条
                if row["mid"]:
                    idx.seen_mids.add(row["mid"])
                new_rows.append(row)
            if new_rows:
                idx.rows.extend(new_rows)
            if new_rows or rewrite:
                idx.generation += 1
                self._mark_dirty()
            # 刚读过内容, 当前身份下的锚点复核必然成立 → 记录, 下次请求免复核。
            idx.verified_identity = idx.identity()
            return idx

    def _parse_line(
        self, line: str, chat_type: str, self_id: str, bot_mids: set[str],
        bot_id: str = "",
    ) -> dict[str, Any] | None:
        """解析一行历史事件 → 过滤后的行(群聊只留 bot 相关); 不入审返回 None。"""
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(d, dict):
            return None
        post_type = d.get("post_type")
        if post_type == "message_sent":
            kind = "assistant"
        elif post_type == "message":
            kind = "user"
        else:
            return None

        mid = str(d.get("message_id") or "").strip()
        ts = int(d.get("time") or 0)
        text, image_url = _render_message(d.get("message"))
        if not text and not image_url:
            return None

        if kind == "assistant":
            # 来源过滤: "auto"=命令/插件等机械内容(欢迎/禁言通知/点赞结果等),
            # 不参与审核; "llm"=LLM 生成; 旧数据无标记 → 保留待审(历史兼容)。
            # ID 登记必须先于 source 过滤(issue #8 第 3 点): 用户引用一条 auto
            # 消息提问时, 该 ID 需被识别为 bot 发言, 否则引用消息被丢弃漏审。
            if mid:
                bot_mids.add(mid)
            if str(d.get("source") or "") == "auto":
                return None
            sender = d.get("sender") or {}
            nick = str(sender.get("nickname") or "").strip()
            return {
                "kind": "assistant", "mid": mid, "uid": "",
                "nick": nick, "text": text, "image_url": image_url, "time": ts,
                "bot": bot_id, "source": str(d.get("source") or ""),
            }

        # 用户消息 — 指令(/开头, ping)是机械输入, 不参与审核
        t = text.strip()
        if t.startswith("/") or t.lower() == "ping":
            return None
        # 用户消息 — 群聊需命中 @ 本 bot 或引用本 bot 发言
        if chat_type == "group":
            if not self._user_hits_bot(d.get("message"), self_id, bot_mids):
                return None
        sender = d.get("sender") or {}
        uid = str(d.get("user_id") or sender.get("user_id") or "")
        nick = str(sender.get("card") or sender.get("nickname") or "").strip() or uid
        return {
            "kind": "user", "mid": mid, "uid": uid,
            "nick": nick, "text": text, "image_url": image_url, "time": ts,
            "bot": bot_id, "source": "",
        }

    @staticmethod
    def _user_hits_bot(message: Any, self_id: str, bot_mids: set[str]) -> bool:
        """群消息是否 @ 本 bot 或引用本 bot 发言。"""
        if not isinstance(message, list):
            return False
        for seg in message:
            if not isinstance(seg, dict):
                continue
            data = seg.get("data") or {}
            if seg.get("type") == "at":
                if self_id and str(data.get("qq") or "") == self_id:
                    return True
            elif seg.get("type") == "reply":
                if str(data.get("id") or "") in bot_mids:
                    return True
        return False

    # ── 群合并文件解析(history/group/{群号}.jsonl) ────────────

    def _parse_merged_line(self, line: str, idx: "_FileIndex") -> dict[str, Any] | None:
        """解析群合并文件一行 → 过滤后的行(只留 bot 相关); 不入审返回 None。

        bot 归属: message_sent 行取行内 bot_id(旧数据无该字段时按 self_id
        反查); 用户行按 @/引用 命中判定, 同时命中多只 bot 时取 bot_id 排序
        最靠前者。
        """
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(d, dict):
            return None
        post_type = d.get("post_type")
        if post_type == "message_sent":
            kind = "assistant"
        elif post_type == "message":
            kind = "user"
        else:
            return None

        mid = str(d.get("message_id") or "").strip()
        ts = int(d.get("time") or 0)
        text, image_url = _render_message(d.get("message"))
        if not text and not image_url:
            return None

        if kind == "assistant":
            # 来源过滤: "auto"=命令/插件等机械内容, 不参与审核;
            # "llm"=LLM 生成; 旧数据无标记 → 保留待审(历史兼容)。
            source = str(d.get("source") or "")
            bot = str(d.get("bot_id") or "").strip()
            if not bot:
                bot = self._bot_by_self_id(str(d.get("self_id") or "")) or "bot"
            # Bot 消息 ID 登记与"是否入审正文"解耦(issue #8 第 3 点):
            # 用户【引用】一条 auto 消息提问时, 该消息需要被识别为 bot 发言,
            # 否则会被当成用户消息丢弃。因此先登记 ID, 再按 source 决定是否入审。
            if mid:
                idx.bot_mids.add(mid)
                idx.bot_mids_by_bot.setdefault(bot, set()).add(mid)
            self_id = str(d.get("self_id") or "")
            if self_id:
                prev = idx.bot_self_ids.get(bot)
                idx.bot_self_ids[bot] = self_id if not prev else prev
            if bot not in idx.bots:
                idx.bots.append(bot)
            if source == "auto":
                # 机械内容不入审, 但 ID 已登记(见上)
                return None
            sender = d.get("sender") or {}
            nick = str(sender.get("nickname") or "").strip()
            return {
                "kind": "assistant", "mid": mid, "uid": "",
                "nick": nick, "text": text, "image_url": image_url, "time": ts,
                "bot": bot, "source": source,
            }

        # 用户消息 — 指令(/开头, ping)是机械输入, 不参与审核
        t = text.strip()
        if t.startswith("/") or t.lower() == "ping":
            return None
        # 用户消息 — 群聊需 @ 某只 bot 或引用某只 bot 的发言
        hit_bot = self._user_hits_any_bot(d.get("message"), idx)
        if hit_bot is None:
            return None
        sender = d.get("sender") or {}
        uid = str(d.get("user_id") or sender.get("user_id") or "")
        nick = str(sender.get("card") or sender.get("nickname") or "").strip() or uid
        return {
            "kind": "user", "mid": mid, "uid": uid,
            "nick": nick, "text": text, "image_url": image_url, "time": ts,
            "bot": hit_bot, "source": "",
        }

    def _user_hits_any_bot(self, message: Any, idx: "_FileIndex") -> str | None:
        """群消息命中的 bot(未命中返回 None)。

        bot 上下文 = 文件内出现过的 bot(message_sent 行自带 self_id)∪
        bots 配置目录; 按 bot_id 排序保证归属判定确定性。
        """
        ctx: dict[str, str] = {}
        for bot, qq in idx.bot_self_ids.items():
            if bot:
                ctx[bot] = str(qq)
        for bot, meta in self._bot_meta().items():
            if bot not in ctx:
                ctx[bot] = str(meta.get("qq") or "")
        for bot in sorted(ctx):
            if self._user_hits_bot(message, ctx[bot], idx.bot_mids_by_bot.get(bot, set())):
                return bot
        return None

    def _bot_by_self_id(self, self_id: str) -> str:
        """QQ 号 → bot_id(bots 配置反查; 查不到返回空)。"""
        if not self_id:
            return ""
        for bot, meta in self._bot_meta().items():
            if str(meta.get("qq") or "") == self_id:
                return bot
        return ""

    # ── 会话扫描 ─────────────────────────────────────────────

    def list_sessions(self, force: bool = False) -> list[dict[str, Any]]:
        """全部会话概要(短 TTL 缓存)。

        返回: session_key/bot_id/chat_type/chat_id/mtime/
              total(入审消息数)/last_ts/display_name
        """
        now = time.time()
        if not force and self._scan_cache and now - self._scan_cache[0] < SCAN_TTL:
            return self._scan_cache[1]

        self._ensure_cache_loaded()
        result: list[dict[str, Any]] = []
        base = self._hist_base()
        if base.exists():
            for bot_dir in sorted(base.iterdir()):
                if not bot_dir.is_dir():
                    continue
                private_dir = bot_dir / "private"
                if private_dir.is_dir():
                    for f in sorted(private_dir.glob("*.jsonl")):
                        idx = self._read_file_index(f)
                        if not idx or not idx.rows:
                            continue
                        last_ts = max((r["time"] for r in idx.rows), default=0)
                        result.append({
                            "session_key": session_key(
                                bot_dir.name, "private", f.stem,
                            ),
                            "bot_id": bot_dir.name,
                            "chat_type": "private",
                            "chat_id": f.stem,
                            "mtime": f.stat().st_mtime,
                            "total": len(idx.rows),
                            "last_ts": last_ts,
                            "display_name": self._display_name(
                                "private", f.stem, idx.rows,
                            ),
                        })
            # 群聊: 合并文件 history/group/{群号}.jsonl(单文件, 写侧已去重)
            merged_dir = base / "group"
            if merged_dir.is_dir():
                for f in sorted(merged_dir.glob("*.jsonl")):
                    idx = self._read_file_index(f)
                    if not idx or not idx.rows:
                        continue
                    result.append({
                        "session_key": session_key(MERGED_BOT_ID, "group", f.stem),
                        "bot_id": MERGED_BOT_ID,
                        "chat_type": "group",
                        "chat_id": f.stem,
                        "mtime": f.stat().st_mtime,
                        # total 用兜底去重后的数量(无 mid 的重复用户消息只算一次)
                        "total": len(self._merged_rows(f.stem)),
                        "last_ts": max((r["time"] for r in idx.rows), default=0),
                        "display_name": f"群 {f.stem}",
                        "bots": list(idx.bots),
                    })
        self._scan_cache = (now, result)
        self._maybe_save_cache()
        return result

    @staticmethod
    def _display_name(
        chat_type: str, chat_id: str, rows: list[dict[str, Any]],
    ) -> str:
        """会话显示名: 群聊=群号, 私聊=最近一条用户消息的昵称(回退 QQ 号)。"""
        if chat_type == "group":
            return f"群 {chat_id}"
        for row in reversed(rows):
            if row["kind"] == "user" and row.get("nick"):
                return row["nick"]
        return chat_id

    def filtered_rows(self, sk: str) -> list[dict[str, Any]]:
        """会话的入审行(不存在/为空返回 [])。群聊合并会话跨 bot 聚合去重。"""
        bot_id, chat_type, chat_id = parse_session_key(sk)
        if bot_id == MERGED_BOT_ID and chat_type == "group":
            return self._merged_rows(chat_id)
        path = self.history_path(sk)
        if not path.exists():
            return []
        idx = self._read_file_index(path)
        if not idx:
            return []
        with self._lock:
            self._maybe_save_cache()
            return list(idx.rows)

    def _merged_rows(self, group_id: str) -> list[dict[str, Any]]:
        """群聊合并会话的行: 单文件解析结果按时间排序 + 兜底去重。

        解析时已按 message_id 去重(seen_mids); 用户消息无 mid 时按
        (time, uid, text) 兜底去重 —— 覆盖重启窗口内可能的重复写入。
        bot 发言不做内容级去重。
        """
        path = self._group_file(group_id)
        if not path.exists():
            return []
        idx = self._read_file_index(path)
        if not idx:
            return []
        # 派生缓存签名包含文件身份与内容代次: 等长替换/inode 变化/就地重写
        # 都会让 merged 行缓存失效(issue #133, 旧签名只有 path/mtime/size)。
        sig = ((str(path), idx.identity(), idx.generation),)
        with self._lock:
            cached = self._merged_cache.get(group_id)
            if cached and cached[0] == sig:
                return cached[1]
        out: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for row in sorted(idx.rows, key=lambda r: r["time"]):
            if row["kind"] == "assistant":
                dk: Any = f"mid:{row['mid']}" if row["mid"] else id(row)
            else:
                dk = (row["mid"] if row["mid"]
                      else (row["time"], row["uid"], row["text"]))
            if dk in seen:
                continue
            seen.add(dk)
            out.append(row)
        with self._lock:
            self._merged_cache[group_id] = (sig, out)
            self._maybe_save_cache()
        return out

    def load_entries(self, sk: str) -> list[dict[str, Any]] | None:
        """兼容入口: 会话入审行(不存在返回 None)。"""
        try:
            bot_id, chat_type, chat_id = parse_session_key(sk)
        except ValueError:
            return None
        if bot_id == MERGED_BOT_ID and chat_type == "group":
            return self._merged_rows(chat_id) or []
        path = self.history_path(sk)
        if not path.exists():
            return None
        idx = self._read_file_index(path)
        if not idx:
            return []
        with self._lock:
            return list(idx.rows)

    def unreviewed_counts(
        self, statuses: dict[str, dict[str, Any]], revision: int | None = None,
    ) -> dict[str, int]:
        """{session_key: 待审条数} —— 对**当前文件里的有效消息集合**求交。

        旧实现用 ``total - 已判定数``, 恢复较短备份后 total 变小而 DB 里仍有
        旧判定记录, 相减会得到负数/0, 与明细里的待审条目矛盾(issue #8 第 4 点)。
        这里改为逐条枚举当前索引里的消息, 只要其 fingerprint 不在已判定集合中
        就计为待审, 因此与明细口径一致。

        issue #136: 50k 行规模下逐条求交会拖慢会话列表。传入 ``revision``
        (ReviewStore.revision) 时按「文件身份快照 + 审核状态快照 + revision」
        缓存结果; 文件变化/判定/撤销都会让快照变化。revision=None 时禁用缓存,
        保持旧调用方的实时语义。
        """
        self.list_sessions()  # 确保文件索引已加载(幂等)
        with self._lock:
            files = list(self._file_cache.items())
            files_sig = tuple(
                (path, idx.identity(), idx.generation)
                for path, idx in sorted(files)
            )
        sig: tuple | None = None
        if revision is not None:
            st_sig = tuple(
                (sk, len(st)) for sk, st in sorted(statuses.items()) if st
            )
            sig = (files_sig, st_sig, int(revision))
            with self._lock:
                cached = self._pending_cache
                if cached is not None and cached[0] == sig:
                    return dict(cached[1])
        out: dict[str, int] = {}
        for path_str, idx in files:
            path = Path(path_str)
            merged_group = (
                len(path.parts) >= 3
                and path.parts[-3] == "history" and path.parts[-2] == "group"
            )
            if merged_group:
                sk = session_key(MERGED_BOT_ID, "group", path.stem)
                rows = self._merged_rows(path.stem)
            else:
                if len(path.parts) < 3:
                    continue
                sk = session_key(path.parts[-3], path.parts[-2], path.stem)
                rows = list(idx.rows)
            st = statuses.get(sk, {})
            n = 0
            for row in rows:
                if row["kind"] == "user" and row["uid"] in self._exempt_uids:
                    continue  # 管理员免检消息不计入待审
                ident = entry_identity(
                    row["mid"],
                    content_fingerprint(sk, row["kind"], row["time"], row["text"]),
                )
                if ident not in st:
                    n += 1
            if n:
                out[sk] = n
        if sig is not None:
            with self._lock:
                self._pending_cache = (sig, dict(out))
        return out

    def exempt_pending_counts(self, statuses: dict[str, dict[str, Any]]) -> dict[str, int]:
        """{session_key: 管理员免检且未判定的条数} — 供待审计数扣除。

        只统计「管理员发出 + 尚无审核结论」的用户消息; 已判定(normal/
        abnormal)的按原结论计, 不受免检影响。
        """
        if not self._exempt_uids:
            return {}
        self.list_sessions()  # 确保文件索引已加载(幂等)
        out: dict[str, int] = {}
        with self._lock:
            for path_str, idx in self._file_cache.items():
                path = Path(path_str)
                merged_group = (
                    len(path.parts) >= 3
                    and path.parts[-3] == "history" and path.parts[-2] == "group"
                )
                if merged_group:
                    sk = session_key(MERGED_BOT_ID, "group", path.stem)
                    rows = self._merged_rows(path.stem)
                else:
                    sk = session_key(path.parts[-3], path.parts[-2], path.stem)
                    rows = idx.rows
                st = statuses.get(sk, {})
                n = 0
                for row in rows:
                    if (row["kind"] == "user"
                            and row["uid"] in self._exempt_uids
                            and entry_identity(
                                row["mid"],
                                content_fingerprint(sk, row["kind"], row["time"], row["text"]),
                            ) not in st):
                        n += 1
                if n:
                    out[sk] = n
        return out

    def search_content(
        self, q: str, bot: str = "", chat_type: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按内容子串搜索全部入审消息(与审核界面同一数据集)。

        数据都在内存(过滤后约几万行), 单次扫描毫秒级, 无需索引。
        搜索范围 = 会话明细能展示的范围(群聊仅 bot 相关消息)。
        返回按时间倒序的命中列表, 带条目在会话内的序号(index,
        与 filtered_rows/enrich_entries 的顺序一致), 前端据此跳页定位;
        群聊合并会话的 bot 过滤按行内 bot 归属判断。
        """
        q = (q or "").strip()
        if len(q) < 2:
            return []
        ql = q.lower()
        self.list_sessions()  # 确保文件索引已加载(幂等)
        with self._lock:
            info_map = {
                s["session_key"]: s
                for s in (self._scan_cache[1] if self._scan_cache else [])
            }
            out: list[dict[str, Any]] = []
            for path_str, idx in self._file_cache.items():
                path = Path(path_str)
                merged_group = (
                    len(path.parts) >= 3
                    and path.parts[-3] == "history" and path.parts[-2] == "group"
                )
                if merged_group:
                    ct, chat_id, bot_id = "group", path.stem, MERGED_BOT_ID
                    sk = session_key(MERGED_BOT_ID, "group", chat_id)
                    info = info_map.get(sk) or {}
                    if bot and bot not in (info.get("bots") or []):
                        continue
                    if chat_type and ct != chat_type:
                        continue
                    rows = self._merged_rows(chat_id)
                else:
                    bot_id = path.parts[-3]
                    ct, chat_id = path.parts[-2], path.stem
                    if bot and bot_id != bot:
                        continue
                    if chat_type and ct != chat_type:
                        continue
                    sk = session_key(bot_id, ct, chat_id)
                    rows = idx.rows
                for i, row in enumerate(rows):
                    pos = row["text"].lower().find(ql)
                    if pos < 0:
                        continue
                    info = info_map.get(sk) or {}
                    out.append({
                        "session_key": sk,
                        "display_name": info.get("display_name", chat_id),
                        "bot_id": bot_id,
                        "chat_type": ct,
                        "chat_id": chat_id,
                        "index": i,
                        "kind": row["kind"],
                        "speaker": row["nick"] if row["kind"] == "user" else "",
                        "content": row["text"],
                        "timestamp": row["time"],
                        "time_str": format_ts(row["time"]),
                        "message_id": row["mid"],
                        "fingerprint": entry_identity(
                            row["mid"],
                            content_fingerprint(sk, row["kind"], row["time"], row["text"]),
                        ),
                    })
        out.sort(key=lambda x: x["timestamp"], reverse=True)
        return out[: max(1, int(limit))]

    # ── VLM 图片概括 ─────────────────────────────────────────

    def vlm_caption(self, image_url: str) -> str | None:
        """图片 URL → VLM 概括(mohobot ImageCache 的缓存文件, mtime 失效)。"""
        if not image_url:
            return None
        path = self.data_dir / "cache" / "image_cache_map.json"
        if not path.exists():
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if self._imgmap_cache is None or self._imgmap_cache[0] != mtime:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            self._imgmap_cache = (mtime, data if isinstance(data, dict) else {})
        item = self._imgmap_cache[1].get(image_url)
        if isinstance(item, dict):
            desc = str(item.get("description") or "").strip()
            return desc or None
        return None

    # ── 会话明细组装 / 分页 ──────────────────────────────────

    def _row_identity(self, sk: str, row: dict[str, Any]) -> str:
        """单行审核身份(message_id 优先, 无 mid 回退内容指纹)。"""
        return entry_identity(
            row["mid"],
            content_fingerprint(sk, row["kind"], row["time"], row["text"]),
        )

    def _distinct_rows(self, sk: str) -> list[tuple[dict[str, Any], str]]:
        """会话去重后的 (行, 身份) 列表 — 明细页与 enrich 共用的稳定顺序。"""
        out: list[tuple[dict[str, Any], str]] = []
        seen: set[str] = set()
        for row in self.filtered_rows(sk):
            identity = self._row_identity(sk, row)
            if identity in seen:
                continue  # 同一消息重复推送时只保留第一条
            seen.add(identity)
            out.append((row, identity))
        return out

    def _row_status(
        self, row: dict[str, Any], identity: str,
        statuses: dict[str, dict[str, Any]],
    ) -> str:
        st = statuses.get(identity)
        if st:
            return st["status"]
        if row["kind"] == "user" and row["uid"] in self._exempt_uids:
            return "exempt"  # 管理员免检: 未判定时淡显保留上下文
        return "unreviewed"

    def _enrich_row(
        self, row: dict[str, Any], identity: str,
        statuses: dict[str, dict[str, Any]],
        abnormal_map: dict[str, dict[str, Any]],
        nicknames: dict[str, str],
    ) -> dict[str, Any]:
        """单行 → 前端展示结构(与旧 enrich_entries 字段完全一致)。"""
        kind = row["kind"]
        mid = row["mid"]
        ts = row["time"]
        st = statuses.get(identity)
        abnormal = abnormal_map.get(identity)
        bot_id = str(row.get("bot") or "")
        status = self._row_status(row, identity, statuses)
        return {
            "fingerprint": identity,
            "role": "assistant" if kind == "assistant" else (row["uid"] or "user"),
            "kind": kind,
            "speaker": row["nick"] if kind == "user" else "",
            "user_id": row["uid"],
            "content": row["text"],
            "timestamp": ts,
            "time_str": format_ts(ts),
            "message_id": mid,
            "image_url": row["image_url"],
            "vlm": self.vlm_caption(row["image_url"]) if row["image_url"] else None,
            "status": status,
            "reviewer": (st or {}).get("reviewer", ""),
            "reviewed_at": (st or {}).get("reviewed_at"),
            "bot_id": bot_id,
            "bot_nickname": (nicknames.get(bot_id, bot_id)
                             if kind == "assistant" and bot_id else ""),
            "abnormal": (
                {"id": abnormal["id"], "tags": abnormal.get("tags", []),
                 "note": abnormal.get("note", "")}
                if abnormal else None
            ),
        }

    def enrich_entries(
        self, sk: str, statuses: dict[str, dict[str, Any]],
        abnormal_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把会话的入审行组装为前端展示结构(身份 + 审核状态 + 图片/VLM)。

        群聊合并会话里, 每条 bot 发言附带 bot_id/bot_nickname(多 bot 混流)。
        全量组装仅用于小会话/兼容调用方; 明细分页请用 page_entries(issue #136)。
        """
        nicknames = self.bot_nicknames()
        return [
            self._enrich_row(row, identity, statuses, abnormal_map, nicknames)
            for row, identity in self._distinct_rows(sk)
        ]

    def page_entries(
        self, sk: str, statuses: dict[str, dict[str, Any]],
        abnormal_map: dict[str, dict[str, Any]],
        page: int = 0, page_size: int = 100,
    ) -> dict[str, Any] | None:
        """会话明细分页: 只 enrich 当前窗口, 不组装整会话(issue #136)。

        统计口径与旧实现一致(total/unreviewed/abnormal_count/abnormal_pages
        都基于整会话), page<=0 自动锚定第一条待审所在页, 无待审则最后一页。
        会话不存在或没有入审行时返回 None(与旧 404 语义一致)。
        """
        distinct = self._distinct_rows(sk)
        if not distinct:
            return None
        total = len(distinct)
        page_size = max(1, min(int(page_size or 100), 500))
        pages = max(1, (total + page_size - 1) // page_size)
        unreviewed = 0
        abnormal_count = 0
        first_pending: int | None = None
        abnormal_idx: list[int] = []
        for i, (row, identity) in enumerate(distinct):
            status = self._row_status(row, identity, statuses)
            if status == "unreviewed":
                unreviewed += 1
                if first_pending is None:
                    first_pending = i
            elif status == "abnormal":
                abnormal_count += 1
                abnormal_idx.append(i)
        if page <= 0:
            page = (first_pending // page_size + 1) if first_pending is not None else pages
        page = min(max(page, 1), pages)
        start = (page - 1) * page_size
        nicknames = self.bot_nicknames()
        window = [
            self._enrich_row(row, identity, statuses, abnormal_map, nicknames)
            for row, identity in distinct[start:start + page_size]
        ]
        return {
            "total": total,
            "unreviewed": unreviewed,
            "abnormal_count": abnormal_count,
            "abnormal_pages": sorted({i // page_size + 1 for i in abnormal_idx}),
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "entries": window,
        }

    def unreviewed_fingerprints(
        self, sk: str, statuses: dict[str, dict[str, Any]],
    ) -> list[str]:
        """会话内全部待审身份(顺序与明细一致)。"""
        return [
            identity for row, identity in self._distinct_rows(sk)
            if self._row_status(row, identity, statuses) == "unreviewed"
        ]

    def unreviewed_count_session(
        self, sk: str, statuses: dict[str, dict[str, Any]],
    ) -> int:
        """单个会话的待审条数(判定后用, 无需重新 enrich 整会话)。"""
        return sum(
            1 for row, identity in self._distinct_rows(sk)
            if self._row_status(row, identity, statuses) == "unreviewed"
        )

    def entries_by_fingerprints(
        self, sk: str, statuses: dict[str, dict[str, Any]],
        abnormal_map: dict[str, dict[str, Any]], fingerprints: list[str],
    ) -> list[dict[str, Any]]:
        """只为指定指纹组装展示结构(异常判定写库只需命中的那几条)。"""
        wanted = set(fingerprints)
        if not wanted:
            return []
        nicknames = self.bot_nicknames()
        out: list[dict[str, Any]] = []
        for row, identity in self._distinct_rows(sk):
            if identity not in wanted:
                continue
            out.append(self._enrich_row(row, identity, statuses, abnormal_map, nicknames))
            wanted.discard(identity)
            if not wanted:
                break
        return out
