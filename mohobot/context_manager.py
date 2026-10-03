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
import re
import uuid
from functools import wraps
from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.file_store import _get_lock, json_read, json_update, json_write


def _chat_locked(method):
    @wraps(method)
    async def locked(self, bot_id, chat_type, chat_id, *args, **kwargs):
        async with self.maintenance_lock, self.chat_lock(bot_id, chat_type, chat_id):
            return await method(self, bot_id, chat_type, chat_id, *args, **kwargs)
    return locked


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
        summary_age_hours: int = 3,
        sweep_enabled: bool = True,
        sweep_interval_minutes: int = 30,
        min_interval_hours: int = 24,
    ):
        self._data_dir = data_dir
        self.maintenance_lock = _get_lock(str(Path(data_dir).absolute() / "contexts") + ".maintenance")
        # 异步总结回调: async (entries: list[dict]) -> str | None
        self._summarizer = summarizer
        self._summary_enabled = summary_enabled
        self._trim_at_rounds = max(2, trim_at_rounds)
        self._trim_remove_rounds = max(1, trim_remove_rounds)
        # 时间压缩(满轮顺带 + 周期任务): 旧对话年龄 / 周期扫描开关与间隔 /
        # 已压缩会话再次周期压缩的最小间隔("间隔一天", 防反复压)
        self._summary_age_hours = max(1, int(summary_age_hours))
        self._sweep_enabled = bool(sweep_enabled)
        self._sweep_interval_minutes = max(1, int(sweep_interval_minutes))
        self._min_interval_hours = max(1, int(min_interval_hours))

    def set_summarizer(self, summarizer) -> None:
        """注入总结回调(LLMService.summarize_context)。"""
        self._summarizer = summarizer

    def set_trim_config(
        self, *, enabled: bool | None = None,
        at_rounds: int | None = None, remove_rounds: int | None = None,
        age_hours: int | None = None, sweep_enabled: bool | None = None,
        sweep_interval_minutes: int | None = None,
        min_interval_hours: int | None = None,
    ) -> None:
        """热更新压缩配置(web 面板保存全局配置后调用)。"""
        if enabled is not None:
            self._summary_enabled = enabled
        if at_rounds is not None:
            self._trim_at_rounds = max(2, at_rounds)
        if remove_rounds is not None:
            self._trim_remove_rounds = max(1, remove_rounds)
        if age_hours is not None:
            self._summary_age_hours = max(1, int(age_hours))
        if sweep_enabled is not None:
            self._sweep_enabled = bool(sweep_enabled)
        if sweep_interval_minutes is not None:
            self._sweep_interval_minutes = max(1, int(sweep_interval_minutes))
        if min_interval_hours is not None:
            self._min_interval_hours = max(1, int(min_interval_hours))

    @property
    def sweep_enabled(self) -> bool:
        """周期时间压缩总开关(后台任务每周期读取)。"""
        return self._sweep_enabled

    @property
    def sweep_interval_minutes(self) -> int:
        """周期时间压缩间隔(分钟, 后台任务每周期读取, 热生效)。"""
        return self._sweep_interval_minutes

    def _context_base(self, bot_id: str, chat_type: str) -> Path:
        return Path(self._data_dir) / "contexts" / bot_id / chat_type

    def _session_index_path(self, bot_id: str, chat_type: str, chat_id: str) -> Path:
        return self._context_base(bot_id, chat_type) / chat_id / "session_index.json"

    def _session_file_path(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str
    ) -> Path:
        return self._context_base(bot_id, chat_type) / chat_id / f"{session_id}.json"

    def chat_lock(self, bot_id: str, chat_type: str, chat_id: str):
        """Shared per-chat transaction lock (also used by PersonaService)."""
        path = self._session_index_path(bot_id, chat_type, chat_id)
        return _get_lock(str(path.absolute()) + ".chat")

    @staticmethod
    def _new_session(session_id: str, name: str) -> dict:
        return {"id": session_id, "name": name, "created": int(time.time()),
                "generation": uuid.uuid4().hex, "persona_id": ""}

    # ── Session Index ─────────────────────────────────────────

    async def _read_index_unlocked(self, bot_id, chat_type, chat_id, *, create=False):
        path = self._session_index_path(bot_id, chat_type, chat_id)
        data = await json_read(path)
        if data is None:
            if path.exists():
                raise ValueError("会话索引为空或损坏")
            if not create:
                return None
            data = {"sessions": [self._new_session("sess_main", "默认会话")],
                    "active": "sess_main"}
            await json_write(path, data)
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            raise ValueError("会话索引格式非法")
        if not isinstance(data.get("active"), str):
            raise ValueError("会话索引active格式非法")
        changed = False
        seen = set()
        for session in data["sessions"]:
            if (not isinstance(session, dict) or not isinstance(session.get("id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", session["id"])):
                raise ValueError("会话索引含非法会话")
            if session["id"] in seen:
                raise ValueError("会话索引含重复ID")
            seen.add(session["id"])
            if "generation" not in session:
                session["generation"] = uuid.uuid4().hex
                changed = True
            elif (not isinstance(session["generation"], str)
                  or not re.fullmatch(r"[0-9a-f]{32}", session["generation"])):
                raise ValueError("会话generation必须是UUID")
            if "persona_id" not in session:
                session["persona_id"] = ""
                changed = True
            elif (not isinstance(session["persona_id"], str)
                  or (session["persona_id"] and not re.fullmatch(r"persona_[0-9]{3,}", session["persona_id"]))):
                raise ValueError("会话persona_id格式非法")
        if not seen or data["active"] not in seen:
            raise ValueError("会话索引active不存在")
        if changed:
            await json_write(path, data)
        return data

    @_chat_locked
    async def _load_session_index(self, bot_id: str, chat_type: str, chat_id: str) -> dict[str, Any]:
        return await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)

    @_chat_locked
    async def read_existing_session_index(self, bot_id: str, chat_type: str, chat_id: str) -> dict | None:
        """Read an existing index; never create a chat or switch its active session."""
        return await self._read_index_unlocked(bot_id, chat_type, chat_id)

    async def _metadata_unlocked(self, bot_id, chat_type, chat_id, session_id=None, generation=None, *, create=False):
        if chat_type == "group":
            metadata = {"id": "main", "name": "群聊默认会话", "generation": "group-main", "persona_id": ""}
            if session_id not in (None, "main") or generation not in (None, "group-main"):
                return None
            return metadata
        index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=create)
        if index is None:
            return None
        session_id = session_id if session_id is not None else index.get("active", "sess_main")
        metadata = next((s for s in index["sessions"] if s["id"] == session_id), None)
        if metadata is None or (generation is not None and generation != metadata["generation"]):
            return None
        return dict(metadata)

    @_chat_locked
    async def capture_session(self, bot_id: str, chat_type: str, chat_id: str) -> dict:
        metadata = await self._metadata_unlocked(bot_id, chat_type, chat_id, create=True)
        if metadata is None:
            raise ValueError("当前活动会话不存在")
        return {key: metadata[key] for key in ("id", "generation", "persona_id")}

    async def get_private_session_metadata(self, bot_id: str, user_id: str, session_id: str) -> dict | None:
        async with self.maintenance_lock, self.chat_lock(bot_id, "private", user_id):
            return await self._metadata_unlocked(bot_id, "private", user_id, session_id)

    @_chat_locked
    async def set_session_persona(self, bot_id: str, chat_type: str, chat_id: str, session_id: str, persona_id: str) -> dict:
        if chat_type != "private":
            raise ValueError("只有私聊会话允许覆盖人设")
        index = await self._read_index_unlocked(bot_id, chat_type, chat_id)
        metadata = next((s for s in (index or {}).get("sessions", []) if s["id"] == session_id), None)
        if metadata is None:
            raise ValueError("会话不存在")
        metadata["persona_id"] = persona_id
        await self._save_session_index(bot_id, chat_type, chat_id, index)
        return dict(metadata)

    async def _save_session_index(
        self, bot_id: str, chat_type: str, chat_id: str, data: dict
    ) -> None:
        """Save the session index."""
        path = self._session_index_path(bot_id, chat_type, chat_id)
        await json_write(path, data)

    # ── Context CRUD ───────────────────────────────────────────

    @_chat_locked
    async def load_context(
        self, bot_id: str, chat_type: str, chat_id: str, *,
        session_id: str | None = None, generation: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read the captured session, or the active session for legacy callers."""
        metadata = await self._metadata_unlocked(
            bot_id, chat_type, chat_id, session_id, generation,
            create=session_id is None and generation is None,
        )
        if metadata is None:
            return []
        data = await json_read(self._session_file_path(bot_id, chat_type, chat_id, metadata["id"]))
        return data if isinstance(data, list) else []

    async def append_context(
        self, bot_id: str, chat_type: str, chat_id: str,
        entries: list[dict[str, Any]], *,
        session_id: str | None = None, generation: str | None = None,
    ) -> bool:
        """Append only to the captured generation; deletion never revives files."""
        async with self.maintenance_lock, self.chat_lock(bot_id, chat_type, chat_id):
            metadata = await self._metadata_unlocked(
                bot_id, chat_type, chat_id, session_id, generation,
                create=session_id is None and generation is None,
            )
            if metadata is None:
                return False
            path = self._session_file_path(bot_id, chat_type, chat_id, metadata["id"])
            def _append(data):
                context = data if isinstance(data, list) else []
                return context + entries
            context = await json_update(path, _append, default=[])
        # Slow summarization runs outside the chat lock; merge revalidates generation.
        if context and self._count_rounds(context) >= self._trim_at_rounds:
            await self._compact(path, context, head=self._head_for_compaction(context),
                                expected_generation=metadata["generation"])
        return True

    # ── 时间压缩(旧对话判定) ────────────────────────────────

    def _old_prefix_len(self, context: list[dict], now: float | None = None) -> int:
        """旧前缀长度: 前导 summary 块 + 时间戳早于 cutoff 的消息。

        cutoff = now - summary_age_hours。要求旧部分至少含 1 条非 summary 消息,
        否则返回 0 —— 防止对孤立旧总结块反复再总结(无意义轮换)。
        缺时间戳的条目视为足够旧(旧版遗留数据, 会被时间压缩收走)。
        """
        cutoff = (now if now is not None else time.time()) - self._summary_age_hours * 3600
        end = 0
        saw_old_content = False
        for i, entry in enumerate(context):
            if entry.get("role") == "summary":
                if i == 0:
                    end = 1  # 前导总结块计入前缀(可随本次一并再总结)
                    continue
                break  # 中段 summary 理论不发生, 防御性停下
            ts = entry.get("timestamp")
            old = True
            if ts is not None:
                try:
                    old = float(ts) < cutoff
                except (TypeError, ValueError):
                    old = True
            if not old:
                break
            saw_old_content = True
            end = i + 1
        return end if saw_old_content else 0

    def _head_for_compaction(self, context: list[dict]) -> list[dict]:
        """满轮触发时待总结的头部 = 最早的 trim_remove_rounds 轮 ∪ 超龄旧对话(取更长前缀)。"""
        head, _tail = self._split_first_rounds(context, self._trim_remove_rounds)
        old_len = self._old_prefix_len(context)
        if old_len > len(head):
            head = context[:old_len]
        return head

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

    async def _compact(
        self, path: Path, context: list[dict],
        head: list[dict] | None = None, trim_on_failure: bool = True,
        expected_generation: str | None = None,
    ) -> bool:
        """压缩: 总结 head(缺省为最早的 trim_remove_rounds 轮) → 总结块插入最前。

        trim_on_failure=True(满轮触发路径): 总结失败/关闭时直接裁剪 head, 防上下文无限增长;
        trim_on_failure=False(周期时间压缩路径): 总结失败保留原数据, 等下次周期重试。
        返回是否实际改写了文件(并发守卫跳过合并时返回 False)。
        """
        if head is None:
            head, _tail = self._split_first_rounds(context, self._trim_remove_rounds)
        if not head:
            return False

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
        if summary_entry is None and not trim_on_failure:
            logger.info(f"周期时间压缩跳过(总结不可用), 保留 {len(head)} 条待下次重试")
            return False

        changed = [False]

        def _merge(cur):
            cur = cur if isinstance(cur, list) else []
            # 并发保护: 头部已被其他协程压缩(内容不匹配)则跳过
            if len(cur) < len(head) or not self._same_entries(cur[:len(head)], head):
                return cur
            rest = cur[len(head):]
            if summary_entry is not None:
                changed[0] = True
                return [summary_entry] + rest
            changed[0] = True  # 总结失败/关闭: 直接裁剪(仅满轮路径)
            return rest

        bot_id, chat_type, chat_id = path.parent.parent.parent.name, path.parent.parent.name, path.parent.name
        async with self.maintenance_lock, self.chat_lock(bot_id, chat_type, chat_id):
            metadata = await self._metadata_unlocked(bot_id, chat_type, chat_id, path.stem, expected_generation)
            if metadata is None or not path.exists():
                return False
            await json_update(path, _merge, default=[])
        if changed[0]:
            logger.info(
                f"上下文压缩: 移除 {len(head)} 条, "
                f"总结块={'有' if summary_entry else '无'}"
            )
        return changed[0]

    # ── 周期时间压缩(后台任务) ──────────────────────────────

    async def sweep_context(
        self, bot_id: str, chat_type: str, chat_id: str,
    ) -> bool:
        """周期时间压缩单个会话(群聊 main.json / 私聊当前活动会话)。

        判定: 旧前缀(超过 summary_age_hours, 前导 summary 允许一并再总结)非空才处理;
        - 总轮数 ≥ trim_at_rounds → 立即压缩(超限豁免);
        - 否则: 从未压缩过(头部无 summary)→ 压缩;
                已压缩过 → 距上次压缩 ≥ min_interval_hours 才压缩(否则跳过, 防反复压)。
        总结失败不裁剪(保留数据下次重试)。返回是否实际触发压缩。
        """
        if not self._summary_enabled:
            return False
        async with self.maintenance_lock, self.chat_lock(bot_id, chat_type, chat_id):
            metadata = await self._metadata_unlocked(bot_id, chat_type, chat_id, create=True)
            if metadata is None:
                return False
            path = self._session_file_path(bot_id, chat_type, chat_id, metadata["id"])
            context = await json_read(path)
        if not isinstance(context, list) or not context:
            return False

        now = time.time()
        old_len = self._old_prefix_len(context, now)
        if old_len <= 0:
            return False
        head = context[:old_len]

        # day-gate: 未超限且已压缩过 → 距上次压缩不足 min_interval_hours 则跳过
        if self._count_rounds(context) < self._trim_at_rounds:
            first = context[0]
            if first.get("role") == "summary":
                last_ts = first.get("timestamp")
                try:
                    last_ts_f = float(last_ts) if last_ts is not None else 0.0
                except (TypeError, ValueError):
                    last_ts_f = 0.0
                if last_ts_f > 0 and (now - last_ts_f) < self._min_interval_hours * 3600:
                    return False
        return await self._compact(path, context, head=head, trim_on_failure=False,
                                   expected_generation=metadata["generation"])

    async def sweep_all_sessions(self) -> int:
        """扫描全部会话做时间压缩(群聊 main + 私聊当前活动会话), 返回触发数。"""
        base = Path(self._data_dir) / "contexts"
        if not base.exists():
            return 0
        done = 0
        for bot_dir in sorted(base.iterdir()):
            if not bot_dir.is_dir():
                continue
            bot_id = bot_dir.name
            for chat_type in ("private", "group"):
                type_dir = bot_dir / chat_type
                if not type_dir.exists():
                    continue
                for chat_dir in sorted(type_dir.iterdir()):
                    if not chat_dir.is_dir():
                        continue
                    try:
                        if await self.sweep_context(bot_id, chat_type, chat_dir.name):
                            done += 1
                    except Exception as e:
                        logger.warning(
                            f"会话时间压缩失败({bot_id} {chat_type}:{chat_dir.name}): {e}"
                        )
        if done:
            logger.info(f"周期时间压缩完成: {done} 个会话")
        return done

    @_chat_locked
    async def clear_context(
        self, bot_id: str, chat_type: str, chat_id: str
    ) -> None:
        """Clear the current active session context."""
        if chat_type == "group":
            session_id = "main"
        else:
            index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)
            session_id = index.get("active", "sess_main")

        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        await json_write(path, [])

    @_chat_locked
    async def forget_last_n(
        self, bot_id: str, chat_type: str, chat_id: str, n: int
    ) -> int:
        """Remove the last N entries from current session context.
        Returns the number of entries actually removed.
        """
        if chat_type == "group":
            session_id = "main"
        else:
            index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)
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

    @_chat_locked
    async def list_sessions(
        self, bot_id: str, chat_type: str, chat_id: str
    ) -> list[dict[str, Any]]:
        """List all sessions for a user."""
        if chat_type == "group":
            return [await self._metadata_unlocked(bot_id, chat_type, chat_id)]
        index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)
        return index.get("sessions", [])

    @_chat_locked
    async def get_active_session_id(
        self, bot_id: str, chat_type: str, chat_id: str
    ) -> str:
        """Get the currently active session ID."""
        if chat_type == "group":
            return "main"
        index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)
        return index.get("active", "sess_main")

    @_chat_locked
    async def create_session(
        self, bot_id: str, chat_type: str, chat_id: str, name: str
    ) -> str:
        """Create a new session and switch to it. Returns the session ID."""
        if chat_type == "group":
            return "main"  # Groups don't support multi-session

        index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)
        sessions = index.get("sessions", [])

        # Generate next session ID
        existing_ids = {s["id"] for s in sessions}
        n = 1
        while f"sess_{n:03d}" in existing_ids:
            n += 1
        session_id = f"sess_{n:03d}"

        sessions.append(self._new_session(session_id, name))
        index["sessions"] = sessions
        index["active"] = session_id
        await self._save_session_index(bot_id, chat_type, chat_id, index)

        # Initialize empty context
        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        await json_write(path, [])

        logger.info(f"Created session {session_id} ('{name}') for {chat_type}:{chat_id}")
        return session_id

    @_chat_locked
    async def switch_session(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str
    ) -> bool:
        """Switch to an existing session. Returns True on success."""
        if chat_type == "group":
            return session_id == "main"

        index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)
        sessions = index.get("sessions", [])

        if not any(s["id"] == session_id for s in sessions):
            return False

        index["active"] = session_id
        await self._save_session_index(bot_id, chat_type, chat_id, index)
        logger.info(f"Switched to session {session_id} for {chat_type}:{chat_id}")
        return True

    @_chat_locked
    async def delete_session(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str
    ) -> bool:
        """Delete a session. Cannot delete the last session or 'sess_main'."""
        if chat_type == "group":
            return False

        index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)
        sessions = index.get("sessions", [])

        if session_id == "sess_main":
            return False  # Cannot delete default session

        if not any(s["id"] == session_id for s in sessions):
            return False
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
        async with _get_lock(str(path.absolute())):
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

    @_chat_locked
    async def get_session(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str | None = None
    ) -> dict[str, Any] | None:
        """Get a session's messages plus metadata. session_id=None → active session."""
        if chat_type == "group":
            session_id = "main"
        else:
            index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)
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
            index = await self._read_index_unlocked(bot_id, chat_type, chat_id, create=True)
            for s in index.get("sessions", []):
                if s["id"] == session_id:
                    name = s.get("name", session_id)
                    break

        return {
            **(await self._metadata_unlocked(bot_id, chat_type, chat_id, session_id)),
            "id": session_id,
            "name": name,
            "chat_type": chat_type,
            "chat_id": chat_id,
            "messages": messages if isinstance(messages, list) else [],
        }

    @_chat_locked
    async def update_message(
        self, bot_id: str, chat_type: str, chat_id: str,
        session_id: str, index: int, content: str, role: str | None = None,
    ) -> bool:
        """Edit a single message in a session. Returns True on success."""
        if await self._metadata_unlocked(bot_id, chat_type, chat_id, session_id) is None:
            return False
        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        messages = await json_read(path)
        if not isinstance(messages, list) or not (0 <= index < len(messages)):
            return False
        messages[index]["content"] = content
        if role:
            messages[index]["role"] = role
        await json_write(path, messages)
        return True

    @_chat_locked
    async def reset_session(
        self, bot_id: str, chat_type: str, chat_id: str, session_id: str
    ) -> bool:
        """Reset (clear) a session's messages. Returns True on success."""
        if await self._metadata_unlocked(bot_id, chat_type, chat_id, session_id) is None:
            return False
        path = self._session_file_path(bot_id, chat_type, chat_id, session_id)
        if not path.exists():
            return False
        await json_write(path, [])
        return True