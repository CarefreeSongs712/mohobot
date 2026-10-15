"""Mohobot 上下文恢复插件 — 通过合并转发聊天记录恢复私聊会话上下文。

用法（仅私聊，全员可用）：
  1. 回复（引用）一条包含历史对话的合并转发消息；
  2. 发送 ``/恢复`` —— 解析并弹出确认（含条数），回复「确认」写入，「取消」放弃；
     ``/恢复 clean`` 则先清空当前会话再写入。

恢复内容按昵称映射角色：bot 自己（按 QQ 号）→ assistant；其他人 → 命名角色
``QQ号-昵称``（与框架群聊"谁说的"一致的私有会话用法）。图片/语音/视频/文件
用占位符（[图片]/[语音]/[视频]/[文件]），表情 → [表情]。
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

RESTORE_TRIGGERS = ("/恢复", "确认", "取消")
PENDING_TTL = 600.0  # /恢复 → 确认 的等待窗口(秒)

_PLACEHOLDERS = {
    "image": "[图片]",
    "voice": "[语音]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "face": "[表情]",
}
_SKIP_SEGMENTS = {"reply", "forward", "json", "xml", "node"}


class Plugin:
    """把用户引用的合并转发聊天记录恢复进私聊会话上下文。"""

    info = {
        "commands": [
            {"name": "/恢复", "desc": "引用合并转发恢复私聊上下文(/恢复 clean=先清空)"},
        ],
    }

    no_prefix_triggers = {"/恢复", "确认", "取消"}

    _ws_server = None
    _bot_manager = None
    _context_manager = None

    # 待确认恢复: f"{bot_id}:{peer}" -> {"entries": [...], "clean": bool, "expires": ts}
    _pending: dict[str, dict] = {}

    def __init__(self):
        self.plugin_config: dict = {}

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_bot_manager(cls, bot_manager) -> None:
        cls._bot_manager = bot_manager

    @classmethod
    def inject_context_manager(cls, context_manager) -> None:
        cls._context_manager = context_manager

    def _cfg(self, key: str, default):
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None and value != "" else default

    # ── 消息入口 ─────────────────────────────────────────────

    async def on_message(self, bot_id: str, event: Any, raw_event: dict):
        """返回 (handled, reply)。仅处理私聊。"""
        if not self._is_private(event):
            return (False, None)
        peer = str(getattr(event, "user_id", "") or "")
        if not peer:
            return (False, None)
        key = f"{bot_id}:{peer}"
        text = self._plain_text(event)

        pending = self._pending.get(key)
        if pending is not None:
            if time.time() > pending["expires"]:
                del self._pending[key]
                pending = None
            elif text == "确认":
                return await self._commit(bot_id, event, pending, key)
            elif text == "取消":
                del self._pending[key]
                return (True, "已取消恢复。")
            # 等待期间不干预其它消息(普通聊天照常)
            return (False, None)

        if not text.startswith("/恢复"):
            return (False, None)
        if self._context_manager is None:
            return (True, "上下文服务未就绪，无法恢复。")

        # /恢复 [clean]
        tokens = text.split()
        clean = len(tokens) > 1 and tokens[1].lower() == "clean"

        reply_id = self._reply_id(event)
        if not reply_id:
            return (True, "请先引用（回复）一条合并转发聊天记录，再发送 /恢复。")

        messages = await self._fetch_forward(bot_id, reply_id)
        if not messages:
            return (True, "解析合并转发失败：引用的不是可拉取的合并转发消息。")

        entries = self._build_entries(self._bot_qq_set(), messages)
        if not entries:
            return (True, "合并转发里没有可恢复的对话内容。")

        limit = max(1, int(self._cfg("max_entries", 200) or 200))
        dropped = max(0, len(entries) - limit)
        if dropped:
            entries = entries[-limit:]

        self._pending[key] = {"entries": entries, "clean": clean,
                              "expires": time.time() + PENDING_TTL}

        user_n = sum(1 for e in entries if e["role"] != "assistant")
        bot_n = len(entries) - user_n
        lines = [f"共解析到 {len(entries)} 条消息（用户 {user_n} 条 / bot {bot_n} 条）。",
                 ("⚠️ 将先清空当前会话再写入——现有上下文会被全部覆盖！"
                  if clean else "将追加写入当前会话。")]
        if dropped:
            lines.append(f"超出上限 {limit} 条，仅保留最近 {limit} 条。")
        lines.append("回复「确认」写入，回复「取消」放弃。（10 分钟内有效）")
        return (True, "\n".join(lines))

    # ── 确认落地 ─────────────────────────────────────────────

    async def _commit(self, bot_id: str, event: Any, pending: dict, key: str):
        del self._pending[key]
        cm = self._context_manager
        n = len(pending["entries"])
        # chat_id 必须是 str: ContextManager 用 Path / chat_id 拼会话文件路径
        peer = str(event.user_id)
        try:
            if pending.get("clean"):
                await cm.clear_context(bot_id, "private", peer)
            ok = await cm.append_context(bot_id, "private", peer,
                                         pending["entries"])
        except Exception as e:
            logger.error(f"ctx_restore commit failed: {e}")
            return (True, f"恢复失败：{e}")
        if ok:
            if pending.get("clean"):
                logger.info(f"ctx_restore: replaced context with {n} entries "
                            f"({bot_id}/{peer})")
                return (True, f"已清空并用 {n} 条消息重建当前会话上下文。")
            logger.info(f"ctx_restore: restored {n} entries ({bot_id}/{peer})")
            return (True, f"已恢复 {n} 条消息到当前会话上下文。")
        return (True, "恢复失败：会话不可写。")

    # ── 抓取合并转发 ─────────────────────────────────────────

    async def _fetch_forward(self, bot_id: str, reply_id: str) -> list | None:
        """get_msg 取被引用原消息; forward 段再 get_forward_msg 拉节点列表。"""
        ws = self._ws_server
        if ws is None:
            return None
        try:
            resp = await ws.send_to_bot(bot_id, "get_msg", {"message_id": int(reply_id)},
                                        wait_response=True, timeout=10.0)
        except Exception as e:
            logger.debug(f"ctx_restore get_msg({reply_id}) failed: {e}")
            return None
        data = self._resp_data(resp)
        if not isinstance(data, dict):
            return None
        message = data.get("message")
        if not isinstance(message, list) or not message:
            logger.info(f"ctx_restore diagnostic: get_msg({reply_id}) no message list, "
                        f"data keys={list(data.keys())}")
            return None
        fwd_id = ""
        fwd_types = []
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") in ("forward", "node"):
                fwd_types.append(seg.get("type"))
                seg_data = seg.get("data") or {}
                fwd_id = str(seg_data.get("id", "") or seg_data.get("resid", "") or "")
                if fwd_id:
                    break
        if not fwd_id:
            logger.info(f"ctx_restore diagnostic: referral {reply_id} segments="
                        f"{[s.get('type') if isinstance(s, dict) else type(s).__name__ for s in message]}, "
                        f"forward-id empty, types={fwd_types}")
        if fwd_id:
            tried = []
            for params in ({"message_id": int(reply_id)}, {"id": fwd_id}):
                try:
                    resp2 = await ws.send_to_bot(bot_id, "get_forward_msg",
                                                 params, wait_response=True, timeout=15.0)
                except Exception as e:
                    logger.debug(f"ctx_restore get_forward_msg({params}) failed: {e}")
                    continue
                fw = self._resp_data(resp2)
                cur = fw.get("messages") if isinstance(fw, dict) else None
                if isinstance(cur, list) and cur:
                    tried.append((params, cur))
                    break
                logger.info(f"ctx_restore diagnostic: get_forward_msg(params={list(params)}) "
                            f"returned {type(cur).__name__}, keys={list(fw.keys()) if isinstance(fw, dict) else 'N/A'}")
            if not tried:
                return None
            msgs = tried[0][1]
            try:
                node_desc = [
                    {"sender": (m.get("sender") or {}).get("user_id"),
                     "nick": (m.get("sender") or {}).get("nickname"),
                     "keys": [k for k in m.keys() if k in ("content", "message")],
                     "seg_count": len(m.get("content") or m.get("message") or [])}
                    for m in msgs if isinstance(m, dict)]
                logger.info(f"ctx_restore diagnostic: forward({fwd_id}) "
                            f"nodes={len(msgs)} {node_desc}")
                import json as _json
                raw_dump = _json.dumps(msgs[:6], ensure_ascii=False, default=str)
                for ch in range(0, min(len(raw_dump), 7200), 1800):
                    logger.info(f"ctx_restore raw dump [{ch}]: "
                                f"{raw_dump[ch:ch+1800]}")
            except Exception:
                pass
        # 展开嵌套 forward: 节点本身是嵌套合并转发时递归拉一层
        expanded: list = []
        for node in msgs:
            if isinstance(node, dict):
                nested = ""
                for seg in (node.get("content") or node.get("message") or []):
                    if isinstance(seg, dict) and seg.get("type") == "forward":
                        nid = str((seg.get("data") or {}).get("id", "") or "")
                        if nid:
                            nested = nid
                            break
                if nested:
                    deeper = await self._fetch_forward(bot_id, nested)
                    if deeper:
                        expanded.extend(deeper)
                    continue
            expanded.append(node)
        if len(expanded) != len(msgs):
            logger.info(f"ctx_restore diagnostic: expanded {len(msgs)} -> {len(expanded)} nodes")
        return expanded
        # 没找到 forward id: 落回单条被引用消息(当前行为)
        sender = data.get("sender") or {}
        logger.info(f"ctx_restore diagnostic: fallback single-message restore "
                    f"for referral {reply_id}, sender uid={sender.get('user_id')}")
        return [{
            "sender": {"user_id": sender.get("user_id"),
                       "nickname": sender.get("card") or sender.get("nickname")},
            "message": message,
        }]

    @staticmethod
    def _resp_data(resp) -> Any:
        if not isinstance(resp, dict):
            return None
        status = str(resp.get("status", "") or "").lower()
        try:
            retcode = int(resp.get("retcode", 0) or 0)
        except (TypeError, ValueError):
            retcode = 0
        if status in ("", "ok") and retcode == 0:
            return resp.get("data")
        return None

    # ── 解析节点 → 上下文条目 ────────────────────────────────

    def _build_entries(self, bot_qqs: set[int], msgs: list) -> list[dict]:
        bot_nicks = self._bot_nickname_set()
        entries: list[dict] = []
        for node in msgs:
            if not isinstance(node, dict):
                continue
            sender = node.get("sender") or {}
            try:
                uid = int(sender.get("user_id") or node.get("user_id") or 0)
            except (TypeError, ValueError):
                uid = 0
            display = str(sender.get("card") or sender.get("nickname")
                          or sender.get("nickname_card") or "").strip()
            segments = node.get("content")
            if segments is None:
                segments = node.get("message")
            text = self._render_segments(segments)
            if not text:
                continue
            # 助手判定: QQ 号精确 + 昵称(含机器人名片变体, 如"墨清弦bot"/"洛天依bot-6")
            is_bot = (uid and uid in bot_qqs) or bool(
                display and any(nick and nick in display for nick in bot_nicks))
            if is_bot:
                role = "assistant"
            else:
                role = f"{uid or display}-{display or uid}"
            try:
                ts = int(node.get("time") or 0) or int(time.time())
            except (TypeError, ValueError):
                ts = int(time.time())
            entries.append({"role": role, "content": text, "timestamp": ts})
        return entries

    def _bot_nickname_set(self) -> set[str]:
        bm = self._bot_manager
        nicks: set[str] = set()
        if bm is None:
            return nicks
        try:
            for inst in getattr(bm, "all_bots", []) or []:
                nick = str(getattr(inst, "nickname", "") or "").strip()
                if nick:
                    nicks.add(nick)
        except Exception as e:
            logger.debug(f"ctx_restore bot nicknames unavailable: {e}")
        return nicks

    def _bot_qq_set(self) -> set[int]:
        bm = self._bot_manager
        qqs: set[int] = set()
        if bm is None:
            return qqs
        try:
            for inst in getattr(bm, "all_bots", []) or []:
                qq = getattr(inst, "qq", None)
                if qq:
                    try:
                        qqs.add(int(qq))
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            logger.debug(f"ctx_restore bot list unavailable: {e}")
        return qqs

    def _render_segments(self, segments) -> str:
        if isinstance(segments, str):
            return segments.strip()
        if not isinstance(segments, list):
            return ""
        parts: list[str] = []
        for seg in segments:
            if isinstance(seg, dict):
                seg_type = seg.get("type")
                if seg_type in _SKIP_SEGMENTS:
                    continue
                if seg_type == "text":
                    t = str((seg.get("data") or {}).get("text", "") or "").strip()
                    if t:
                        parts.append(t)
                    continue
                parts.append(_PLACEHOLDERS.get(seg_type or "", f"[{seg_type or '未知'}]"))
            else:
                t = str(seg or "").strip()
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()

    # ── 辅助 ─────────────────────────────────────────────────

    @staticmethod
    def _is_private(event: Any) -> bool:
        if getattr(event, "group_id", None):
            return False
        return getattr(event, "message_type", "private") == "private" \
            or getattr(event, "message_type", None) is None

    @staticmethod
    def _plain_text(event: Any) -> str:
        message = getattr(event, "message", None)
        if isinstance(message, str):
            return message.strip()
        parts: list[str] = []
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(str((seg.get("data") or {}).get("text", "") or ""))
        return "".join(parts).strip()

    @staticmethod
    def _reply_id(event: Any) -> str:
        message = getattr(event, "message", None)
        if not isinstance(message, list):
            return ""
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                rid = str((seg.get("data") or {}).get("id", "") or "")
                if rid.isdigit():
                    return rid
        return ""
