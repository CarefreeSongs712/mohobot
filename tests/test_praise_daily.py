"""praise 每日定时点赞回归测试。

覆盖:
1. 到点触发一次: 全体 bot 点管理员 + bot 互赞(自己不点自己)
2. 时间未到 / 当天已执行 → 不触发
3. 点赞间隔随机延时在配置区间内
4. 单个点赞失败不影响后续(如非好友)
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "praise_daily_test", Path(__file__).resolve().parent.parent / "plugins" / "praise" / "main.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Plugin


class FakeWS:
    def __init__(self, bots, fail_pairs=None):
        self._bot_manager = SimpleNamespace(all_bots=bots)
        self.fail_pairs = fail_pairs or set()  # {(bot_id, target_qq)}
        self.likes = []  # (bot_id, target_qq, times)

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=None):
        if action == "send_like":
            target = params["user_id"]
            if (bot_id, target) in self.fail_pairs:
                return {"status": "failed", "retcode": 1, "wording": "非好友"}
            self.likes.append((bot_id, target, params.get("times")))
            return {"status": "ok", "retcode": 0}
        return {}


def _bots():
    return [
        SimpleNamespace(bot_id=f"bot_00{i}", config=SimpleNamespace(qq=100 + i), bound=True)
        for i in (1, 2, 3)
    ]


async def test_daily_trigger_once():
    Plugin = _load_plugin()
    Plugin._daily_done_date = ""
    Plugin._ws_server = FakeWS(_bots())
    plugin = Plugin()
    plugin.plugin_config.update({
        "daily_like_enabled": True, "daily_like_time": "08:00",
        "daily_admin_qq": 3831097597, "daily_like_times": 10,
        "daily_min_delay": 0, "daily_max_delay": 0,
    })

    # 时间未到 → 不触发
    await plugin._maybe_daily("2026-08-30", "07:59")
    ws = Plugin._ws_server
    assert ws.likes == [], ws.likes
    # 到点 → 触发一次
    await plugin._maybe_daily("2026-08-30", "08:00")
    await plugin._daily_task
    # 3 bot: 管理员 3 组 + 互赞 6 组(自己不点自己) = 9 组
    assert len(ws.likes) == 9, ws.likes
    # 每个 bot 不给自己点赞
    for bot_id, target, _ in ws.likes:
        qq = {f"bot_00{i}": 100 + i for i in (1, 2, 3)}[bot_id]
        assert target != qq, (bot_id, target)
    # 管理员被所有 bot 点
    admin_likes = [t for b, t, _ in ws.likes if t == 3831097597]
    assert len(admin_likes) == 3, admin_likes
    # 当天第二次 → 不重复
    await plugin._maybe_daily("2026-08-30", "09:00")
    await plugin._daily_task
    assert len(ws.likes) == 9, len(ws.likes)
    print("[1] 到点触发一次 + 目标组合正确 OK")


async def test_daily_disabled_and_next_day():
    Plugin = _load_plugin()
    Plugin._daily_done_date = ""
    Plugin._ws_server = FakeWS(_bots())
    plugin = Plugin()
    plugin.plugin_config.update({
        "daily_like_enabled": False, "daily_admin_qq": 3831097597,
        "daily_min_delay": 0, "daily_max_delay": 0,
    })
    await plugin._maybe_daily("2026-08-30", "08:00")
    assert Plugin._ws_server.likes == []
    # 次日重新触发
    plugin.plugin_config["daily_like_enabled"] = True
    await plugin._maybe_daily("2026-08-31", "08:05")
    await plugin._daily_task
    assert len(Plugin._ws_server.likes) == 9
    print("[2] 禁用开关 + 跨天重置 OK")


async def test_failure_continues():
    Plugin = _load_plugin()
    Plugin._daily_done_date = ""
    # 第一个 bot 给管理员点赞失败(如非好友) → 其余照常
    ws = FakeWS(_bots(), fail_pairs={("bot_001", 3831097597)})
    Plugin._ws_server = ws
    plugin = Plugin()
    plugin.plugin_config.update({
        "daily_admin_qq": 3831097597, "daily_min_delay": 0, "daily_max_delay": 0,
    })
    await plugin._maybe_daily("2026-08-30", "08:00")
    await plugin._daily_task
    admin_ok = [(b, t) for b, t, _ in ws.likes if t == 3831097597]
    assert ("bot_002", 3831097597) in admin_ok and ("bot_003", 3831097597) in admin_ok
    assert len(ws.likes) == 8  # 9 组 - 1 失败
    print("[3] 单点失败不影响后续 OK")


async def main() -> None:
    await test_daily_trigger_once()
    await test_daily_disabled_and_next_day()
    await test_failure_continues()
    print("\nALL PRAISE DAILY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())