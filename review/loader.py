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

_ROW_FIELDS = ("kind", "mid", "uid", "nick", "text", "image_url", "time", "bot")


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

    __slots__ = ("mtime", "size", "offset", "rows", "bot_mids", "seen_mids",
                 "bot_mids_by_bot", "bot_self_ids", "bots")

    def __init__(self) -> None:
        self.mtime: float = -1.0
        self.size: int = -1
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
        idx.bot_mids = {str(x) for x in (d.get("bot_mids") or [])}
        idx.bot_mids_by_bot = {
            str(k): {str(x) for x in (v or [])}
            for k, v in (d.get("bot_mids_by_bot") or {}).items()
        }
        idx.bot_self_ids = {
            str(k): str(v) for k, v in (d.get("bot_self_ids") or {}).items()
        }
        idx.bots = [str(x) for x in (d.get("bots") or [])]
        idx.rows = [dict(zip(_ROW_FIELDS, r)) for r in (d.get("rows") or [])]
        idx.seen_mids = {r["mid"] for r in idx.rows if r["mid"]}
        return idx


class MohobotData:
    """只读访问 mohobot 数据目录(history 增量解析 + mtime 失效缓存)。

    方法均为同步纯函数; 内部线程锁保护, 允许调用方用线程池并发执行。
    cache_path 传入时启用 sidecar 持久化(重启免冷解析)。
    """

    # v5: 群聊 history 切换合并布局(history/group/{群号}.jsonl), 旧缓存作废
    _CACHE_VERSION = 5
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
        self._saving = False
        self._save_thread: threading.Thread | None = None
        # 群聊合并行缓存: group_id -> ((文件, mtime, size), 排序去重后的行)
        self._merged_cache: dict[str, tuple[tuple, list[dict[str, Any]]]] = {}

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
                (path, idx.mtime, idx.size, idx.offset,
                 sorted(idx.bot_mids), list(idx.rows),
                 {k: sorted(v) for k, v in idx.bot_mids_by_bot.items()},
                 dict(idx.bot_self_ids), list(idx.bots))
                for path, idx in self._file_cache.items()
            ]
            self._dirty = False
            self._last_save = now

        files: dict[str, Any] = {}
        for (path, mtime, size, offset, bot_mids, rows,
             bot_mids_by_bot, bot_self_ids, bots) in snapshot:
            try:
                if not Path(path).exists():
                    continue  # 文件已删除 → 不再缓存
                rel = Path(path).relative_to(self.data_dir).as_posix()
            except (OSError, ValueError):
                continue
            files[rel] = {
                "mtime": mtime,
                "size": size,
                "offset": offset,
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
                idx.seen_mids = set()
                idx.bot_mids_by_bot = {}
                idx.bot_self_ids = {}
                idx.bots = []
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
                self._mark_dirty()
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
            if mid:
                bot_mids.add(mid)
            sender = d.get("sender") or {}
            nick = str(sender.get("nickname") or "").strip()
            return {
                "kind": "assistant", "mid": mid, "uid": "",
                "nick": nick, "text": text, "image_url": image_url, "time": ts,
                "bot": bot_id,
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
            "bot": bot_id,
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
            bot = str(d.get("bot_id") or "").strip()
            if not bot:
                bot = self._bot_by_self_id(str(d.get("self_id") or "")) or "bot"
            if mid:
                idx.bot_mids.add(mid)
                idx.bot_mids_by_bot.setdefault(bot, set()).add(mid)
            self_id = str(d.get("self_id") or "")
            if self_id:
                prev = idx.bot_self_ids.get(bot)
                idx.bot_self_ids[bot] = self_id if not prev else prev
            if bot not in idx.bots:
                idx.bots.append(bot)
            sender = d.get("sender") or {}
            nick = str(sender.get("nickname") or "").strip()
            return {
                "kind": "assistant", "mid": mid, "uid": "",
                "nick": nick, "text": text, "image_url": image_url, "time": ts,
                "bot": bot,
            }

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
            "bot": hit_bot,
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
        sig = ((str(path), idx.mtime, idx.size),)
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

    # ── 会话明细组装 ─────────────────────────────────────────

    def enrich_entries(
        self, sk: str, statuses: dict[str, dict[str, Any]],
        abnormal_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把会话的入审行组装为前端展示结构(身份 + 审核状态 + 图片/VLM)。

        群聊合并会话里, 每条 bot 发言附带 bot_id/bot_nickname(多 bot 混流)。
        """
        nicknames = self.bot_nicknames()
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
            bot_id = str(row.get("bot") or "")
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
                "bot_id": bot_id,
                "bot_nickname": (nicknames.get(bot_id, bot_id)
                                 if kind == "assistant" and bot_id else ""),
                "abnormal": (
                    {"id": abnormal["id"], "tags": abnormal.get("tags", []),
                     "note": abnormal.get("note", "")}
                    if abnormal else None
                ),
            })
        return out
