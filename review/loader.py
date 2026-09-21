"""mohobot 数据加载(只读) — data/history 为唯一数据源。

对 mohobot 的 data/ 目录只读:
  history/{bot_id}/{private|group}/{chat_id}.jsonl   消息事件流(收到的 + bot 发的)
  cache/image_cache_map.json                         VLM 图片概括缓存
  bots/{bot_id}/config.json                          bot 昵称与 QQ

history JSONL 每行一个事件, 只增不删:
  post_type="message"       收到的消息(群里所有成员的消息都在)
  post_type="message_sent"  bot 自己发送的消息(WSServer 出站层归档)

审核范围(面板侧过滤, 归档保持完整):
  - 私聊: 全部消息
  - 群聊: bot 的发言 + 用户 @ 本 bot 或引用本 bot 发言的消息
    (@: 事件 message 段里 at.qq == self_id;
     引用: reply 段 id ∈ 该会话已归档的 bot message_id 集合)

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

    __slots__ = ("mtime", "size", "offset", "rows", "bot_mids")

    def __init__(self) -> None:
        self.mtime: float = -1.0
        self.size: int = -1
        self.offset: int = 0
        # 过滤后的行(仅入审内容): {kind, mid, uid, nick, text, image_url, time}
        self.rows: list[dict[str, Any]] = []
        # 该会话内 bot 发言的 message_id 集合(引用过滤用; 含未过过滤的转发)
        self.bot_mids: set[str] = set()

    # ── sidecar 缓存序列化(rows 用紧凑列表, 每行 7 字段) ─────

    def to_cache(self) -> dict[str, Any]:
        return {
            "mtime": self.mtime,
            "size": self.size,
            "offset": self.offset,
            "bot_mids": sorted(self.bot_mids),
            "rows": [
                [r["kind"], r["mid"], r["uid"], r["nick"],
                 r["text"], r["image_url"], r["time"]]
                for r in self.rows
            ],
        }

    @classmethod
    def from_cache(cls, d: dict[str, Any]) -> "_FileIndex":
        idx = cls()
        idx.mtime = float(d.get("mtime") or 0)
        idx.size = int(d.get("size") or 0)
        idx.offset = int(d.get("offset") or 0)
        idx.bot_mids = {str(x) for x in (d.get("bot_mids") or [])}
        fields = ("kind", "mid", "uid", "nick", "text", "image_url", "time")
        idx.rows = [dict(zip(fields, r)) for r in (d.get("rows") or [])]
        return idx


class MohobotData:
    """只读访问 mohobot 数据目录(history 增量解析 + mtime 失效缓存)。

    方法均为同步纯函数; 内部线程锁保护, 允许调用方用线程池并发执行。
    cache_path 传入时启用 sidecar 持久化(重启免冷解析)。
    """

    _CACHE_VERSION = 2
    _SAVE_MIN_INTERVAL = 30.0  # sidecar 保存节流(秒)

    def __init__(self, data_dir: str | Path, cache_path: str | Path | None = None):
        self.data_dir = Path(data_dir)
        self._lock = threading.RLock()
        self._file_cache: dict[str, _FileIndex] = {}     # path -> _FileIndex
        self._meta_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._imgmap_cache: tuple[float, dict] | None = None
        self._scan_cache: tuple[float, list[dict]] | None = None
        # sidecar 持久化缓存(可选)
        self._cache_path = Path(cache_path) if cache_path else None
        self._cache_loaded = False
        self._last_save = 0.0
        self._dirty = False

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
        """立即落盘 sidecar(进程退出时调用)。"""
        self._save_cache(force=True)

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _maybe_save_cache(self) -> None:
        """解析有增量时按节流间隔保存 sidecar。"""
        if self._cache_path is None or not self._dirty:
            return
        now = time.time()
        if now - self._last_save < self._SAVE_MIN_INTERVAL:
            return
        self._save_cache()

    def _save_cache(self, force: bool = False) -> None:
        if self._cache_path is None or not self._dirty:
            return
        now = time.time()
        if not force and now - self._last_save < self._SAVE_MIN_INTERVAL:
            return
        files: dict[str, Any] = {}
        with self._lock:
            for path, idx in self._file_cache.items():
                try:
                    if not Path(path).exists():
                        continue  # 文件已删除 → 不再缓存
                except OSError:
                    continue
                rel = Path(path).relative_to(self.data_dir).as_posix()
                files[rel] = idx.to_cache()
        payload = {"version": self._CACHE_VERSION, "files": files}
        tmp = self._cache_path.with_suffix(".json.tmp")
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._cache_path)  # 原子替换
            self._last_save = now
            self._dirty = False
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # ── 目录工具 ─────────────────────────────────────────────

    def _hist_base(self) -> Path:
        return self.data_dir / "history"

    def history_path(self, sk: str) -> Path:
        bot_id, chat_type, chat_id = parse_session_key(sk)
        return self._hist_base() / bot_id / chat_type / f"{chat_id}.jsonl"

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

    def _read_file_index(self, path: Path) -> _FileIndex | None:
        """读一个 history 文件的过滤行(增量: 只解析新增字节)。损坏返回 None。

        首次调用会先加载 sidecar 缓存; 有增量时标脏并按节流落盘。
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
            mtime, size = st.st_mtime, st.st_size
            if idx.mtime == mtime and idx.size == size:
                return idx

            if size < idx.offset or idx.mtime < 0:
                # 文件被截断/替换 → 全量重读
                idx.rows = []
                idx.bot_mids = set()
                idx.offset = 0
            new_rows: list[dict[str, Any]] = []
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    if idx.offset:
                        fh.seek(idx.offset)
                    new_text = fh.read()
                    idx.offset = fh.tell()
                idx.mtime, idx.size = mtime, size
            except OSError:
                return None
            bot_id = path.parts[-3]
            chat_type = path.parts[-2]
            self_id = self.bot_self_id(bot_id)
            for line in new_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                row = self._parse_line(line, chat_type, self_id, idx.bot_mids)
                if row is not None:
                    new_rows.append(row)
            if new_rows:
                idx.rows.extend(new_rows)
                self._mark_dirty()
            return idx

    def _parse_line(
        self, line: str, chat_type: str, self_id: str, bot_mids: set[str],
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
            if mid:
                bot_mids.add(mid)
            sender = d.get("sender") or {}
            nick = str(sender.get("nickname") or "").strip()
            return {
                "kind": "assistant", "mid": mid, "uid": "",
                "nick": nick, "text": text, "image_url": image_url, "time": ts,
            }

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
                for chat_type in ("private", "group"):
                    type_dir = bot_dir / chat_type
                    if not type_dir.is_dir():
                        continue
                    for f in sorted(type_dir.glob("*.jsonl")):
                        idx = self._read_file_index(f)
                        if not idx or not idx.rows:
                            continue
                        last_ts = max((r["time"] for r in idx.rows), default=0)
                        result.append({
                            "session_key": session_key(
                                bot_dir.name, chat_type, f.stem,
                            ),
                            "bot_id": bot_dir.name,
                            "chat_type": chat_type,
                            "chat_id": f.stem,
                            "mtime": f.stat().st_mtime,
                            "total": len(idx.rows),
                            "last_ts": last_ts,
                            "display_name": self._display_name(
                                chat_type, f.stem, idx.rows,
                            ),
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
        """会话的入审行(不存在/为空返回 [])。"""
        path = self.history_path(sk)
        if not path.exists():
            return []
        idx = self._read_file_index(path)
        if not idx:
            return []
        with self._lock:
            self._maybe_save_cache()
            return list(idx.rows)

    def load_entries(self, sk: str) -> list[dict[str, Any]] | None:
        """兼容入口: 会话入审行(不存在返回 None)。"""
        path = self.history_path(sk)
        if not path.exists():
            return None
        idx = self._read_file_index(path)
        if not idx:
            return []
        with self._lock:
            return list(idx.rows)

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

    # ── 会话明细组装 ─────────────────────────────────────────

    def enrich_entries(
        self, sk: str, statuses: dict[str, dict[str, Any]],
        abnormal_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把会话的入审行组装为前端展示结构(身份 + 审核状态 + 图片/VLM)。"""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in self.filtered_rows(sk):
            kind = row["kind"]
            mid = row["mid"]
            ts = row["time"]
            identity = entry_identity(mid, content_fingerprint(sk, kind, ts, row["text"]))
            if identity in seen:
                continue  # 同一消息重复推送时只保留第一条
            seen.add(identity)
            st = statuses.get(identity)
            abnormal = abnormal_map.get(identity)
            out.append({
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
                "status": st["status"] if st else "unreviewed",
                "reviewer": (st or {}).get("reviewer", ""),
                "reviewed_at": (st or {}).get("reviewed_at"),
                "abnormal": (
                    {"id": abnormal["id"], "tags": abnormal.get("tags", []),
                     "note": abnormal.get("note", "")}
                    if abnormal else None
                ),
            })
        return out
