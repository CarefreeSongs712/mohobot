"""Independent atomic per-bot/user A/B storage. Never stores credentials/history."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from mohobot.file_store import _get_lock
from mohobot.persona_service import _atomic_json

ACTIVE = {"generating", "sending", "pending"}


class PersonaABRepository:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "persona_ab"

    def path(self, bot_id, user_id):
        if not re.fullmatch(r"bot_\d+", str(bot_id)) or not re.fullmatch(r"\d+", str(user_id)):
            raise ValueError("非法评估身份")
        return self.root / str(bot_id) / f"{user_id}.json"

    @staticmethod
    def expire(data, now):
        for record in data["records"]:
            if record["state"] == "pending" and now >= record["expires_at"]:
                record.update(state="expired", ended_at=now)
        if data.get("active") and not any(r["id"] == data["active"] and r["state"] in ACTIVE for r in data["records"]):
            data["active"] = None

    async def transaction(self, bot_id, user_id, operation):
        path = self.path(bot_id, user_id)
        async with _get_lock(str(path.absolute())):
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
                "version": 1, "cooldown_until": 0, "active": None, "records": [],
            }
            before = copy.deepcopy(data)
            result = operation(data)  # synchronous only: no network or persona lock
            if data != before:
                _atomic_json(path, data)
            return copy.deepcopy(result)

    async def update(self, bot_id, user_id, identifier, **changes):
        def apply(data):
            record = next(r for r in data["records"] if r["id"] == identifier)
            record.update(changes)
            if record["state"] not in ACTIVE and data.get("active") == identifier:
                data["active"] = None
            return record
        return await self.transaction(bot_id, user_id, apply)

    async def records(self, now):
        result = []
        for path in sorted(self.root.glob("bot_*/*.json")):
            def read(data):
                self.expire(data, now)
                return data["records"]
            result.extend(await self.transaction(path.parent.name, path.stem, read))
        return sorted(result, key=lambda r: r["created_at"], reverse=True)

    async def recover(self, now):
        for path in sorted(self.root.glob("bot_*/*.json")):
            def repair(data):
                for record in data["records"]:
                    if record["state"] in ("generating", "sending"):
                        record.update(state="aborted" if record["state"] == "generating" else "delivery_unknown", ended_at=now)
                self.expire(data, now)
            await self.transaction(path.parent.name, path.stem, repair)
