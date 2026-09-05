"""mohobot 数据加载(只读) — contexts 会话扫描 + history join + 图片 VLM 缓存。

对 mohobot 的 data/ 目录只读:
  contexts/{bot_id}/{private|group}/{chat_id}/{session_id}.json  会话上下文
  history/{bot_id}/{private|group}/{chat_id}.jsonl               原始事件归档
  cache/image_cache_map.json                                     VLM 图片概括缓存

缓存策略(mtime 失效):
  - 每个会话文件按 mtime 缓存解析结果(contexts 可能被 mohobot 改写, 不能长缓存)
  - history 索引与图片缓存同样按 mtime 失效

消息身份(方案 B, 不修改 mohobot):
  指纹 = sha256(f"{session_key}|{role}|{timestamp}|{content}")
  用户消息(role 形如 "qq-昵称")通过 (timestamp, user_id) 与 history join 出
  message_id 与图片 URL; assistant 消息无 message_id(以指纹锚定)。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

# 豁免审核的角色(框架生成, 非对话)
EXEMPT_ROLES = {"summary", "system"}

SCAN_TTL = 5.0  # 会话列表扫描的短 TTL(秒), 防止频繁刷新全量读盘


def session_key(bot_id: str, chat_type: str, chat_id: str, session_id: str) -> str:
    return f"{bot_id}/{chat_type}/{chat_id}/{session_id}"


def parse_session_key(key: str) -> tuple[str, str, str, str]:
    parts = key.split("/")
    if len(parts) != 4:
        raise ValueError(f"bad session key: {key!r}")
    return parts[0], parts[1], parts[2], parts[3]


def fingerprint(sk: str, role: str, timestamp: Any, content: str) -> str:
    raw = f"{sk}|{role}|{timestamp}|{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def user_id_of_role(role: str) -> str:
    """从 "1070813311-次瓦音匀" 提取 QQ 号; 非该形态返回空。"""
    if not role or role in EXEMPT_ROLES or role in ("user", "assistant"):
        return ""
    head = role.split("-", 1)[0].strip()
    return head if head.isdigit() else ""


def nickname_of_role(role: str) -> str:
    """从 speaker role 提取昵称部分(仅当 QQ 前缀为数字才剥离)。"""
    head, sep, rest = role.partition("-")
    if sep and head.isdigit():
        return rest
    return role


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


class MohobotData:
    """只读访问 mohobot 数据目录(带 mtime 失效缓存)。"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._ctx_cache: dict[str, tuple[float, list[dict]]] = {}   # file -> (mtime, entries)
        self._hist_cache: dict[str, tuple[float, dict]] = {}        # file -> (mtime, index)
        self._imgmap_cache: tuple[float, dict] | None = None
        self._scan_cache: tuple[float, list[dict]] | None = None
        self._bots_cache: tuple[float, dict[str, str]] | None = None

    # ── 目录工具 ─────────────────────────────────────────────

    def _ctx_base(self) -> Path:
        return self.data_dir / "contexts"

    def _hist_base(self) -> Path:
        return self.data_dir / "history"

    def session_path(self, sk: str) -> Path:
        bot_id, chat_type, chat_id, session_id = parse_session_key(sk)
        return self._ctx_base() / bot_id / chat_type / chat_id / f"{session_id}.json"

    def _history_path(self, bot_id: str, chat_type: str, chat_id: str) -> Path:
        return self._hist_base() / bot_id / chat_type / f"{chat_id}.jsonl"

    # ── 会话扫描 ─────────────────────────────────────────────

    def list_sessions(self, force: bool = False) -> list[dict[str, Any]]:
        """全部会话概要(短 TTL 缓存)。

        返回: session_key/bot_id/chat_type/chat_id/session_id/mtime/
              total(非豁免条数)/last_ts/display_name
        """
        now = time.time()
        if not force and self._scan_cache and now - self._scan_cache[0] < SCAN_TTL:
            return self._scan_cache[1]

        result: list[dict[str, Any]] = []
        base = self._ctx_base()
        if base.exists():
            for bot_dir in sorted(base.iterdir()):
                if not bot_dir.is_dir():
                    continue
                for chat_type in ("private", "group"):
                    type_dir = bot_dir / chat_type
                    if not type_dir.is_dir():
                        continue
                    for chat_dir in sorted(type_dir.iterdir()):
                        if not chat_dir.is_dir():
                            continue
                        for f in sorted(chat_dir.glob("*.json")):
                            if f.name == "session_index.json":
                                continue
                            entries = self._load_json_cached(f)
                            if entries is None:
                                continue
                            reviewable = [e for e in entries if e.get("role") not in EXEMPT_ROLES]
                            last_ts = max(
                                (int(e.get("timestamp") or 0) for e in reviewable), default=0
                            )
                            result.append({
                                "session_key": session_key(
                                    bot_dir.name, chat_type, chat_dir.name, f.stem
                                ),
                                "bot_id": bot_dir.name,
                                "chat_type": chat_type,
                                "chat_id": chat_dir.name,
                                "session_id": f.stem,
                                "mtime": f.stat().st_mtime,
                                "total": len(reviewable),
                                "last_ts": last_ts,
                                "display_name": self._display_name(
                                    chat_type, chat_dir.name, entries
                                ),
                            })
        self._scan_cache = (now, result)
        return result

    @staticmethod
    def _display_name(chat_type: str, chat_id: str, entries: list[dict]) -> str:
        """会话显示名: 群聊=群号, 私聊=最近一条用户消息的昵称(回退 QQ 号)。"""
        if chat_type == "group":
            return f"群 {chat_id}"
        for e in reversed(entries):
            role = e.get("role", "")
            if role not in EXEMPT_ROLES and role not in ("user", "assistant"):
                nick = nickname_of_role(role)
                if nick:
                    return nick
        return chat_id

    def _load_json_cached(self, path: Path) -> list[dict] | None:
        """读会话 JSON(按 mtime 缓存); 不存在/损坏返回 None。"""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        key = str(path)
        cached = self._ctx_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        entries = data if isinstance(data, list) else []
        self._ctx_cache[key] = (mtime, entries)
        return entries

    def load_entries(self, sk: str) -> list[dict] | None:
        """读单个会话条目(不存在返回 None)。"""
        path = self.session_path(sk)
        if not path.exists():
            return None
        return self._load_json_cached(path) or []

    def bot_nicknames(self) -> dict[str, str]:
        """{bot_id: nickname} — 从 data/bots/{bot_id}/config.json 读(只读)。"""
        now = time.time()
        if self._bots_cache and now - self._bots_cache[0] < SCAN_TTL:
            return self._bots_cache[1]
        result: dict[str, str] = {}
        bots_dir = self.data_dir / "bots"
        if bots_dir.exists():
            for d in sorted(bots_dir.iterdir()):
                cfg_file = d / "config.json"
                if not cfg_file.is_file():
                    continue
                try:
                    cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                    result[d.name] = str(cfg.get("nickname") or "") or d.name
                except (OSError, json.JSONDecodeError):
                    continue
        self._bots_cache = (now, result)
        return result

    # ── history join ─────────────────────────────────────────

    def history_index(self, bot_id: str, chat_type: str, chat_id: str) -> dict:
        """{(timestamp, user_id): {message_id, image_url, nickname}} — mtime 缓存。"""
        path = self._history_path(bot_id, chat_type, chat_id)
        if not path.exists():
            return {}
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        key = str(path)
        cached = self._hist_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]

        index: dict[tuple[int, str], dict[str, str]] = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t, uid = d.get("time"), d.get("user_id")
                    if t is None or uid is None:
                        continue
                    try:
                        t = int(t)
                    except (TypeError, ValueError):
                        continue
                    image_url = ""
                    msg = d.get("message")
                    if isinstance(msg, list):
                        for seg in msg:
                            if isinstance(seg, dict) and seg.get("type") == "image":
                                u = str((seg.get("data") or {}).get("url") or "")
                                if u:
                                    image_url = u
                                    break
                    sender = d.get("sender") or {}
                    index[(t, str(uid))] = {
                        "message_id": str(d.get("message_id") or ""),
                        "image_url": image_url,
                        "nickname": str(sender.get("card") or sender.get("nickname") or ""),
                    }
        except OSError:
            return {}
        self._hist_cache[key] = (mtime, index)
        return index

    def lookup_history(
        self, bot_id: str, chat_type: str, chat_id: str, timestamp: Any, user_id: str,
    ) -> dict[str, str] | None:
        try:
            t = int(timestamp)
        except (TypeError, ValueError):
            return None
        if not user_id:
            return None
        return self.history_index(bot_id, chat_type, chat_id).get((t, user_id))

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
        """把会话条目组装为前端展示结构(身份 join + 审核状态 + 图片/VLM)。"""
        bot_id, chat_type, chat_id, _sid = parse_session_key(sk)
        raw_entries = self.load_entries(sk)
        if raw_entries is None:
            return []
        out: list[dict[str, Any]] = []
        for e in raw_entries:
            role = str(e.get("role", ""))
            content = str(e.get("content", ""))
            ts = e.get("timestamp")
            kind = (
                "assistant" if role == "assistant"
                else "summary" if role in EXEMPT_ROLES
                else "user"
            )
            fp = fingerprint(sk, role, ts, content)
            uid = user_id_of_role(role)

            message_id = ""
            image_url = ""
            hist = self.lookup_history(bot_id, chat_type, chat_id, ts, uid) if kind == "user" else None
            if hist:
                message_id = hist.get("message_id", "")
                image_url = hist.get("image_url", "")
            vlm = self.vlm_caption(image_url) if image_url else None

            st = statuses.get(fp)
            if kind in ("summary",):
                status = "exempt"
                reviewer = ""
            elif st:
                status = st["status"]
                reviewer = st["reviewer"]
            else:
                status = "unreviewed"
                reviewer = ""
            abnormal = abnormal_map.get(fp)

            out.append({
                "fingerprint": fp,
                "role": role,
                "kind": kind,
                "speaker": nickname_of_role(role) if kind == "user" else "",
                "user_id": uid,
                "content": content,
                "timestamp": ts,
                "time_str": format_ts(ts),
                "message_id": message_id,
                "image_url": image_url,
                "vlm": vlm,
                "status": status,
                "reviewer": reviewer,
                "reviewed_at": (st or {}).get("reviewed_at"),
                "abnormal": (
                    {"id": abnormal["id"], "tags": abnormal.get("tags", []),
                     "note": abnormal.get("note", "")}
                    if abnormal else None
                ),
            })
        return out
