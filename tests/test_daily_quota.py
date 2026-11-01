"""每日 LLM token 配额（群聊/私聊）回归测试。

注意:
- 根 runner (tests/_run_all.py) 在同一个事件循环里 await 协程测试函数，
  所以这里全部写成 ``async def``，不要用 ``asyncio.run``；
- 本项目 runner 不以 fixture 方式调用测试，故只用 tempfile 临时目录。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from mohobot.models.config import GlobalConfig, QuotaConfig
from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender
from mohobot.services.quota import DailyTokenQuota


def _cfg(enabled=True, group=700_000, private=150_000, admins=()) -> GlobalConfig:
    cfg = GlobalConfig()
    cfg.quota = QuotaConfig(
        enabled=enabled, group_daily_tokens=group, private_daily_tokens=private
    )
    cfg.admins = list(admins)
    return cfg


def _group(group_id=398870315, user_id=3001) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1, self_id=2774641554, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id, nickname="tester"),
        message=[{"type": "text", "data": {"text": "hi"}}],
    )


def _private(user_id=3001) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=1, self_id=2774641554, post_type="message", message_type="private",
        sub_type="friend", message_id=1, user_id=user_id,
        sender=Sender(user_id=user_id, nickname="tester"),
        message=[{"type": "text", "data": {"text": "hi"}}],
    )


def _rec(chat_type, chat_id, tokens, bot_id="bot_003", ts=None, module="chat") -> dict:
    return {
        "time": ts if ts is not None else time.time(),
        "bot_id": bot_id, "chat_type": chat_type, "chat_id": str(chat_id),
        "user_id": str(chat_id), "module": module, "kind": "stream",
        "model": "deepseek-chat", "prompt_tokens": tokens,
        "completion_tokens": 0, "total_tokens": tokens, "cached_tokens": 0,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


async def test_quota_config_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "global.yaml"
        _cfg(group=700_000, private=150_000).save(path)
        loaded = GlobalConfig.load(path)
        assert loaded.quota.enabled is True
        assert loaded.quota.group_daily_tokens == 700_000
        assert loaded.quota.private_daily_tokens == 150_000
        assert loaded.to_dict()["quota"] == {
            "enabled": True,
            "group_daily_tokens": 700_000,
            "private_daily_tokens": 150_000,
        }
        default = GlobalConfig.load(Path(td) / "missing.yaml")
        assert default.quota.enabled is False
        print("[1] 配置 roundtrip OK")


async def test_group_quota_blocks_after_limit():
    with tempfile.TemporaryDirectory() as td:
        usage = Path(td) / "llm_usage.jsonl"
        _write(usage, [_rec("group", 398870315, 400_000)])
        quota = DailyTokenQuota(_cfg(group=700_000), usage)
        g = _group()

        assert await quota.check("bot_003", g) is None
        _write(usage, [_rec("group", 398870315, 300_000)])
        notice = await quota.check("bot_003", g)
        assert notice is not None
        assert "已用 700,000" in notice and "上限 700,000" in notice
        print("[2] 群额度触顶提示 OK")


async def test_private_quota_is_per_bot_and_user():
    with tempfile.TemporaryDirectory() as td:
        usage = Path(td) / "llm_usage.jsonl"
        _write(usage, [
            _rec("private", 10001, 150_000, bot_id="bot_003"),
            _rec("private", 10001, 150_000, bot_id="bot_002"),
            _rec("private", 10002, 10_000, bot_id="bot_003"),
        ])
        quota = DailyTokenQuota(_cfg(private=150_000), usage)

        assert await quota.check("bot_003", _private(10001)) is not None
        assert await quota.check("bot_002", _private(10001)) is not None
        assert await quota.check("bot_003", _private(10002)) is None
        print("[3] 私聊按 (bot, 用户) 独立额度 OK")


async def test_admin_exempt_and_disabled():
    with tempfile.TemporaryDirectory() as td:
        usage = Path(td) / "llm_usage.jsonl"
        _write(usage, [_rec("group", 398870315, 10_000_000)])
        cfg = _cfg(admins=[2038427197])

        quota = DailyTokenQuota(cfg, usage)
        assert await quota.check("bot_003", _group(user_id=2038427197)) is None
        assert await quota.check("bot_003", _group(user_id=3001)) is not None
        cfg.quota.enabled = False
        assert await quota.check("bot_003", _group(user_id=3001)) is None
        print("[4] 管理员豁免 + 开关关闭 OK")


async def test_yesterday_usage_not_counted():
    with tempfile.TemporaryDirectory() as td:
        usage = Path(td) / "llm_usage.jsonl"
        _write(usage, [_rec("group", 398870315, 999_999, ts=time.time() - 3 * 86400)])
        quota = DailyTokenQuota(_cfg(group=700_000), usage)
        assert await quota.check("bot_003", _group()) is None
        print("[5] 跨天重置 OK")


async def test_incremental_append_is_picked_up():
    with tempfile.TemporaryDirectory() as td:
        usage = Path(td) / "llm_usage.jsonl"
        quota = DailyTokenQuota(_cfg(group=700_000), usage)
        g = _group()

        _write(usage, [_rec("group", 398870315, 100_000)])
        assert await quota.check("bot_003", g) is None
        _write(usage, [_rec("group", 398870315, 700_000)])
        assert await quota.check("bot_003", g) is not None
        assert quota.used_tokens(("group", "398870315")) == 800_000
        print("[6] 增量读取新增记录 OK")


async def test_usage_helper_returns_used_and_limit():
    with tempfile.TemporaryDirectory() as td:
        usage = Path(td) / "llm_usage.jsonl"
        _write(usage, [_rec("private", 10001, 42_000)])
        quota = DailyTokenQuota(_cfg(private=150_000), usage)
        used, limit = await quota.usage("bot_003", _private(10001))
        assert used == 42_000 and limit == 150_000
        print("[7] usage() 返回已用/上限 OK")


async def test_handler_helper_replies_notice():
    from mohobot.message_handler import MessageHandler

    with tempfile.TemporaryDirectory() as td:
        usage = Path(td) / "llm_usage.jsonl"
        _write(usage, [_rec("group", 398870315, 700_000)])
        handler = MessageHandler.__new__(MessageHandler)
        handler._quota = DailyTokenQuota(_cfg(group=700_000), usage)
        notice = await handler._quota_block_notice("bot_003", _group())
        assert notice and "今日额度已用完" in notice

        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        handler._quota = SimpleNamespace(check=_boom)
        assert await handler._quota_block_notice("bot_003", _group()) is None
        print("[8] handler 触顶 helper OK")
