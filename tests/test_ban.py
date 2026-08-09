"""封禁系统测试:
1. 时间解析 (1d/2h/30m/10s, 0=永久)
2. BanStore: 增删改查 / 过期清理 / 冗余清理(pass 覆盖 ban)
3. 优先级: pass > ban > pass-all > ban-all
4. 拦截器: 被禁用户静默拦截 / 管理员命令 / 非管理员拒绝 / banlist 公开
5. 会话 key: group / private
"""

import asyncio
import sys
import tempfile
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender


def group_event(text: str, user_id: int = 1001, group_id: int = 2001) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=0, self_id=1, post_type="message",
        message_type="group", message_id=1,
        user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def private_event(text: str, user_id: int = 3001) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=0, self_id=1, post_type="message",
        message_type="private", message_id=1,
        user_id=user_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


async def test_time_parse() -> None:
    from mohobot.ban.time_utils import time_format, timelast_format, timestr_to_int

    assert timestr_to_int("0") == 0
    assert timestr_to_int("1d") == 86400
    assert timestr_to_int("1d2h30m10s") == 86400 + 7200 + 1800 + 10
    assert timestr_to_int("30m") == 1800
    assert time_format("1d2h") == "1天2小时"
    assert time_format("0") == "永久"
    assert timelast_format(0) == "永久"
    assert timelast_format(90061) == "1天1小时1分钟1秒"
    assert timelast_format(-5) == "已过期"
    try:
        timestr_to_int("abc")
        assert False, "应抛 ValueError"
    except ValueError:
        pass
    print("[1] 时间解析 OK")


async def test_store_crud_and_expiry() -> None:
    from mohobot.ban.store import BanStore

    tmp = tempfile.mkdtemp(prefix="ban_")
    store = BanStore(data_dir=tmp)

    # 写入
    ok = await store.upsert("ban", "111", session_key="group:1", time_val=3600, reason="刷屏")
    assert ok
    ok = await store.upsert("ban-all", "222", time_val=0, reason="永久")
    assert ok

    banned, reason = await store.is_banned("group:1", "111")
    assert banned and reason == "刷屏"
    banned, _ = await store.is_banned("group:1", "222")
    assert banned  # 全局封禁在任意会话生效
    banned, _ = await store.is_banned("private:9", "222")
    assert banned  # 全局封禁在私聊也生效
    banned, _ = await store.is_banned("group:2", "111")
    assert not banned  # 会话封禁只在本会话生效

    # 数据持久化(新实例读取)
    store2 = BanStore(data_dir=tmp, cache_ttl=0)
    banned, _ = await store2.is_banned("group:1", "111")
    assert banned

    # 删除
    ok, err = await store.delete("ban", "111", session_key="group:1")
    assert ok and err is None
    banned, _ = await store.is_banned("group:1", "111")
    assert not banned

    # 过期清理: 写入 1 秒的, 睡 1.2s 后应自动失效
    await store.upsert("ban", "333", session_key="group:9", time_val=1)
    banned, _ = await store.is_banned("group:9", "333")
    assert banned
    await asyncio.sleep(1.2)
    banned, _ = await store.is_banned("group:9", "333")
    assert not banned, "过期记录应视为未禁"
    print("[2] 存储 CRUD + 过期清理 OK")


async def test_priority_and_redundant() -> None:
    """pass > ban > pass-all > ban-all; pass 覆盖 ban 冗余清理。"""
    from mohobot.ban.store import BanStore

    tmp = tempfile.mkdtemp(prefix="ban_prio_")
    store = BanStore(data_dir=tmp)

    # ban-all 生效
    await store.upsert("ban-all", "555", time_val=0)
    assert (await store.is_banned("group:1", "555"))[0] is True

    # pass-all 覆盖 ban-all
    await store.upsert("pass-all", "555", time_val=0)
    assert (await store.is_banned("group:1", "555"))[0] is False

    # 会话 ban 生效, 但会话 pass 覆盖它
    await store.upsert("ban", "666", session_key="group:1", time_val=0)
    assert (await store.is_banned("group:1", "666"))[0] is True
    await store.upsert("pass", "666", session_key="group:1", time_val=3600)
    assert (await store.is_banned("group:1", "666"))[0] is False
    # 会话级记录不跨会话: 其他会话没有该用户的 ban, 也不受该会话 pass 影响
    assert (await store.is_banned("group:2", "666"))[0] is False

    # pass-all 覆盖会话 ban(全局解禁 > 会话封禁? 不 — 会话 ban 优先于 pass-all)
    await store.upsert("ban", "777", session_key="group:1", time_val=0)
    await store.upsert("pass-all", "777", time_val=0)
    banned, _ = await store.is_banned("group:1", "777")
    assert banned, "会话封禁优先于全局解禁"

    # 冗余清理: 永久 pass 覆盖永久 ban → 清理后 ban 记录被移除
    await store.upsert("pass", "666", session_key="group:1", time_val=0)  # 改为永久解禁
    await store.clear_banned()
    data = await store.get_all()
    assert all("666" != r["uid"] for r in data["ban"].get("group:1", [])), \
        "被永久 pass 覆盖的 ban 应被清理"
    # 临时 pass 不清理永久 ban(过期后 ban 恢复)
    await store.upsert("ban", "888", session_key="group:1", time_val=0)
    await store.upsert("pass", "888", session_key="group:1", time_val=3600)
    await store.clear_banned()
    data = await store.get_all()
    assert any("888" == r["uid"] for r in data["ban"].get("group:1", [])), \
        "临时 pass 不应清理永久 ban"
    print("[3] 优先级 + 冗余清理 OK")


