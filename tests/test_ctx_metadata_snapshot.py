"""issue #137: capture_session 快照复用与写回 generation 校验的一致性测试。

快照固定本轮选中的 id/generation，但不能作为跨 await 的读写凭据。
load/append 都必须重新校验 generation，避免删除重建同名会话后，旧请求
读到新会话数据或把在途回复写回。
"""

import asyncio
from pathlib import Path

from mohobot.context_manager import ContextManager

from contextlib import contextmanager


@contextmanager
def _patch_attr(obj, name, value):
    """临时替换属性(不依赖 pytest monkeypatch, 项目自研 runner 无 fixture)。"""
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)

BOT = "bot_001"
CHAT = "1001"


def make_cm(tmp_path: Path) -> ContextManager:
    return ContextManager(data_dir=str(tmp_path))


async def test_load_revalidates_snapshot_without_switching_to_new_active_session(tmp_path):
    cm = make_cm(tmp_path)
    await cm.append_context(BOT, "private", CHAT, [{"role": "user", "content": "hi"}])
    session = await cm.capture_session(BOT, "private", CHAT)

    await cm.create_session(BOT, "private", CHAT, "other active session")
    reads = []
    read_index = cm._read_index_unlocked

    async def counted_read(*args, **kwargs):
        reads.append(True)
        return await read_index(*args, **kwargs)

    with _patch_attr(cm, "_read_index_unlocked", counted_read):
        context = await cm.load_context(BOT, "private", CHAT, validated_session=session)
        assert context == [{"role": "user", "content": "hi"}]
        assert len(reads) == 1
        assert await cm.append_context(
            BOT, "private", CHAT, [{"role": "assistant", "content": "x"}],
            validated_session=session,
        )
        assert len(reads) == 2


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


async def test_snapshot_read_rejects_deleted_and_recreated_session(tmp_path):
    cm = make_cm(tmp_path)
    victim = await cm.create_session(BOT, "private", CHAT, "victim")
    captured = asyncio.Event()
    continue_read = asyncio.Event()
    snapshot = {}

    async def delayed_read():
        snapshot.update(await cm.capture_session(BOT, "private", CHAT))
        captured.set()
        await continue_read.wait()  # 主链在捕获后解析引用消息的 await 点。
        return await cm.load_context(BOT, "private", CHAT, validated_session=snapshot)

    reader = asyncio.create_task(delayed_read())
    try:
        await asyncio.wait_for(captured.wait(), 2)
        assert await cm.delete_session(BOT, "private", CHAT, victim)
        replacement = await cm.create_session(BOT, "private", CHAT, "replacement")
        assert replacement == victim
        fresh = await cm.capture_session(BOT, "private", CHAT)
        assert fresh["generation"] != snapshot["generation"]
        private_content = [{"role": "user", "content": "new-session-only"}]
        assert await cm.append_context(BOT, "private", CHAT, private_content,
                                       validated_session=fresh)
        continue_read.set()
        assert await asyncio.wait_for(reader, 2) == []
        assert await cm.load_context(BOT, "private", CHAT, validated_session=fresh) == private_content
    finally:
        continue_read.set()
        await asyncio.gather(reader, return_exceptions=True)


async def test_old_group_snapshot_cannot_read_new_generation(tmp_path):
    cm = make_cm(tmp_path)
    old = await cm.capture_session(BOT, "group", "789")
    await cm.clear_context(BOT, "group", "789")
    fresh = await cm.capture_session(BOT, "group", "789")
    assert await cm.append_context(BOT, "group", "789", [{"role": "user", "content": "new"}],
                                   validated_session=fresh)
    assert await cm.load_context(BOT, "group", "789", validated_session=old) == []


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
