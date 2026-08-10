"""Session context management — private multi-session, group single-session.

Directory layout:
  data/contexts/{bot_id}/
    private/{user_id}/
      session_index.json  — { "sessions": [{"id": "...", "name": "..."}], "active": "..." }
      sess_001.json       — [{"role": "user", "content": "..."}, ...]
      sess_002.json       — [...]
    group/{group_id}/
      main.json           — [...] (single session)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.file_store import json_read, json_update, json_write


class ContextManager:
    """Manages session context CRUD with AI-summary compaction.

    压缩机制: 上下文满 trim_at_rounds 轮(默认 40)时, 把最早的
    trim_remove_rounds 轮(默认 15)交给 AI 总结, 总结作为 role="summary"
    的新块插入对话最前; 总结失败则直接裁剪。总结块视为 1 轮参与后续压缩。
    """

    def __init__(
        self,
        data_dir: str = "./data",
        summarizer=None,
        summary_enabled: bool = True,
        trim_at_rounds: int = 40,
        trim_remove_rounds: int = 15,
    ):
        self._data_dir = data_dir
        # 异步总结回调: async (entries: list[dict]) -> str | None
        self._summarizer = summarizer
        self._summary_enabled = summary_enabled
        self._trim_at_rounds = max(2, trim_at_rounds)
        self._trim_remove_rounds = max(1, trim_remove_rounds)

    def set_summarizer(self, summarizer) -> None:
        """注入总结回调(LLMService.summarize_context)。"""
        self._summarizer = summarizer

    def set_trim_config(
        self, *, enabled: bool | None = None,
        at_rounds: int | None = None, remove_rounds: int | None = None,
    ) -> None:
        """热更新压缩配置(web 面板保存全局配置后调用)。"""
        if enabled is not None:
            self._summary_enabled = enabled
        if at_rounds is not None:
            self._trim_at_rounds = max(2, at_rounds)
        if remove_rounds is not None:
            self._trim_remove_rounds = max(1, remove_rounds)

    def _context_base(self, bot_id: str, chat_type: str) -> Path:
        return Path(self._data_dir) / "contexts" / bot_id / chat_type

    def _session_index_path(self, bot_id: str, chat_type: str, chat_id: str) -> Path:
        return self._context_base(bot_id, chat_type) / chat_id / "session_index.json"

    def _session_file_path(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str
    ) -> Path:
        return self._context_base(bot_id, chat_type) / chat_id / f"{session_id}.json"

    # ── Session Index ─────────────────────────────────────────

    async def _load_session_index(
        self, bot_id: str, chat_type: str, chat_id: str
    ) -> dict[str, Any]:
        """Load the session index, creating a default if none exists."""
        path = self._session_index_path(bot_id, chat_type, chat_id)
        data = await json_read(path)
        if data is None:
            # Create default session
            data = {
                "sessions": [{"id": "sess_main", "name": "默认会话", "created": int(time.time())}],
                "active": "sess_main",
            }
            await json_write(path, data)
        return data

    async def _save_session_index(
        self, bot_id: str, chat_type: str, chat_id: str, data: dict
    ) -> None:
        """Save the session index."""
        path = self._session_index_path(bot_id, chat_type, chat_id)
        await json_write(path, data)

    # ── Context CRUD ───────────────────────────────────────────

    async def load_context(
        self, bot_id: str, chat_type: str, chat_id: str
    ) -> list[dict[str, Any]]:
        """Load the current active session context."""
        index = await self._load_session_index(bot_id, chat_type, chat_id)
        active_id = index.get("active", "sess_main")

        if chat_type == "group":
            # Group always uses main.json
            path = self._session_file_path(bot_id, chat_type, chat_id, "main")
        else:
            path = self._session_file_path(bot_id, chat_type, chat_id, active_id)

        data = await json_read(path)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        return []

    async def append_context(
        self,
        bot_id: str,
        chat_type: str,
        chat_id: str,
        entries: list[dict[str, Any]],
        max_rounds: int = 30,
    ) -> None:
        """Append entries to the active session context.

        使用 json_update 原子读改写,避免并发 append 丢失更新。
        追加后检查轮数: 满 trim_at_rounds 轮时触发 AI 总结压缩
        (最早的 trim_remove_rounds 轮 → 总结块插入最前)。
        """
        if chat_type == "group":
            session_id = "main"
        else:
            index = await self._load_session_index(bot_id, chat_type, chat_id)
            session_id = index.get("active", "sess_main")

        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)

        def _append(data):
            context = data if isinstance(data, list) else []
            context.extend(entries)
            return context

        await json_update(path, _append, default=[])

        # 压缩检查(读最新, 锁外做 AI 总结)
        if self._trim_at_rounds > 0:
            context = await json_read(path)
            if isinstance(context, list) and context:
                rounds = self._count_rounds(context)
                if rounds >= self._trim_at_rounds:
                    await self._compact(path, context)

    # ── AI 总结压缩 ──────────────────────────────────────────

    @staticmethod
    def _count_rounds(context: list[dict]) -> int:
        """轮数: 普通消息两条(用户+回复)=1 轮, 总结块=1 轮。"""
        rounds = sum(
            1.0 if e.get("role") == "summary" else 0.5
            for e in context
        )
        import math
        return math.ceil(rounds)

    @staticmethod
    def _split_first_rounds(
        context: list[dict], n: int,
    ) -> tuple[list[dict], list[dict]]:
        """从头部取出最早的 n 轮(总结块 1 轮, 普通消息 2 条=1 轮)。

        返回 (head, tail); 边界可能落在半轮(未配对的用户消息)。
        """
        head: list[dict] = []
        tail: list[dict] = []
        removed = 0.0
        for e in context:
            if removed < n:
                head.append(e)
                removed += 1.0 if e.get("role") == "summary" else 0.5
            else:
                tail.append(e)
        return head, tail

    @staticmethod
    def _same_entries(a: list[dict], b: list[dict]) -> bool:
        """按内容比较两条目序列(并发压缩保护: 头部已被改则跳过)。"""
        def _dump(lst: list[dict]) -> list[str]:
            return [json.dumps(e, ensure_ascii=False, sort_keys=True) for e in lst]
        return _dump(a) == _dump(b)

    async def _compact(self, path: Path, context: list[dict]) -> None:
        """压缩: 总结最早的 n 轮 → 总结块插入最前; 总结失败直接裁剪。"""
        head, _tail = self._split_first_rounds(context, self._trim_remove_rounds)
        if not head:
            return

        summary_text: str | None = None
        if self._summary_enabled and self._summarizer is not None:
            try:
                summary_text = await self._summarizer(head)
            except Exception as e:
                logger.warning(f"上下文总结失败, 直接裁剪: {e}")
                summary_text = None

        summary_entry = None
        if summary_text and summary_text.strip():
            summary_entry = {
                "role": "summary",
                "content": summary_text.strip(),
                "timestamp": int(time.time()),
            }

        def _merge(cur):
            cur = cur if isinstance(cur, list) else []
            # 并发保护: 头部已被其他协程压缩(内容不匹配)则跳过
            if len(cur) < len(head) or not self._same_entries(cur[:len(head)], head):
                return cur
            rest = cur[len(head):]
            if summary_entry is not None:
                return [summary_entry] + rest
            return rest  # 总结失败: 直接裁剪

        await json_update(path, _merge, default=[])
        logger.info(
            f"上下文压缩: 移除 {len(head)} 条, 保留 {len(_tail)} 条, "
            f"总结块={'有' if summary_entry else '无'}"
        )

    async def clear_context(
        self, bot_id: str, chat_type: str, chat_id: str
    ) -> None:
        """Clear the current active session context."""
        if chat_type == "group":
            session_id = "main"
        else:
            index = await self._load_session_index(bot_id, chat_type, chat_id)
            session_id = index.get("active", "sess_main")

        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        await json_write(path, [])

    async def forget_last_n(
        self, bot_id: str, chat_type: str, chat_id: str, n: int
    ) -> int:
        """Remove the last N entries from current session context.
        Returns the number of entries actually removed.
        """
        if chat_type == "group":
            session_id = "main"
        else:
            index = await self._load_session_index(bot_id, chat_type, chat_id)
            session_id = index.get("active", "sess_main")

        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        removed = [0]

        def _forget(data):
            context = data if isinstance(data, list) else []
            if not context:
                return context
            removed[0] = min(n, len(context))
            return context[:-removed[0]]

        await json_update(path, _forget, default=[])
        return removed[0]

    # ── Session Switching (Private Only) ──────────────────────

    async def list_sessions(
        self, bot_id: str, chat_type: str, chat_id: str
    ) -> list[dict[str, Any]]:
        """List all sessions for a user."""
        if chat_type == "group":
            return [{"id": "main", "name": "群聊默认会话"}]
        index = await self._load_session_index(bot_id, chat_type, chat_id)
        return index.get("sessions", [])

    async def get_active_session_id(
        self, bot_id: str, chat_type: str, chat_id: str
    ) -> str:
        """Get the currently active session ID."""
        if chat_type == "group":
            return "main"
        index = await self._load_session_index(bot_id, chat_type, chat_id)
        return index.get("active", "sess_main")

    async def create_session(
        self, bot_id: str, chat_type: str, chat_id: str, name: str
    ) -> str:
        """Create a new session and switch to it. Returns the session ID."""
        if chat_type == "group":
            return "main"  # Groups don't support multi-session

        index = await self._load_session_index(bot_id, chat_type, chat_id)
        sessions = index.get("sessions", [])

        # Generate next session ID
        existing_ids = {s["id"] for s in sessions}
        n = 1
        while f"sess_{n:03d}" in existing_ids:
            n += 1
        session_id = f"sess_{n:03d}"

        sessions.append({
            "id": session_id,
            "name": name,
            "created": int(time.time()),
        })
        index["sessions"] = sessions
        index["active"] = session_id
        await self._save_session_index(bot_id, chat_type, chat_id, index)

        # Initialize empty context
        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        await json_write(path, [])

        logger.info(f"Created session {session_id} ('{name}') for {chat_type}:{chat_id}")
        return session_id

    async def switch_session(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str
    ) -> bool:
        """Switch to an existing session. Returns True on success."""
        if chat_type == "group":
            return session_id == "main"

        index = await self._load_session_index(bot_id, chat_type, chat_id)
        sessions = index.get("sessions", [])

        if not any(s["id"] == session_id for s in sessions):
            return False

        index["active"] = session_id
        await self._save_session_index(bot_id, chat_type, chat_id, index)
        logger.info(f"Switched to session {session_id} for {chat_type}:{chat_id}")
        return True

    async def delete_session(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str
    ) -> bool:
        """Delete a session. Cannot delete the last session or 'sess_main'."""
        if chat_type == "group":
            return False

        index = await self._load_session_index(bot_id, chat_type, chat_id)
        sessions = index.get("sessions", [])

        if session_id == "sess_main":
            return False  # Cannot delete default session

        new_sessions = [s for s in sessions if s["id"] != session_id]
        if len(new_sessions) < 1:
            return False  # Must keep at least one session

        index["sessions"] = new_sessions

        # If active was deleted, switch to first available
        if index.get("active") == session_id:
            index["active"] = new_sessions[0]["id"]

        await self._save_session_index(bot_id, chat_type, chat_id, index)

        # Delete the context file
        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        if path.exists():
            path.unlink()

        logger.info(f"Deleted session {session_id} for {chat_type}:{chat_id}")
        return True

    # ── Session Browsing / Editing (web panel) ────────────────

    async def list_chats(self, bot_id: str) -> list[dict[str, Any]]:
        """List all chats (private users + groups) for a bot, with session counts."""
        base = self._context_base(bot_id, "")
        if not base.exists():
            return []
        result: list[dict[str, Any]] = []
        for chat_type in ("private", "group"):
            type_dir = base / chat_type
            if not type_dir.exists():
                continue
            for chat_dir in sorted(type_dir.iterdir()):
                if not chat_dir.is_dir():
                    continue
                chat_id = chat_dir.name
                sessions = await self.list_sessions(bot_id, chat_type, chat_id)
                result.append({
                    "chat_type": chat_type,
                    "chat_id": chat_id,
                    "session_count": len(sessions),
                })
        return result

    async def get_session(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str | None = None
    ) -> dict[str, Any] | None:
        """Get a session's messages plus metadata. session_id=None → active session."""
        if chat_type == "group":
            session_id = "main"
        else:
            index = await self._load_session_index(bot_id, chat_type, chat_id)
            if session_id is None:
                session_id = index.get("active", "sess_main")
            if not any(s["id"] == session_id for s in index.get("sessions", [])):
                return None

        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        messages = await json_read(path)
        if messages is None:
            messages = []

        name = session_id
        if chat_type == "private":
            index = await self._load_session_index(bot_id, chat_type, chat_id)
            for s in index.get("sessions", []):
                if s["id"] == session_id:
                    name = s.get("name", session_id)
                    break

        return {
            "id": session_id,
            "name": name,
            "chat_type": chat_type,
            "chat_id": chat_id,
            "messages": messages if isinstance(messages, list) else [],
        }

    async def update_message(
        self, bot_id: str, chat_type: str, chat_id: str,
        session_id: str, index: int, content: str, role: str | None = None,
    ) -> bool:
        """Edit a single message in a session. Returns True on success."""
        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        messages = await json_read(path)
        if not isinstance(messages, list) or not (0 <= index < len(messages)):
            return False
        messages[index]["content"] = content
        if role:
            messages[index]["role"] = role
        await json_write(path, messages)
        return True

    async def reset_session(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str
    ) -> bool:
        """Reset (clear) a session's messages. Returns True on success."""
        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        if not path.exists():
            return False
        await json_write(path, [])
        return True