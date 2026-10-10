"""Shared persona presets, legacy migration and private-session bindings.

Lock order is service transaction -> context maintenance -> chat -> file_store path.
Resolution is a synchronous cache read; writes publish only after atomic replace.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from mohobot.file_store import _get_lock
from mohobot.models.config import BotConfig

DEFAULT_PERSONA_ID = "persona_001"
DEFAULT_PERSONA_CONTENT = "你是 Mohobot，一个有用的 AI 助手。"


class PersonaNotFoundError(ValueError):
    """A requested preset does not exist."""


class PersonaInUseError(ValueError):
    def __init__(self, message: str, references: list[dict]):
        super().__init__(message)
        self.references = references


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".personas-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PersonaService:
    def __init__(self, data_dir, bot_manager, context_manager):
        self._data_dir = Path(data_dir)
        self._bot_manager = bot_manager
        self._context_manager = context_manager
        self.path = self._data_dir / "personas" / "personas.json"
        self.lock = _get_lock(str(self.path.absolute()) + ".service")
        self._library: dict = {"version": 1, "next_id": 2, "personas": []}
        self._cache: dict[str, dict] = {}
        self._references_cache: tuple[float, list[dict]] | None = None
        self._references_ttl = 30.0

    @staticmethod
    def _text(name, content) -> tuple[str, str]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("人设名称不能为空")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("人设正文不能为空")
        # Preserve the actual prompt, including meaningful whitespace.
        return name.strip(), content

    @classmethod
    def _validate_library(cls, data: dict) -> dict:
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("人设库版本或格式非法")
        records = data.get("personas")
        next_id = data.get("next_id")
        if not isinstance(records, list) or type(next_id) is not int or next_id < 2:
            raise ValueError("人设库格式非法")
        seen = set()
        max_id = 0
        for item in records:
            if not isinstance(item, dict):
                raise ValueError("人设记录格式非法")
            identifier = item.get("id")
            match = re.fullmatch(r"persona_(\d{3,})", identifier) if isinstance(identifier, str) else None
            if not match or int(match.group(1)) < 1 or identifier != f"persona_{int(match.group(1)):03d}" or identifier in seen:
                raise ValueError("人设ID非法或重复")
            cls._text(item.get("name"), item.get("content"))
            for field in ("created", "updated"):
                if type(item.get(field)) is not int or item[field] < 0:
                    raise ValueError("人设时间戳非法")
            seen.add(identifier)
            max_id = max(max_id, int(match.group(1)))
        if DEFAULT_PERSONA_ID not in seen:
            raise ValueError("系统默认人设必须保留")
        if next_id <= max_id:
            raise ValueError("人设next_id不可回绕")
        return copy.deepcopy(data)

    async def _publish(self, data: dict) -> None:
        async with _get_lock(str(self.path.absolute())):
            _atomic_json(self.path, data)
        self._library = copy.deepcopy(data)
        self._cache = {item["id"]: copy.deepcopy(item) for item in data["personas"]}
        self.invalidate_references()

    def invalidate_references(self) -> None:
        self._references_cache = None

    async def startup(self) -> None:
        async with self.lock:
            async with _get_lock(str(self.path.absolute())):
                if self.path.exists():
                    # Empty and malformed files must fail, never silently reset.
                    data = self._validate_library(json.loads(self.path.read_text(encoding="utf-8")))
                else:
                    now = int(time.time())
                    data = {"version": 1, "next_id": 2, "personas": [{
                        "id": DEFAULT_PERSONA_ID, "name": "默认人设", "content": DEFAULT_PERSONA_CONTENT,
                        "created": now, "updated": now,
                    }]}
                    _atomic_json(self.path, data)
            self._library = copy.deepcopy(data)
            self._cache = {item["id"]: copy.deepcopy(item) for item in data["personas"]}
            for cfg in self._bot_manager.list_bot_configs():
                await self._migrate_bot(cfg.bot_id)

    async def _migrate_bot(self, bot_id: str) -> None:
        path = self._bot_manager._bot_config_path(bot_id)
        async with _get_lock(str(path.absolute())):
            raw_bytes = path.read_bytes()
            json.loads(raw_bytes.decode("utf-8"))  # Fail before changing corrupt config.
            cfg = BotConfig.load(path)
            legacy = cfg.persona
            if not isinstance(legacy, str):
                raise ValueError(f"bot {bot_id} 的旧人设文本非法")
            needs_migration = not cfg.persona_id or (
                cfg.persona_id not in self._cache and legacy.strip()
            ) or (
                cfg.persona_id == DEFAULT_PERSONA_ID and legacy.strip()
                and legacy != DEFAULT_PERSONA_CONTENT
            )
            if not needs_migration and not legacy:
                return
            if needs_migration:
                target = DEFAULT_PERSONA_ID
                if legacy.strip():
                    found = next((p for p in self._cache.values() if p["content"] == legacy), None)
                    if found:
                        target = found["id"]
                    else:
                        record = await self._create_unlocked(cfg.nickname or bot_id, legacy)
                        target = record["id"]
                cfg.persona_id = target
            # Back up original bytes BEFORE changing config. Reuse a matching backup
            # after an interrupted startup; migrated configs never back up again.
            backups = sorted(path.parent.glob("config.json.persona-backup-*.bak"))
            if not any(backup.read_bytes() == raw_bytes for backup in backups):
                backup = path.with_name(f"config.json.persona-backup-{time.time_ns()}.bak")
                with backup.open("xb") as stream:
                    stream.write(raw_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
            cfg.persona = ""
            cfg.save(path)
            instance = self._bot_manager.get(bot_id)
            if instance is not None:
                instance.config = cfg

    def get_persona(self, persona_id: str) -> dict | None:
        record = self._cache.get(persona_id) if isinstance(persona_id, str) else None
        return copy.deepcopy(record) if record is not None else None

    def _require_persona(self, persona_id: str) -> dict:
        record = self.get_persona(persona_id)
        if record is None:
            raise PersonaNotFoundError(f"人设不存在: {persona_id}")
        return record

    async def list_persona_options(self) -> list[dict]:
        """Read a published snapshot without waiting for reference scans or storage."""
        return copy.deepcopy(self._library["personas"])

    async def list_personas(self) -> list[dict]:
        async with self.lock:
            references = await self._all_references_unlocked()
            return [dict(copy.deepcopy(p), reference_count=sum(r["persona_id"] == p["id"] for r in references))
                    for p in self._library["personas"]]

    async def _create_unlocked(self, name: str, content: str) -> dict:
        name, content = self._text(name, content)
        data = copy.deepcopy(self._library)
        identifier = f"persona_{data['next_id']:03d}"
        data["next_id"] += 1
        now = int(time.time())
        record = {"id": identifier, "name": name, "content": content, "created": now, "updated": now}
        data["personas"].append(record)
        await self._publish(data)
        return copy.deepcopy(record)

    async def create_persona(self, name: str, content: str) -> dict:
        async with self.lock:
            return await self._create_unlocked(name, content)

    async def update_persona(self, persona_id: str, name: str, content: str) -> dict:
        async with self.lock:
            self._require_persona(persona_id)
            name, content = self._text(name, content)
            data = copy.deepcopy(self._library)
            record = next(p for p in data["personas"] if p["id"] == persona_id)
            record.update(name=name, content=content, updated=int(time.time()))
            await self._publish(data)
            return copy.deepcopy(record)

    def _scan_references(self) -> list[dict]:
        references = []
        ab = getattr(self, "_ab_config", None)
        if ab is not None:
            for role in ("baseline_id", "test_id"):
                identifier = getattr(ab, role, "")
                if identifier:
                    references.append({"type": "persona_ab", "persona_id": identifier, "role": role})
        for cfg in self._bot_manager.list_bot_configs():
            references.append({"type": "bot", "bot_id": cfg.bot_id,
                               "nickname": cfg.nickname, "persona_id": cfg.persona_id or DEFAULT_PERSONA_ID})
        # Read only the fields needed for references. Old indices must not be migrated
        # by a list request; safety-critical callers still reject unreadable bindings.
        base = self._data_dir / "contexts"
        for path in sorted(base.glob("*/private/*/session_index.json")):
            try:
                index = json.loads(path.read_text(encoding="utf-8"))
                sessions = index.get("sessions") if isinstance(index, dict) else None
                if not isinstance(sessions, list):
                    raise ValueError("sessions 字段非法")
                seen = set()
                for session in sessions:
                    if not isinstance(session, dict):
                        raise ValueError("session 字段非法")
                    sid = session.get("id")
                    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid) or sid in seen:
                        raise ValueError("session ID 非法或重复")
                    seen.add(sid)
                    pid = session.get("persona_id", "")
                    if not isinstance(pid, str) or (pid and not re.fullmatch(r"persona_\d{3,}", pid)):
                        raise ValueError("人设引用非法")
                    if pid:
                        references.append({
                            "type": "session", "bot_id": path.parent.parent.parent.name,
                            "user_id": path.parent.name, "session_id": sid,
                            "name": session.get("name", sid), "persona_id": pid,
                        })
            except (OSError, ValueError) as exc:
                relative = path.relative_to(self._data_dir)
                raise ValueError(f"无法检查人设引用: {relative} ({exc})") from exc
        return references

    async def _all_references_unlocked(self, *, force: bool = False) -> list[dict]:
        cached = self._references_cache
        if not force and cached is not None and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        # Service writes already hold self.lock; one worker scan serves queued reads.
        async with self._context_manager.maintenance_lock:
            scan = asyncio.create_task(asyncio.to_thread(self._scan_references))
            try:
                references = await asyncio.shield(scan)
            except asyncio.CancelledError:
                # Cancelling to_thread doesn't stop its reads. Drain even repeated
                # cancellation before a restore can replace the worker's files.
                while not scan.done():
                    try:
                        await asyncio.shield(scan)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not scan.cancelled():
                    scan.exception()
                raise
        self._references_cache = (time.monotonic() + self._references_ttl, references)
        return copy.deepcopy(references)

    async def list_references(self, persona_id: str) -> list[dict]:
        async with self.lock:
            self._require_persona(persona_id)
            return [r for r in await self._all_references_unlocked() if r["persona_id"] == persona_id]

    async def delete_persona(self, persona_id: str) -> None:
        async with self.lock:
            self._require_persona(persona_id)
            references = [r for r in await self._all_references_unlocked(force=True) if r["persona_id"] == persona_id]
            if references or persona_id == DEFAULT_PERSONA_ID:
                raise PersonaInUseError("人设正在使用或为系统默认，不能删除", references)
            data = copy.deepcopy(self._library)
            data["personas"] = [p for p in data["personas"] if p["id"] != persona_id]
            await self._publish(data)

    @staticmethod
    def _safe_component(value, field: str) -> str:
        value = str(value)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError(f"{field}非法")
        return value

    def _bot(self, bot_id: str) -> BotConfig:
        bot_id = self._safe_component(bot_id, "bot_id")
        path = self._bot_manager._bot_config_path(bot_id)
        if not path.is_file():
            raise ValueError("bot不存在")
        cfg = self._bot_manager.load_bot_config(bot_id)
        if cfg.bot_id != bot_id:
            raise ValueError("bot配置身份不一致")
        return cfg

    def _session_args(self, bot_id, user_id, session_id=None):
        cfg = self._bot(bot_id)
        user_id = str(user_id)
        if not re.fullmatch(r"[0-9]+", user_id):
            raise ValueError("用户ID必须是数字")
        if session_id is not None:
            session_id = self._safe_component(session_id, "session_id")
        return cfg, user_id, session_id

    async def update_bot_config(self, bot_id: str, data: dict) -> BotConfig:
        async with self.lock:
            cfg = self._bot(bot_id)
            if not isinstance(data, dict):
                raise ValueError("bot配置必须是对象")
            if "persona" in data:
                raise ValueError("旧persona文本不可修改，请使用persona_id")
            if "bot_id" in data and data["bot_id"] != bot_id:
                raise ValueError("bot_id不可修改")
            allowed = set(cfg.to_dict()) - {"persona", "bot_id"}
            self._require_persona(data.get("persona_id", cfg.persona_id or DEFAULT_PERSONA_ID))
            path = self._bot_manager._bot_config_path(bot_id)
            async with _get_lock(str(path.absolute())):
                # Reload after acquiring the path lock, retaining unknown-key ignore.
                cfg = self._bot(bot_id)
                for key, value in data.items():
                    if key in allowed:
                        setattr(cfg, key, value)
                cfg.persona_id = cfg.persona_id or DEFAULT_PERSONA_ID
                self._require_persona(cfg.persona_id)
                cfg.save(path)
                instance = self._bot_manager.get(bot_id)
                if instance is not None:
                    instance.config = cfg
            self.invalidate_references()
            return cfg

    def resolve_bot(self, bot_config: BotConfig) -> dict:
        return self.resolve(bot_config)

    def resolve(self, bot_config: BotConfig, session_persona_id: str = "") -> dict:
        warnings = []
        for identifier, source in ((session_persona_id, "session"),
                                   (getattr(bot_config, "persona_id", ""), "bot"),
                                   (DEFAULT_PERSONA_ID, "default")):
            if not identifier:
                continue
            record = self.get_persona(identifier)
            if record is not None:
                resolved = {key: record[key] for key in ("id", "name", "content")}
                resolved["source"] = source
                if warnings:
                    resolved["warnings"] = warnings
                return resolved
            warnings.append(f"{source}人设不存在: {identifier}，已回退")
        # Pre-startup resolution is safe but never imports old hardcoded persona text.
        return {"id": DEFAULT_PERSONA_ID, "name": "默认人设", "content": DEFAULT_PERSONA_CONTENT,
                "source": "default", "warnings": warnings}

    def _binding_result(self, cfg, user_id, session):
        result = {"bot_id": cfg.bot_id, "user_id": user_id, "session_id": session["id"],
                  "persona_id": session.get("persona_id", ""), "bot_persona_id": cfg.persona_id,
                  "effective": self.resolve(cfg, session.get("persona_id", ""))}
        if result["effective"].get("warnings"):
            result["warnings"] = result["effective"]["warnings"]
        return result

    async def bind_session(self, bot_id: str, user_id: str, session_id: str, persona_id: str, *, expected_generation=None, require_active=False, expected_content_hash=None, deadline=None, clock=None) -> dict:
        async with self.lock:
            cfg, user_id, session_id = self._session_args(bot_id, user_id, session_id)
            preset = self._require_persona(persona_id)
            if expected_content_hash is not None:
                import hashlib
                if hashlib.sha256(preset["content"].encode("utf-8")).hexdigest() != expected_content_hash:
                    raise ValueError("人设正文已修改，拒绝切换到旧快照")
            options = {}
            if expected_generation is not None or require_active:
                options = {"expected_generation": expected_generation, "require_active": require_active}
            if deadline is not None:
                options.update(deadline=deadline, clock=clock)
            session = await self._context_manager.set_session_persona(bot_id, "private", user_id, session_id, persona_id, **options)
            self.invalidate_references()
            return self._binding_result(cfg, user_id, session)

    async def clear_session_binding(self, bot_id: str, user_id: str, session_id: str) -> dict:
        async with self.lock:
            cfg, user_id, session_id = self._session_args(bot_id, user_id, session_id)
            session = await self._context_manager.set_session_persona(bot_id, "private", user_id, session_id, "")
            self.invalidate_references()
            return self._binding_result(cfg, user_id, session)

    async def get_session_binding(self, bot_id: str, user_id: str, session_id: str) -> dict:
        async with self.lock:
            cfg, user_id, session_id = self._session_args(bot_id, user_id, session_id)
            session = await self._context_manager.get_private_session_metadata(bot_id, user_id, session_id)
            if session is None:
                raise ValueError("会话不存在")
            return self._binding_result(cfg, user_id, session)

    async def list_user_sessions(self, bot_id: str, user_id: str) -> dict:
        async with self.lock:
            cfg, user_id, _ = self._session_args(bot_id, user_id)
            index = await self._context_manager.read_existing_session_index(bot_id, "private", user_id)
            if index is None:
                return {"sessions": [], "active": ""}
            sessions = []
            for item in index["sessions"]:
                item = dict(item)
                item.update(self._binding_result(cfg, user_id, item))
                sessions.append(item)
            return {"sessions": sessions, "active": index.get("active", "")}

    async def validate_restore(
        self, data: dict, extra_persona_ids: Iterable[str] = (), *,
        replaced_context_bots: Iterable[str] = (),
    ) -> dict:
        """Validate restore data under caller-held ``lock``; return merged high water.

        The caller must keep that lock until persistence/ZIP transaction completes.
        This method does not publish or write the persona library.
        """
        validated = self._validate_library(data)
        identifiers = {p["id"] for p in validated["personas"]}
        replaced = set(replaced_context_bots)
        referenced = {r["persona_id"] for r in await self._all_references_unlocked(force=True)
                      if r["type"] != "session" or r["bot_id"] not in replaced}
        referenced.update(identifier for identifier in extra_persona_ids if identifier)
        missing = referenced - identifiers
        if missing:
            raise ValueError(f"恢复人设库缺少使用中的人设: {', '.join(sorted(missing))}")
        validated["next_id"] = max(validated["next_id"], self._library["next_id"])
        return validated

    async def restore_library_locked(self, data: dict, *, extra_persona_ids: Iterable[str] = ()) -> None:
        """Restore under caller-held ``lock`` (for a larger backup transaction)."""
        await self._publish(await self.validate_restore(data, extra_persona_ids))

    async def restore_library(self, data: dict, *, extra_persona_ids: Iterable[str] = ()) -> None:
        async with self.lock:
            await self.restore_library_locked(data, extra_persona_ids=extra_persona_ids)
