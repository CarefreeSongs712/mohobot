"""issue #137: capture_session 快照复用与写回 generation 校验的一致性测试。

一轮私聊 turn 内 capture_session/load_context/append_context 会各读一次
``session_index.json`` (sessions 越多越慢)。load/append 接受已校验快照后,
load 不再重复读索引; append 仍必须在写回前重新校验 generation, 否则清空/
重置/删除/恢复期间的在途回复会被写回, 破坏 issue #126 的语义。
"""

import asyncio
from pathlib import Path

import pytest

from mohobot.context_manager import ContextManager

BOT = "bot_001"
CHAT = "1001"


def make_cm(tmp_path: Path) -> ContextManager:
    return ContextManager(data_dir=str(tmp_path))


async def test_load_uses_validated_snapshot_without_reading_index(tmp_path, monkeypatch):
    cm = make_cm(tmp_path)
    await cm.append_context(BOT, "private", CHAT, [{"role": "user", "content": "hi"}])
    session = await cm.capture_session(BOT, "private", CHAT)

    async def _fail(*args, **kwargs):
        raise AssertionError("load_context 不应再读 session_index.json")

    monkeypatch.setattr(cm, "_read_index_unlocked", _fail)
    context = await cm.load_context(BOT, "private", CHAT, validated_session=session)
    assert context == [{"role": "user", "content": "hi"}]
    # 写回路径必须保留一次 generation 校验(仍读索引), 快照本身不能作为写入凭据。
    with pytest.raises(AssertionError, match="session_index"):
        await cm.append_context(
            BOT, "private", CHAT, [{"role": "assistant", "content": "x"}],
            validated_session=session,
        )


async def test_stale_snapshot_rejected_after_clear_and_reset(tmp_path):
    cm = make_cm(tmp_path)
    await cm.append_context(BOT, "private", CHAT, [{"role": "user", "content": "old"}])
    session = await cm.capture_session(BOT, "private", CHAT)

    await cm.clear_context(BOT, "private", CHAT)
    assert await cm.append_context(
        BOT, "private", CHAT, [{"role": "assistant", "content": "stale"}],
        validated_session=session,
    ) is False
    assert await cm.load_context(BOT, "private", CHAT, validated_session=session) == []

    fresh = await cm.capture_session(BOT, "private", CHAT)
    assert fresh["id"] == session["id"]
    assert fresh["generation"] != session["generation"]
    assert await cm.append_context(
        BOT, "private", CHAT, [{"role": "user", "content": "new"}],
        validated_session=fresh,
    ) is True

    # reset 同样推进 generation: 旧快照失效, 新快照可写。
    stale = await cm.capture_session(BOT, "private", CHAT)
    assert await cm.reset_session(BOT, "private", CHAT, stale["id"]) is True
    assert await cm.append_context(
        BOT, "private", CHAT, [{"role": "assistant", "content": "stale"}],
        validated_session=stale,
    ) is False
    assert await cm.load_context(BOT, "private", CHAT, validated_session=stale) == []


async def test_stale_snapshot_rejected_after_delete(tmp_path):
    cm = make_cm(tmp_path)
    victim_id = await cm.create_session(BOT, "private", CHAT, "victim")
    first = await cm.capture_session(BOT, "private", CHAT)
    assert first["id"] == victim_id
    assert await cm.append_context(
        BOT, "private", CHAT, [{"role": "user", "content": "old"}],
        validated_session=first,
    ) is True
    # 至少保留一个会话: 先建第二个再删第一个。
    await cm.create_session(BOT, "private", CHAT, "other")
    assert await cm.delete_session(BOT, "private", CHAT, first["id"]) is True
    assert await cm.append_context(
        BOT, "private", CHAT, [{"role": "assistant", "content": "late"}],
        validated_session=first,
    ) is False
    assert await cm.load_context(BOT, "private", CHAT, validated_session=first) == []


async def test_concurrent_clear_and_append_never_resurrects(tmp_path):
    cm = make_cm(tmp_path)
    await cm.append_context(BOT, "private", CHAT, [{"role": "user", "content": "old"}])
    session = await cm.capture_session(BOT, "private", CHAT)
    written, _ = await asyncio.gather(
        cm.append_context(
            BOT, "private", CHAT, [{"role": "assistant", "content": "in-flight"}],
            validated_session=session,
        ),
        cm.clear_context(BOT, "private", CHAT),
    )
    # append 先执行则随后被 clear 清空; clear 先执行则 append 被拒绝。
    # 两种顺序下最终都不能留下旧内容。
    assert await cm.load_context(BOT, "private", CHAT) == []
    assert written in (True, False)


async def test_group_snapshot_validated_and_invalidated_by_bump(tmp_path):
    cm = make_cm(tmp_path)
    group = await cm.capture_session(BOT, "group", "789")
    assert group["generation"] == "group-main"
    assert await cm.append_context(
        BOT, "group", "789", [{"role": "user", "content": "g"}],
        validated_session=group,
    ) is True
    assert await cm.load_context(BOT, "group", "789", validated_session=group) == [
        {"role": "user", "content": "g"}
    ]
    cm.bump_group_generations(BOT)
    assert await cm.append_context(
        BOT, "group", "789", [{"role": "assistant", "content": "stale"}],
        validated_session=group,
    ) is False
