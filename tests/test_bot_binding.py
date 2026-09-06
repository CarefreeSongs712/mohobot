"""Bot↔QQ 解耦单元测试:
1. legacy 迁移 (qq 目录 → 自动编号 bot_id)
2. register: 已绑定 QQ → bot 实例; 未绑定连接
3. create/bind/unbind: QQ 唯一绑定 + 连接升降级
4. unregister 竞态防护 (bound / unbound)
5. pick_bot_for_group: 同一条消息共享随机抽签结果
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.bot_manager import BotInstance, BotManager
from mohobot.models.config import BotConfig


class FakeWS:
    async def send(self, data): pass


async def test_legacy_migration() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mig_"))
    old_dir = tmp / "bots" / "123456789"
    old_dir.mkdir(parents=True)
    (old_dir / "config.json").write_text(
        json.dumps({"qq": 123456789, "nickname": "旧Bot", "persona": "旧人设", "enabled": True}),
        encoding="utf-8",
    )

    bm = BotManager(data_dir=str(tmp))
    n = bm.migrate_legacy_bots()
    assert n == 1, n
    cfg = bm.load_bot_config("bot_001")
    assert cfg.bot_id == "bot_001", cfg.bot_id
    assert cfg.qq == 123456789 and cfg.nickname == "旧Bot"
    assert not (old_dir).exists(), "旧目录应改名为 bot_id 目录"
    assert (tmp / "bots" / "bot_001" / "config.json").exists()
    # 再次迁移不重复
    assert bm.migrate_legacy_bots() == 0
    print("[1] legacy migration OK")


async def test_register_bound_and_unbound() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="reg_"))
    bm = BotManager(data_dir=str(tmp))
    bm.create_bot(nickname="绑定Bot", qq=777001)

    # 已绑定 QQ → bound 实例
    inst = bm.register(777001, FakeWS())
    assert inst.bound and inst.bot_id == "bot_001"
    assert bm.get("bot_001") is inst
    assert bm.get_by_qq(777001) is inst

    # 未绑定 QQ → unbound 连接
    inst2 = bm.register(888002, FakeWS())
    assert not inst2.bound and inst2.bot_id == ""
    assert len(bm.unbound_connections) == 1
    print("[2] register bound/unbound OK")


async def test_bind_unbind_lifecycle() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bind_"))
    bm = BotManager(data_dir=str(tmp))
    bm.create_bot(nickname="A", qq=0)          # bot_001 未绑定
    bm.create_bot(nickname="B", qq=0)          # bot_002 未绑定

    # 绑定 QQ → 连接晋升
    bm.register(555111, FakeWS())              # 未绑定连接
    assert len(bm.unbound_connections) == 1
    ok = bm.bind_qq("bot_001", 555111)
    assert ok
    assert bm.load_bot_config("bot_001").qq == 555111
    assert len(bm.unbound_connections) == 0, "连接应晋升为 bot 实例"
    assert bm.get("bot_001") is not None and bm.get("bot_001").bound

    # QQ 唯一绑定: bot_002 绑同一 QQ → bot_001 被解绑
    bm.bind_qq("bot_002", 555111)
    assert bm.load_bot_config("bot_001").qq == 0
    assert bm.load_bot_config("bot_002").qq == 555111
    assert bm.get("bot_002") is not None
    assert bm.get("bot_001") is None, "bot_001 的连接已降级/移除"

    # 解绑 → 连接降级为未绑定
    bm.unbind_qq("bot_002")
    assert bm.load_bot_config("bot_002").qq == 0
    assert bm.get("bot_002") is None
    assert len(bm.unbound_connections) == 1
    print("[3] bind/unbind lifecycle + QQ unique OK")


async def test_unregister_race() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ur_"))
    bm = BotManager(data_dir=str(tmp))
    bm.create_bot(nickname="R", qq=999001)

    old = bm.register(999001, FakeWS())
    # 新连接替换
    new = bm.register(999001, FakeWS())
    assert bm.get("bot_001") is new
    # 旧连接断开 → 不得误删新实例
    bm.unregister(old)
    assert bm.get("bot_001") is new
    # 当前实例断开 → 正常移除
    bm.unregister(new)
    assert bm.get("bot_001") is None

    # unbound 竞态同理
    u1 = bm.register(777777, FakeWS())
    u2 = bm.register(777777, FakeWS())
    bm.unregister(u1)
    assert len(bm.unbound_connections) == 1
    bm.unregister(u2)
    assert len(bm.unbound_connections) == 0
    print("[4] unregister race protection OK")


async def test_pick_bot_shared_per_message() -> None:
    """随机选中 bot: 同一条消息(同 group_id+message_id)多次调用共享同一抽签;
    不同消息独立抽取; 群内 bot 全部离线返回 None。"""
    tmp = Path(tempfile.mkdtemp(prefix="pb_"))
    bm = BotManager(data_dir=str(tmp))
    group_bots = {"bot_001", "bot_002", "bot_003"}
    for i in (1, 2, 3):
        bid = f"bot_{i:03d}"
        bm.create_bot(nickname=f"B{i}", qq=999100 + i)
        cfg = bm.load_bot_config(bid)
        bm._bots[bid] = BotInstance(bid, FakeWS(), cfg)
        bm.note_group_message(bid, 555)

    # 同一条消息: 多次调用(模拟多 bot 协程)结果一致, 且是群内 bot 之一
    first = bm.pick_bot_for_group(555, "msg_1")
    assert first in group_bots
    for _ in range(10):
        assert bm.pick_bot_for_group(555, "msg_1") == first
    # 不同消息: 抽样分布覆盖多个 bot(30 次至少命中 2 个)
    picks = {bm.pick_bot_for_group(555, f"msg_{n}") for n in range(2, 32)}
    assert len(picks) >= 2, f"随机抽样应覆盖多个 bot: {picks}"
    # 未传 message_id: 直接随机不缓存
    for _ in range(5):
        assert bm.pick_bot_for_group(555) in group_bots
    # 群内 bot 全部离线 → None
    for b in list(bm._bots):
        bm.forget_bot_groups(b)
    assert bm.pick_bot_for_group(555, "msg_1") is None
    print("[5] pick_bot_for_group per-message 共享抽签 OK")


async def main() -> None:
    await test_legacy_migration()
    await test_register_bound_and_unbound()
    await test_bind_unbind_lifecycle()
    await test_unregister_race()
    await test_pick_bot_shared_per_message()
    print("\nALL BOT BINDING TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
