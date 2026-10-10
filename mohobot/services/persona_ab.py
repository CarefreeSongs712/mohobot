"""Single-turn private persona preference evaluation, independent of chat persistence.

Repository callbacks never await or acquire persona locks. Network/rendering run
outside storage locks. Only confirmed complete delivery is an exposure.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import random
import time
import uuid

from mohobot.file_store import _get_lock
from mohobot.models.config import PersonaABConfig
from mohobot.models.onebot import PrivateMessageEvent
from mohobot.services.persona_ab_repository import PersonaABRepository
from mohobot.utils.persona_ab_card import render_comparison

BOT_ALLOWLIST = frozenset({"bot_001", "bot_009", "bot_002"})
VOTES = {"A", "B", "平局", "都不好", "跳过"}


def content_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def delivery_status(response):
    if not isinstance(response, dict):
        return "unknown"
    if response.get("delivery_status") in {"success", "failed", "unknown"}:
        return response["delivery_status"]
    if response.get("status") == "ok" and response.get("retcode") == 0:
        return "success"
    if response.get("status") == "failed" or response.get("retcode", 0) != 0:
        return "failed"
    return "unknown"


class PersonaABService:
    PREPARE_TIMEOUT = 60.0
    GENERATE_TIMEOUT = 120.0
    RETRY_TIMEOUT = 60.0
    SEND_TIMEOUT = 15.0
    RENDER_TIMEOUT = 30.0

    def __init__(self, data_dir, config, persona_service, llm_service, *, clock=time.time, rng=None):
        self.repo = PersonaABRepository(data_dir)
        self.config = config
        self.personas = persona_service
        self.llm = llm_service
        self.clock = clock
        self.rng = rng or random.SystemRandom()
        self.personas._ab_config = config

    def validate_config(self, config):
        config = PersonaABConfig.from_dict(config.to_dict())
        ids = (config.baseline_id, config.test_id)
        if config.enabled or any(ids):
            if not all(ids) or ids[0] == ids[1]:
                raise ValueError("基准与测试必须选择两个不同的现有人设")
            for identifier in ids:
                if self.personas.get_persona(identifier) is None:
                    raise ValueError(f"人设不存在: {identifier}")
        return config

    async def startup(self):
        self.validate_config(self.config)
        await self.repo.recover(self.clock())

    async def reserve(self, bot_id, event, session, effective):
        if bot_id not in BOT_ALLOWLIST or not isinstance(event, PrivateMessageEvent) or session is None:
            return None
        # Snapshot configuration + presets together, before taking repository lock.
        async with self.personas.lock:
            cfg = copy.deepcopy(self.config)
            if not cfg.enabled:
                return None
            self.validate_config(cfg)
            snapshots = {}
            for side, identifier in (("baseline", cfg.baseline_id), ("test", cfg.test_id)):
                snapshot = self.personas.get_persona(identifier)
                snapshot["hash"] = content_hash(snapshot["content"])
                snapshots[side] = snapshot
        now = self.clock()
        from mohobot.utils.cq_code import extract_plain_text
        question = extract_plain_text(event.message)
        selected = "test" if effective.get("id") == cfg.test_id else "baseline"
        def claim(data):
            self.repo.expire(data, now)
            if data.get("active") or now < data["cooldown_until"]:
                return None
            if event.message_id and any(r["source_message_id"] == str(event.message_id) for r in data["records"]):
                return None
            if self.rng.random() >= cfg.probability:
                return None
            sides = ["baseline", "test"]
            self.rng.shuffle(sides)
            record = {
                "id": uuid.uuid4().hex[:16], "bot_id": bot_id, "user_id": str(event.user_id),
                "source_message_id": str(event.message_id), "session_id": session["id"],
                "session_generation": session["generation"], "personas": snapshots,
                "mapping": dict(zip(("A", "B"), sides)), "context_side": selected,
                "created_at": now, "expires_at": now + cfg.pending_seconds,
                "pending_seconds": cfg.pending_seconds, "max_pages": cfg.max_pages,
                "state": "generating", "question": question, "candidates": {}, "parameters": {},
                "vote": None, "switch": None, "exposed_at": None, "context_written": False,
            }
            data["records"].append(record)
            data["active"] = record["id"]
            data["cooldown_until"] = now + cfg.cooldown_seconds
            return record
        return await self.repo.transaction(bot_id, event.user_id, claim)

    async def run(self, record, event, context, bot_config, ws):
        """Return only the selected, delivered answer (or empty); never normal-chat retry."""
        bot_id, user_id, identifier = record["bot_id"], record["user_id"], record["id"]
        async def update(**changes):
            record.update(changes)
            await self.repo.update(bot_id, user_id, identifier, **changes)
        async def send(segments):
            try:
                response = await asyncio.wait_for(ws.send_private_msg(bot_id, user_id, segments, source="persona_ab", wait_response=True), self.SEND_TIMEOUT)
                return delivery_status(response)
            except Exception:
                return "unknown"  # transport exceptions may occur after delivery
        async def failure_notice():
            await send([{"type": "text", "data": {"text": "本轮评估未能完成，未记录错误内容为聊天答案。请稍后继续聊天。"}}])
        frozen = None
        prepared = None
        candidates = {}
        errors = {}
        try:
            frozen = self.llm.freeze_completion(bot_config)
            notice = await send([{"type": "text", "data": {"text": (
                "你好呀！提前跟你说一声，接下来会有一个小环节想邀请你参与～\n"
                "我会展示两组回复（A 和 B），你可以进行比较。本轮的对话内容、两版回答以及你的反馈都会被记录下来，用于人设提示词的改进。\n"
                "你完全可以继续正常跟我聊天，不用有任何顾虑。之后如果想表达偏好，随时用 /ab A 或 /ab B 投票就好～放心，投票不会自动切换我的人设，一切由你掌控。"
            )}}])
            prepared = await asyncio.wait_for(self.llm.prepare_input(bot_id, event, context, bot_config), self.PREPARE_TIMEOUT)
            await update(parameters=copy.deepcopy(frozen["parameters"]), question=prepared[-1]["content"])
            async def generate(side):
                try:
                    candidates[side] = await self.llm.complete_prepared(bot_id, event, prepared, record["personas"][side]["content"], frozen)
                except Exception as exc:
                    errors[side] = type(exc).__name__  # never persist URLs/keys from exceptions
            try:
                await asyncio.wait_for(asyncio.gather(generate("baseline"), generate("test")), self.GENERATE_TIMEOUT)
            except asyncio.TimeoutError:
                for side in ("baseline", "test"):
                    if side not in candidates:
                        errors[side] = "TimeoutError"
            await update(candidates=candidates, generation_errors=errors)
            if len(candidates) == 2 and notice == "success":
                try:
                    images = await asyncio.wait_for(asyncio.to_thread(render_comparison, identifier,
                        candidates[record["mapping"]["A"]], candidates[record["mapping"]["B"]], record["max_pages"]), self.RENDER_TIMEOUT)
                except Exception as exc:
                    await update(render_error=type(exc).__name__)
                else:
                    await update(state="sending", delivery_kind="images")
                    status = await send(images)  # one group; never count partial delivery
                    if status == "success":
                        now = self.clock()
                        await update(state="pending", exposed_at=now, expires_at=now + record["pending_seconds"], delivery="success")
                        return candidates[record["context_side"]]
                    await update(image_delivery=status)
            # Controlled fallback: exactly one retry, only for the context candidate.
            side = record["context_side"]
            if side not in candidates:
                try:
                    await asyncio.wait_for(generate(side), self.RETRY_TIMEOUT)
                except asyncio.TimeoutError:
                    errors[side] = "TimeoutError"
                await update(candidates=candidates, generation_errors=errors, retry_count=1)
            if side not in candidates:
                await failure_notice()
                await update(state="failed", ended_at=self.clock())
                return ""
            await update(state="sending", delivery_kind="text")
            status = await send([{"type": "text", "data": {"text": candidates[side]}}])
            final = "fallback" if status == "success" else ("delivery_unknown" if status == "unknown" else "failed")
            await update(state=final, fallback_delivery=status, ended_at=self.clock())
            return candidates[side] if status == "success" else ""
        except asyncio.CancelledError:
            await update(state="delivery_unknown" if record["state"] == "sending" else "aborted", ended_at=self.clock())
            raise
        except Exception as exc:
            await failure_notice()
            await update(state="delivery_unknown" if record["state"] == "sending" else "failed", failure=type(exc).__name__, ended_at=self.clock())
            return ""

    async def note_context(self, record, written):
        await self.repo.update(record["bot_id"], record["user_id"], record["id"], context_written=bool(written))

    def _find(self, data, identifier, now, *, pending_only=False):
        self.repo.expire(data, now)
        identifier = identifier or data.get("active")
        record = next((r for r in data["records"] if r["id"] == identifier), None)
        if not record or record["state"] not in ("pending", "voted") or now >= record["expires_at"]:
            raise ValueError("评估不存在、未完整展示或已过期（仅限本人当前 Bot 的私聊记录）")
        if pending_only and record["state"] != "pending":
            raise ValueError("没有待投票评估")
        return record

    async def command(self, bot_id, event, args):
        if bot_id not in BOT_ALLOWLIST or not isinstance(event, PrivateMessageEvent):
            return "A/B 评估仅支持 bot_001、bot_009、bot_002 的私聊。"
        parts = " ".join(args).split()
        try:
            if len(parts) == 1 and parts[0] in VOTES:
                return await self.vote(bot_id, event.user_id, None, parts[0])
            if len(parts) == 3 and parts[0] == "vote" and parts[2] in VOTES:
                return await self.vote(bot_id, event.user_id, parts[1], parts[2])
            if len(parts) == 3 and parts[0] == "switch" and parts[2] in {"A", "B"}:
                return await self.switch(bot_id, event.user_id, parts[1], parts[2])
        except ValueError as exc:
            return str(exc)
        return "用法：/ab A|B|平局|都不好|跳过；/ab vote <id> A；/ab switch <id> A|B"

    async def vote(self, bot_id, user_id, identifier, choice):
        if choice not in VOTES:
            raise ValueError("无效投票")
        now = self.clock()
        def cast(data):
            record = self._find(data, identifier, now)
            if record["vote"] is None:
                record.update(vote=choice, voted_at=now, state="voted")
                if data.get("active") == record["id"]:
                    data["active"] = None
            return record
        record = await self.repo.transaction(bot_id, user_id, cast)
        return (f"已记录投票：{record['vote']}（重复提交不改票）。未切换人设，历史不变。\n"
                f"如需切换，请发送 /ab switch {record['id']} A 或 /ab switch {record['id']} B。")

    async def switch(self, bot_id, user_id, identifier, choice):
        if choice not in {"A", "B"}:
            raise ValueError("切换只接受 A 或 B")
        # Serialize switches separately; never hold the evaluation file lock while binding.
        async with _get_lock(str(self.repo.path(bot_id, user_id).absolute()) + ".switch"):
            record = await self.repo.transaction(bot_id, user_id, lambda data: self._find(data, identifier, self.clock()))
            snapshot = record["personas"][record["mapping"][choice]]
            await self.personas.bind_session(bot_id, str(user_id), record["session_id"], snapshot["id"],
                expected_generation=record["session_generation"], require_active=True,
                expected_content_hash=snapshot["hash"], deadline=record["expires_at"], clock=self.clock)
            await self.repo.update(bot_id, user_id, identifier, switch={"choice": choice, "at": self.clock(), "persona_id": snapshot["id"]})
        return f"已将原会话切换到 {choice} 对应人设，聊天历史保留，投票不变。"

    async def report(self, *, bot_id="", version_hash="", page=1, page_size=20, export=False):
        records = await self.repo.records(self.clock())
        records = [r for r in records if (not bot_id or r["bot_id"] == bot_id) and
                   (not version_hash or any(p["hash"] == version_hash for p in r["personas"].values()))]
        stats = {key: 0 for key in ("shown", "votes", "baseline_wins", "test_wins", "ties", "both_bad", "skipped", "expired", "failed")}
        for record in records:
            stats["shown"] += record.get("exposed_at") is not None
            vote = record.get("vote")
            if vote:
                stats["votes"] += vote != "跳过"
                if vote in ("A", "B"):
                    stats[record["mapping"][vote] + "_wins"] += 1
                else:
                    stats[{"平局": "ties", "都不好": "both_bad", "跳过": "skipped"}[vote]] += 1
            stats["expired"] += record["state"] == "expired"
            stats["failed"] += record["state"] in {"failed", "aborted", "delivery_unknown", "fallback"}
        stats["response_rate"] = stats["votes"] / stats["shown"] if stats["shown"] else 0
        start = (max(1, page) - 1) * max(1, min(100, page_size))
        return {"statistics": stats, "total": len(records), "page": page,
                "notice": "单轮同历史偏好，不代表长期实验。聊天备份不含评估数据；请独立 JSON 导出，首版不支持恢复导入。",
                "records": records if export else records[start:start + max(1, min(100, page_size))]}
