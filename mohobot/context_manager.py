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

from mohobot.file_store import json_read, json_write


class ContextManager:
    """Manages session context CRUD with trimming."""

    def __init__(self, data_dir: str = "./data"):
        self._data_dir = data_dir

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
        """Append entries to the active session context, trimming to max_rounds."""
        if chat_type == "group":
            session_id = "main"
        else:
            index = await self._load_session_index(bot_id, chat_type, chat_id)
            session_id = index.get("active", "sess_main")

        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        context = await json_read(path)
        if not isinstance(context, list):
            context = []

        context.extend(entries)

        # Trim to max_rounds (keep last N user-assistant pairs)
        if len(context) > max_rounds * 2:
            context = context[-(max_rounds * 2):]

        await json_write(path, context)

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
        context = await self.load_context(bot_id, chat_type, chat_id)
        if not context:
            return 0

        actual = min(n, len(context))
        remaining = context[:-actual]

        if chat_type == "group":
            session_id = "main"
        else:
            index = await self._load_session_index(bot_id, chat_type, chat_id)
            session_id = index.get("active", "sess_main")

        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        await json_write(path, remaining)
        return actual

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