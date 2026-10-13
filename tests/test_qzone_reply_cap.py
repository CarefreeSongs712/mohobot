"""QQ 空间评论自动回复单帖上限回归 — 真实 Plugin.on_tick 入口。

覆盖 Issue #9 的第 4 项。

缺陷：``_monitor_own_comments`` 只在进入某条说说时检查一次 ``max_replies_per_post``，
循环内每回复一条只累加计数、不再复查上限。因此单次轮询若发现同帖多条新评论，
会一次性全部回复，突破配置上限并多消耗 LLM 调用。

只替换远程服务（假 Service）与时间(``asyncio.sleep`` 立即返回)；插件入口、开关判定、
首启基线、去重存储、上限统计都走真实实现。
"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name: str):
    """按插件目录形态加载 plugins/<name>/main.py。"""
    directory = ROOT / "plugins" / name
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location("audit_regression_" + name, directory / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _WS:
    """只记录 OneBot 调用，不做真实网络。"""

    def __init__(self, manager=None):
        self._bot_manager = manager
        self.sent = []

    async def send_group_msg(self, *args, **kw):
        self.sent.append(args)

    async def send_private_msg(self, *args, **kw):
        self.sent.append(args)


async def test_qzone_on_tick_cap_baseline_cumulative_and_unread_remainder():
    """默认关 / 首启基线 / 上限 1、2、0 / 跨轮累计 / 提高上限后消费剩余评论。"""
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.models.config import BotConfig

    module = _load("qzone")
    from qzone_core.model import Comment, Post

    for limit in (1, 2, 0):
        with tempfile.TemporaryDirectory() as td:
            manager = BotManager(td)
            manager._bots["bot_001"] = BotInstance("bot_001", None, BotConfig(bot_id="bot_001", qq=111))
            module.Plugin.inject_data_dir(td)
            module.Plugin.inject_ws_server(_WS(manager))
            module.Plugin.inject_llm_service(None)
            plugin = module.Plugin()
            calls = {"query": 0, "reply": 0}
            post = Post(uin=111, tid="post", comments=[])

            class Service:
                async def query_feeds(self, **kw):
                    calls["query"] += 1
                    return [post]

                async def reply_comment(self, target, idx, text):
                    calls["reply"] += 1

                async def comment_posts(self, target, text):
                    calls["reply"] += 1

            class Session:
                async def get_uin(self):
                    return 111

            plugin._get_api = lambda bot_id: (SimpleNamespace(session=Session()), Service())

            # 1) 默认关闭自动回复: 完全不查询。
            await plugin.on_tick()
            assert calls["query"] == 0, "auto replies default disabled"

            plugin.plugin_config = {"comment_reply_enabled": True, "max_replies_per_post": limit,
                                    "max_comment_age_sec": 0}
            # 2) 首次启用只建立基线, 不回历史评论。
            await plugin.on_tick()
            assert calls["reply"] == 0, "first enabled tick only baselines old comments"

            # 3) 同帖一次出现 3 条新评论: 回复数不得超过剩余额度。
            post.comments = [Comment(uin=201 + i, nickname="fixture", content="hi", create_time=0, tid=i + 1)
                             for i in range(3)]
            real_sleep = asyncio.sleep

            async def no_delay(seconds):
                await real_sleep(0)

            with patch.object(module.asyncio, "sleep", no_delay):
                await plugin.on_tick()
                expected = min(limit, 3) if limit else 3
                assert calls["reply"] == expected, (limit, calls)
                store = plugin._get_auto_store("bot_001")
                assert await store.post_reply_count("111:post") == expected

                # 4) 计数跨轮累计, 下一轮不重复回复。
                await plugin.on_tick()
                assert calls["reply"] == expected, "cap cumulative across ticks"

                # 5) 达到上限的评论保留未读: 提高上限后可继续消费。
                plugin.plugin_config["max_replies_per_post"] = 0
                await plugin.on_tick()
                assert calls["reply"] == 3


def main():
    asyncio.run(test_qzone_on_tick_cap_baseline_cumulative_and_unread_remainder())
    print("\nALL QZONE REPLY CAP TESTS PASSED")


if __name__ == "__main__":
    main()
