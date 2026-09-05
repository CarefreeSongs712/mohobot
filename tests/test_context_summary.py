"""上下文 AI 总结压缩测试:
1. 轮数计算: 普通消息 2 条=1 轮, 总结块=1 轮(ceil)
2. 满 40 轮触发: 裁剪最早 15 轮 → AI 总结块插入对话最前
3. 总结失败降级: 直接裁剪(不插块)
4. 嵌套总结: 总结块参与后续再总结(视为 1 轮)
5. 并发保护: 压缩期间头部被改则跳过
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.context_manager import ContextManager
from mohobot.file_store import json_read, json_write


def mk_round(i: int, base_ts: int | None = None) -> list[dict]:
    """第 i 轮对话: 一条用户 + 一条机器人回复。

    时间戳默认相对当前时间(递增), 避免被时间压缩按"旧对话"误判;
    需要制造旧对话时显式传 base_ts。
    """
    ts = base_ts if base_ts is not None else int(time.time()) + i
    return [
        {"role": "user", "content": f"问题{i}", "timestamp": ts},
        {"role": "assistant", "content": f"回答{i}", "timestamp": ts},
    ]


def count_rounds(ctx: list[dict]) -> int:
    import math
    return math.ceil(sum(1.0 if e.get("role") == "summary" else 0.5 for e in ctx))


class FakeSummarizer:
    """记录被总结的 entries, 返回固定文本。"""

    def __init__(self, text="<AI总结内容>"):
        self.text = text
        self.calls: list[list[dict]] = []

    async def __call__(self, entries):
        self.calls.append(list(entries))
        return self.text


# ── 1. 轮数计算 ─────────────────────────────────────────────

async def test_count_rounds():
    assert ContextManager._count_rounds(mk_round(0) + mk_round(1)) == 2
    assert ContextManager._count_rounds(mk_round(0)) == 1
    # 奇数条(半轮)向上取整
    assert ContextManager._count_rounds(mk_round(0)[:1]) == 1
    # 总结块 = 1 轮
    ctx = mk_round(0) + [{"role": "summary", "content": "s"}] + mk_round(1)
    assert ContextManager._count_rounds(ctx) == 3


# ── 2. 满 40 轮 → 裁剪 15 轮 + 总结块插入最前 ─────────────────

async def test_trim_40_remove_15_with_summary():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            summarizer = FakeSummarizer("1-15轮总结")
            mgr = ContextManager(
                data_dir=td, summarizer=summarizer,
                trim_at_rounds=40, trim_remove_rounds=15,
            )
            # 39 轮: 不触发
            for i in range(39):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i))
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            assert len(ctx) == 78 and count_rounds(ctx) == 39
            assert summarizer.calls == []  # 未触发总结

            # 第 40 轮: 触发压缩(39→40)
            await mgr.append_context("bot_001", "group", "g1", mk_round(39))
            ctx = await json_read(path)

            # 总结块在最前
            assert ctx[0]["role"] == "summary"
            assert ctx[0]["content"] == "1-15轮总结"
            # 15 轮(30 条)被总结移除, 剩余 25 轮(第 16-40 轮)
            assert count_rounds(ctx) == 25 + 1
            # 第 16 轮(索引 30,31 的 user 内容 "问题15")保留
            assert ctx[1]["content"] == "问题15"
            # 最后一条是第 40 轮
            assert ctx[-1]["content"] == "回答39"
            # 总结器收到最早的 15 轮(30 条)
            assert len(summarizer.calls) == 1
            head = summarizer.calls[0]
            assert len(head) == 30 and head[0]["content"] == "问题0"
            assert head[-1]["content"] == "回答14"
    await run()


# ── 3. 总结失败 → 直接裁剪 ─────────────────────────────────

async def test_summary_failure_falls_back_to_trim():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            async def broken(entries):
                raise RuntimeError("api down")
            mgr = ContextManager(
                data_dir=td, summarizer=broken,
                trim_at_rounds=4, trim_remove_rounds=2,
            )
            for i in range(4):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i))
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            assert all(e.get("role") != "summary" for e in ctx)
            assert len(ctx) == 4  # 2 轮(4 条)被直接裁剪, 剩第 3-4 轮
            assert ctx[0]["content"] == "问题2"
    await run()


# ── 4. 嵌套总结: 总结块参与再总结 ───────────────────────────

async def test_summary_block_joins_next_compaction():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            summarizer = FakeSummarizer("新总结")
            mgr = ContextManager(
                data_dir=td, summarizer=summarizer,
                trim_at_rounds=4, trim_remove_rounds=2,
            )
            # 4 轮 → 压缩1: 总结块+2轮
            for i in range(4):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i))
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            assert ctx[0]["role"] == "summary" and count_rounds(ctx) == 3

            # 再 1 轮(总计 3+1=4 ≥ 4) → 压缩2: 总结块(1轮)+第3轮(1轮) 被再总结
            await mgr.append_context("bot_001", "group", "g1", mk_round(4))
            ctx = await json_read(path)
            assert count_rounds(ctx) == 3  # 新总结块 + 第 5 轮 + 第 4 轮
            assert ctx[0]["role"] == "summary" and ctx[0]["content"] == "新总结"
            # 压缩2 保留了第 4-5 轮的 4 条(第 3 轮随旧总结块一起被移除)
            assert [e["content"] for e in ctx[1:]] == \
                ["问题3", "回答3", "问题4", "回答4"]
            # 总结器第二次调用收到: 旧总结块 + 第 3 轮(2 条)
            assert len(summarizer.calls) == 2
            second = summarizer.calls[1]
            assert second[0]["role"] == "summary" and second[0]["content"] == "新总结"
            assert [e["content"] for e in second[1:]] == ["问题2", "回答2"]
    await run()


# ── 5. 并发保护: 头部已被改则跳过 ───────────────────────────

async def test_compact_skips_if_head_changed():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            mgr = ContextManager(
                data_dir=td, summarizer=FakeSummarizer("s"),
                trim_at_rounds=4, trim_remove_rounds=2,
            )
            for i in range(4):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i))
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)

            # 模拟: 另一个协程在总结期间改写了文件(清空)
            await mgr.clear_context("bot_001", "group", "g1")
            # 用旧 context 触发 _compact → 头部不匹配 → 跳过, 不丢数据
            await mgr._compact(path, ctx)
            final = await json_read(path)
            assert final == []  # clear 的结果被保留
    await run()


# ── 6. 禁用总结: 只裁剪不调用 AI ───────────────────────────

async def test_summary_disabled_trims_only():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            summarizer = FakeSummarizer()
            mgr = ContextManager(
                data_dir=td, summarizer=summarizer, summary_enabled=False,
                trim_at_rounds=4, trim_remove_rounds=2,
            )
            for i in range(4):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i))
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            assert summarizer.calls == []      # 未调用 AI
            assert len(ctx) == 4               # 直接裁剪 2 轮
            assert ctx[0]["content"] == "问题2"
    await run()


# ── 7. 满轮触发顺带: 头部 = 最早 N 轮 ∪ 超龄旧对话(取更长前缀) ──

async def test_round_trigger_union_with_old_prefix():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            summarizer = FakeSummarizer()
            mgr = ContextManager(
                data_dir=td, summarizer=summarizer,
                trim_at_rounds=12, trim_remove_rounds=4,
            )
            now = int(time.time())
            old = now - 5 * 3600  # 5h 前(默认 age=3h → 旧对话)
            for i in range(6):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i, old + i))
            for i in range(6, 12):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i, now + i))
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            # 满 12 轮触发: 旧前缀(6 轮=12 条)比最早 4 轮(8 条)长 → 6 轮全被收走
            assert ctx[0]["role"] == "summary"
            assert [e["content"] for e in ctx[1:]] == [
                "问题6", "回答6", "问题7", "回答7", "问题8", "回答8",
                "问题9", "回答9", "问题10", "回答10", "问题11", "回答11",
            ]
            assert len(summarizer.calls) == 1
            head = summarizer.calls[0]
            assert len(head) == 12 and head[0]["content"] == "问题0"
            assert head[-1]["content"] == "回答5"
    await run()


# ── 8. 周期时间压缩 ─────────────────────────────────────────

async def test_sweep_first_compression_all_old():
    """从未压缩过的会话: 旧对话(>age) 全部被一次总结收走。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            summarizer = FakeSummarizer()
            mgr = ContextManager(data_dir=td, summarizer=summarizer)
            old = int(time.time()) - 4 * 3600  # 4h 前
            for i in range(5):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i, old + i))
            done = await mgr.sweep_context("bot_001", "group", "g1")
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            assert done is True
            assert len(summarizer.calls) == 1
            assert ctx[0]["role"] == "summary" and len(ctx) == 1
            assert len(summarizer.calls[0]) == 10  # 5 轮 = 10 条全部交给总结
    await run()


