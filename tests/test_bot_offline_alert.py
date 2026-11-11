"""Bot 离线监控告警回归测试（不依赖真实 bot / WS）。

注意：根 runner 在运行中的 event loop 内调用测试，异步测试必须写成
``async def`` 并直接 await，不能使用 ``asyncio.run``。
"""

from __future__ import annotations

from mohobot.services.bot_watchdog import BotOfflineWatcher


class FakeCfg:
    def __init__(self, bot_id: str, qq: int, nickname: str = "", enabled: bool = True):
        self.bot_id = bot_id
        self.qq = qq
        self.nickname = nickname
        self.enabled = enabled


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_group_msg(self, bot_id: str, group_id: str, message: str) -> None:
        self.sent.append((bot_id, str(group_id), message))


class FakeBotManager:
    def __init__(self, configs: list[FakeCfg], online: set[str]):
        self._configs = configs
        self.online = set(online)

    def list_bot_configs(self) -> list[FakeCfg]:
        return self._configs

    def get(self, bot_id: str):
        if bot_id not in self.online:
            return None

        class _Inst:
            def __init__(self) -> None:
                self.bot_id = bot_id
                self.ws = object()

        return _Inst()

    @property
    def all_bots(self):
        out = []
        for bot_id in self.online:
            inst = self.get(bot_id)
            if inst is not None:
                out.append(inst)
        return out


def _watcher(mgr, ws, **kwargs) -> BotOfflineWatcher:
    return BotOfflineWatcher(mgr, ws, group_id=1070473353, threshold_sec=300, **kwargs)


async def test_alerts_after_threshold_once_then_recover() -> None:
    mgr = FakeBotManager(
        [FakeCfg("bot_001", 111, "天依"), FakeCfg("bot_002", 222, "乐正绫")],
        online={"bot_001"},
    )
    ws = FakeWS()
    w = _watcher(mgr, ws)

    # 第一轮只记录离线起点，不发告警
    assert await w.check_once(now=1000.0) == []
    assert ws.sent == []

    # 超过 300s：聚合告警，由在线 bot_001 发到管理群
    alerted = await w.check_once(now=1301.0)
    assert alerted == ["bot_002"]
    assert len(ws.sent) == 1
    sender, group_id, message = ws.sent[0]
    assert sender == "bot_001"
    assert group_id == "1070473353"
    assert "bot_002" in message and "乐正绫" in message

    # 同一轮离线不重复告警（避免每 30s 刷屏）
    assert await w.check_once(now=1400.0) == []
    assert len(ws.sent) == 1

    # 恢复在线后状态重置
    mgr.online.add("bot_002")
    assert await w.check_once(now=1500.0) == []

    # 再次离线：重新计时并在超过阈值后再次告警
    mgr.online.discard("bot_002")
    assert await w.check_once(now=1600.0) == []
    assert await w.check_once(now=1901.0) == ["bot_002"]
    assert len(ws.sent) == 2


async def test_no_online_sender_keeps_pending() -> None:
    mgr = FakeBotManager([FakeCfg("bot_002", 222)], online=set())
    ws = FakeWS()
    w = _watcher(mgr, ws)

    assert await w.check_once(now=1000.0) == []
    # 没有在线 bot 可发：不发送、不标记已告警
    assert await w.check_once(now=1301.0) == []
    assert ws.sent == []

    # 出现任意在线 bot 后，下一轮补发
    mgr.online.add("bot_009")
    assert await w.check_once(now=1310.0) == ["bot_002"]
    assert ws.sent[0][0] == "bot_009"


async def test_disabled_and_unbound_are_ignored() -> None:
    mgr = FakeBotManager(
        [
            FakeCfg("bot_001", 111),
            FakeCfg("bot_002", 0),                       # 未绑定 QQ
            FakeCfg("bot_003", 333, enabled=False),      # 已禁用
        ],
        online={"bot_001"},
    )
    ws = FakeWS()
    w = _watcher(mgr, ws)

    assert await w.check_once(now=1000.0) == []
    assert await w.check_once(now=1400.0) == []
    assert ws.sent == []


async def test_sender_never_uses_offline_bot() -> None:
    mgr = FakeBotManager(
        [
            FakeCfg("bot_002", 222),
            FakeCfg("bot_003", 333),
            FakeCfg("bot_005", 555),
        ],
        online={"bot_003", "bot_005"},
    )
    ws = FakeWS()
    w = _watcher(mgr, ws)

    await w.check_once(now=1000.0)
    alerted = await w.check_once(now=1305.0)
    assert alerted == ["bot_002"]
    assert ws.sent[0][0] in {"bot_003", "bot_005"}


async def test_group_id_hot_reload_from_provider() -> None:
    """管理群是 WebUI 可热改字段：每轮读取 provider，而不是启动时固定。"""
    mgr = FakeBotManager([FakeCfg("bot_002", 222), FakeCfg("bot_001", 111)], online={"bot_001"})
    ws = FakeWS()
    current = {"group": 0}
    w = BotOfflineWatcher(
        mgr, ws, group_id=lambda: current["group"], threshold_sec=300
    )

    assert await w.check_once(now=1000.0) == []
    # 群号为 0：跳过发送（不标记已告警）
    assert await w.check_once(now=1301.0) == []
    assert ws.sent == []

    # 热改为有效群号：下一轮补发
    current["group"] = 1070473353
    assert await w.check_once(now=1310.0) == ["bot_002"]
    assert ws.sent[0][1] == "1070473353"


async def test_aggregates_multiple_offline_into_one_message() -> None:
    mgr = FakeBotManager(
        [FakeCfg("bot_001", 111), FakeCfg("bot_002", 222), FakeCfg("bot_004", 444)],
        online={"bot_001"},
    )
    ws = FakeWS()
    w = _watcher(mgr, ws)

    await w.check_once(now=1000.0)
    alerted = await w.check_once(now=1305.0)
    assert alerted == ["bot_002", "bot_004"]
    assert len(ws.sent) == 1
    message = ws.sent[0][2]
    assert "bot_002" in message and "bot_004" in message
    assert message.count("已离线") == 2


async def test_send_failure_retries_next_round() -> None:
    class _FailingWS(FakeWS):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def send_group_msg(self, bot_id: str, group_id: str, message: str) -> None:
            if self.fail:
                raise RuntimeError("send failed")
            await super().send_group_msg(bot_id, group_id, message)

    mgr = FakeBotManager([FakeCfg("bot_001", 111), FakeCfg("bot_002", 222)], online={"bot_001"})
    ws = _FailingWS()
    w = _watcher(mgr, ws)

    await w.check_once(now=1000.0)
    # 发送失败：本轮不算已告警，下一轮继续尝试
    assert await w.check_once(now=1305.0) == []
    ws.fail = False
    assert await w.check_once(now=1310.0) == ["bot_002"]
    assert len(ws.sent) == 1


async def test_config_read_error_is_contained() -> None:
    class _BrokenMgr(FakeBotManager):
        def list_bot_configs(self):
            raise RuntimeError("config unreadable")

    w = BotOfflineWatcher(_BrokenMgr([], set()), FakeWS(), group_id=1070473353, threshold_sec=300)
    assert await w.check_once(now=1000.0) == []