async def test_interceptor_commands() -> None:
    from mohobot.ban.ban_filter import BanInterceptor
    from mohobot.ban.store import BanStore

    tmp = tempfile.mkdtemp(prefix="ban_if_")
    store = BanStore(data_dir=tmp)
    filt = BanInterceptor(data_dir=tmp, enabled=True, admins=[1001], store=store)

    # 非管理员执行 /ban → 拒绝
    handled, reply = await filt.intercept("bot_001", group_event("/ban 222", user_id=9999), {})
    assert handled and "没有权限" in reply

    # 管理员 /ban → 成功
    handled, reply = await filt.intercept("bot_001", group_event("/ban 222 1h 刷屏", user_id=1001), {})
    assert handled and "已封禁 222" in reply and "1小时" in reply
    assert (await store.is_banned("group:2001", "222"))[0] is True

    # 被禁用户发消息 → 静默拦截
    handled, reply = await filt.intercept("bot_001", group_event("你好", user_id=222), {})
    assert handled and reply is None, "被禁用户消息应静默丢弃"

    # 被禁用户发命令 → 同样静默
    handled, reply = await filt.intercept("bot_001", group_event("/help", user_id=222), {})
    assert handled and reply is None

    # 管理员 /pass → 解禁后放行
    handled, reply = await filt.intercept("bot_001", group_event("/pass 222 1h", user_id=1001), {})
    assert handled and "已临时解禁" in reply
    handled, reply = await filt.intercept("bot_001", group_event("你好", user_id=222), {})
    assert handled is False, "解禁后消息应放行"

    # banlist 公开可查(非管理员)
    handled, reply = await filt.intercept("bot_001", group_event("/banlist", user_id=8888), {})
    assert handled and "封禁" in reply

    # 会话隔离: 其他群不受影响
    handled, reply = await filt.intercept("bot_001", group_event("你好", user_id=222, group_id=9999), {})
    assert handled is False
    print("[4] 拦截器命令 + 静默拦截 OK")


async def test_private_session_and_reset() -> None:
    from mohobot.ban.ban_filter import BanInterceptor
    from mohobot.ban.store import BanStore

    tmp = tempfile.mkdtemp(prefix="ban_priv_")
    store = BanStore(data_dir=tmp)
    filt = BanInterceptor(data_dir=tmp, enabled=True, admins=[1001], store=store)

    # 私聊封禁: session = private:3001
    handled, reply = await filt.intercept("bot_001", private_event("/ban 3001 1d", user_id=1001), {})
    assert handled and "已封禁 3001" in reply
    banned, _ = await store.is_banned("private:3001", "3001")
    assert banned
    handled, reply = await filt.intercept("bot_001", private_event("在吗", user_id=3001), {})
    assert handled and reply is None

    # ban-reset 清除全部记录
    await store.upsert("ban-all", "4000", time_val=0)
    handled, reply = await filt.intercept("bot_001", group_event("/ban-reset 4000", user_id=1001), {})
    assert handled and "已清除" in reply
    banned, _ = await store.is_banned("group:1", "4000")
    assert not banned
    print("[5] 私聊会话 + ban-reset OK")


async def main() -> None:
    await test_time_parse()
    await test_store_crud_and_expiry()
    await test_priority_and_redundant()
    await test_interceptor_commands()
    await test_private_session_and_reset()
    print("\nALL BAN TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