async def test_sweep_skips_when_nothing_old():
    """没有超过年龄的旧对话 → 跳过(不调用总结)。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            summarizer = FakeSummarizer()
            mgr = ContextManager(data_dir=td, summarizer=summarizer)
            for i in range(3):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i))
            done = await mgr.sweep_context("bot_001", "group", "g1")
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            assert done is False
            assert summarizer.calls == []
            assert len(ctx) == 6  # 数据原样
    await run()


async def test_sweep_day_gate_after_first_compression():
    """已压缩过的会话: 未超触发轮数且距上次压缩不足 1 天 → 跳过; 超 1 天 → 再压。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            summarizer = FakeSummarizer()
            mgr = ContextManager(data_dir=td, summarizer=summarizer)
            old = int(time.time()) - 30 * 3600  # 30h 前
            for i in range(3):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i, old + i))
            # 第一次: 从未压缩过 → 立即压
            assert await mgr.sweep_context("bot_001", "group", "g1") is True
            assert len(summarizer.calls) == 1

            # 又有 4h 前的新内容(距上次压缩不足 1 天, 总轮数 < 40) → day-gate 拦截
            older = int(time.time()) - 4 * 3600
            await mgr.append_context("bot_001", "group", "g1", mk_round(10, older))
            await mgr.append_context("bot_001", "group", "g1", mk_round(11, older + 1))
            assert await mgr.sweep_context("bot_001", "group", "g1") is False
            assert len(summarizer.calls) == 1  # 未触发总结

            # 回拨上次压缩时间戳到 25h 前(模拟已隔一天) → 再次压缩
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            ctx[0]["timestamp"] = int(time.time()) - 25 * 3600
            await json_write(path, ctx)
            assert await mgr.sweep_context("bot_001", "group", "g1") is True
            assert len(summarizer.calls) == 2
            # 第二次总结把旧总结块也一并再总结
            second_head = summarizer.calls[1]
            assert second_head[0]["role"] == "summary"
            assert [e["content"] for e in second_head[1:]] == ["问题10", "回答10", "问题11", "回答11"]
    await run()


async def test_sweep_summarize_failure_keeps_data():
    """周期压缩总结失败 → 跳过不裁剪(与满轮路径的直接裁剪不同), 数据保留。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            async def broken(entries):
                raise RuntimeError("api down")
            mgr = ContextManager(data_dir=td, summarizer=broken)
            old = int(time.time()) - 4 * 3600
            for i in range(3):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i, old + i))
            done = await mgr.sweep_context("bot_001", "group", "g1")
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            assert done is False
            assert len(ctx) == 6  # 数据原样保留
            assert all(e.get("role") != "summary" for e in ctx)
    await run()


async def test_sweep_all_sessions_scope():
    """周期扫描范围: 群聊 main + 私聊当前活动会话(非活动历史会话不动)。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            summarizer = FakeSummarizer()
            mgr = ContextManager(data_dir=td, summarizer=summarizer)
            old = int(time.time()) - 5 * 3600
            # 群聊 g1: 旧对话
            await mgr.append_context("bot_001", "group", "g1", mk_round(0, old))
            # 私聊 u1 默认会话 sess_main: 旧对话(非活动)
            await mgr.append_context("bot_001", "private", "u1", mk_round(1, old))
            # 私聊 u1 新建会话并切换为活动 → 旧对话
            await mgr.create_session("bot_001", "private", "u1", "历史")
            await mgr.append_context("bot_001", "private", "u1", mk_round(2, old))

            done = await mgr.sweep_all_sessions()
            assert done == 2  # 群 g1 + 私聊活动会话(sess_002)

            base = Path(td) / "contexts/bot_001"
            group_ctx = await json_read(base / "group/g1/main.json")
            assert group_ctx[0]["role"] == "summary"
            sess_main = await json_read(base / "private/u1/sess_main.json")
            assert all(e.get("role") != "summary" for e in sess_main)  # 非活动不动
            idx = await json_read(base / "private/u1/session_index.json")
            active = idx.get("active")
            assert active != "sess_main"  # 活动会话是新创建的
            active_ctx = await json_read(base / "private/u1" / f"{active}.json")
            assert active_ctx[0]["role"] == "summary"
    await run()


async def test_sweep_disabled_does_nothing():
    """关闭 AI 总结后周期扫描直接跳过(不压缩也不裁剪)。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            summarizer = FakeSummarizer()
            mgr = ContextManager(
                data_dir=td, summarizer=summarizer, summary_enabled=False,
            )
            old = int(time.time()) - 4 * 3600
            for i in range(3):
                await mgr.append_context("bot_001", "group", "g1", mk_round(i, old + i))
            done = await mgr.sweep_context("bot_001", "group", "g1")
            path = Path(td) / "contexts/bot_001/group/g1/main.json"
            ctx = await json_read(path)
            assert done is False
            assert summarizer.calls == []
            assert len(ctx) == 6
    await run()


if __name__ == "__main__":
    import traceback

    async def _main() -> int:
        failed = 0
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    await fn()
                    print(f"PASS {name}")
                except Exception:
                    failed += 1
                    print(f"FAIL {name}")
                    traceback.print_exc()
        return failed

    failed = asyncio.run(_main())
    total = len([n for n in globals() if n.startswith("test_") and callable(globals()[n])])
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
