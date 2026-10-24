"""history 群聊合并布局测试 — 合并存储 / 写入去重 / bot_id 标注 / 单写 / 迁移脚本。

覆盖:
1. MessageHandler 群消息 → history/group/{群号}.jsonl(行内 bot_id), 私聊不变
2. 多 bot 收到同一条群消息(同 mid) → 合并文件只写一份
3. 群聊只写合并布局 → 旧布局 {bot_id}/group/ 不再产生新文件
4. WSServer bot 发言 → 群写合并文件 + bot_id, 私聊按 bot 分目录
5. 迁移脚本: 旧 per-bot 群文件去重合并 + bot_id 补全 + 时间排序 + 备份移动

运行: python tests/_run_all.py (从仓库根) / python tests/test_history_merged.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _group_raw(mid: int, group_id: int = 20002, user_id: int = 10001,
               text: str = "你好", self_id: int = 111) -> dict:
    return {
        "time": 1700000000 + mid, "self_id": self_id, "post_type": "message",
        "message_type": "group", "sub_type": "normal", "message_id": mid,
        "group_id": group_id, "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text, "font": 0,
        "sender": {"user_id": user_id, "nickname": "张三", "card": "张三"},
    }


def _private_raw(mid: int, user_id: int = 10001, self_id: int = 111) -> dict:
    return {
        "time": 1700000000 + mid, "self_id": self_id, "post_type": "message",
        "message_type": "private", "sub_type": "friend", "message_id": mid,
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": "私聊"}}],
        "raw_message": "私聊", "font": 0,
        "sender": {"user_id": user_id, "nickname": "张三"},
    }


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _make_handler(tmp: Path):
    """最小 MessageHandler: 只绑定 _archive_event 用到的属性。"""
    from mohobot.message_handler import MessageHandler
    from mohobot.models.config import GlobalConfig

    mh = MessageHandler.__new__(MessageHandler)
    mh._data_dir = str(tmp)
    cfg = GlobalConfig()
    mh._global_config = cfg
    mh._writer_registry = {}
    mh._group_mid_window = {}
    mh._group_fp_window = {}
    return mh


async def _close_writers(mh) -> None:
    """关闭归档 writer(Windows 下不关会锁住临时目录, 清理失败)。"""
    for writer in mh._writer_registry.values():
        await writer.close()
    mh._writer_registry.clear()


def _group_event(raw: dict):
    from mohobot.models.onebot import GroupMessageEvent
    return GroupMessageEvent.from_dict(raw)


def _private_event(raw: dict):
    from mohobot.models.onebot import PrivateMessageEvent
    return PrivateMessageEvent.from_dict(raw)


async def test_group_merged_archive_and_dedup():
    """群消息写合并文件(带 bot_id); 多 bot 同 mid 只写一份; 私聊不变。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp)

        await mh._archive_event("bot_001", _group_event(_group_raw(1)), _group_raw(1))
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert len(merged) == 1
        assert merged[0]["bot_id"] == "bot_001"
        assert merged[0]["message"][0]["data"]["text"] == "你好"
        legacy_path = tmp / "history" / "bot_001" / "group" / "20002.jsonl"
        assert not legacy_path.exists(), "旧 per-bot 布局不应再产生新文件"

        # 另一只 bot 收到同一条消息(同 mid, 不同 self_id) → 合并文件不重复
        raw2 = _group_raw(1, self_id=222)
        await mh._archive_event("bot_002", _group_event(raw2), raw2)
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert len(merged) == 1, "同 mid 的群消息不得重复归档"
        assert not (tmp / "history" / "bot_002" / "group" / "20002.jsonl").exists(), \
            "第二只 bot 也不应写旧布局"

        # 不同 mid → 正常写入
        await mh._archive_event("bot_001", _group_event(_group_raw(2)), _group_raw(2))
        assert len(_read_jsonl(tmp / "history" / "group" / "20002.jsonl")) == 2

        # 私聊仍按 bot 分目录
        praw = _private_raw(3)
        await mh._archive_event("bot_001", _private_event(praw), praw)
        assert len(_read_jsonl(tmp / "history" / "bot_001" / "private" / "10001.jsonl")) == 1
        assert not (tmp / "history" / "private").exists()
        await _close_writers(mh)
        print("[1] 群消息合并归档 + 同 mid 去重 + 私聊不变 OK")


async def test_dedup_window_eviction():
    """去重窗口容量有限: 超出窗口的 mid 再次出现会再写一次(近期窗口语义)。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp)
        mh._GROUP_MID_WINDOW = 2  # 缩小窗口便于测试
        for mid in (1, 2, 3):
            await mh._archive_event("bot_001", _group_event(_group_raw(mid)), _group_raw(mid))
        assert len(_read_jsonl(tmp / "history" / "group" / "20002.jsonl")) == 3
        # mid=1 已被逐出窗口 → 重复出现会再写
        await mh._archive_event("bot_001", _group_event(_group_raw(1)), _group_raw(1))
        assert len(_read_jsonl(tmp / "history" / "group" / "20002.jsonl")) == 4
        # mid=3 仍在窗口内 → 不重复
        await mh._archive_event("bot_001", _group_event(_group_raw(3)), _group_raw(3))
        assert len(_read_jsonl(tmp / "history" / "group" / "20002.jsonl")) == 4
        await _close_writers(mh)
        print("[3] 去重窗口逐出语义 OK")


async def test_cross_account_mid_dedup():
    """message_id 按账号分配时(NapCat): 同一消息多 bot 推送只写一行。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp)
        # 同一条用户消息: 同 user/time/内容, 不同 message_id 与 self_id
        for i, (mid, self_id) in enumerate(((101, 111), (202, 222), (303, 333))):
            raw = _group_raw(mid, user_id=10001, text="大家好", self_id=self_id)
            raw["time"] = 1700000500  # 同一秒
            await mh._archive_event(f"bot_00{i+1}", _group_event(raw), raw)
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert len(merged) == 1, f"跨账号同一消息应只归档一行: {len(merged)}"
        assert merged[0]["message_id"] == 101, "保留首见者"
        await _close_writers(mh)
        print("[6] 跨账号 message_id 不同 → 指纹去重 OK")


async def test_same_second_distinct_content_kept():
    """同一秒内同人不同内容/不同图片必须各自保留(指纹不能过度合并)。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp)
        base = _group_raw(1, user_id=10001, text="第一条")
        base["time"] = 1700000600
        second = _group_raw(2, user_id=10001, text="第二条")
        second["time"] = 1700000600
        for raw in (base, second):
            await mh._archive_event("bot_001", _group_event(raw), raw)
        # 同一秒两张不同图片
        img1 = _group_raw(3, user_id=10001, text="")
        img1["time"] = 1700000601
        img1["message"] = [{"type": "image", "data": {"file": "a.jpg"}}]
        img2 = _group_raw(4, user_id=10001, text="")
        img2["time"] = 1700000601
        img2["message"] = [{"type": "image", "data": {"file": "b.jpg"}}]
        for raw in (img1, img2):
            await mh._archive_event("bot_001", _group_event(raw), raw)
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert len(merged) == 4, f"不同内容不得合并: {len(merged)}"
        await _close_writers(mh)
        print("[7] 同秒不同内容/不同图片各自保留 OK")


async def test_bot_sent_inbound_copy_skipped():
    """发送侧登记后, 其他 bot 收到的同内容副本不再写 message 行。"""
    from mohobot.history_dedup import SentMessageClaims, signature_for_send

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp)
        claims = SentMessageClaims()
        mh._sent_claims = claims
        text = "今天也要开心呀"
        # 发送方登记(发送内容与入站副本的非文本段结构一致: 纯文本)
        claims.claim(20002, "111", signature_for_send("send_group_msg", {"message": text}))
        # 其他 bot 收到这条发言(不同 message_id)
        raw = _group_raw(777, user_id=111, text=text, self_id=222)
        raw["time"] = 1700000700
        await mh._archive_event("bot_002", _group_event(raw), raw)
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert merged == [], "已登记为我方发言的入站副本不应归档"

        # 未登记过的 bot 发言(旁路发送/旧数据)仍要保留
        raw2 = _group_raw(888, user_id=111, text="旁路发出的消息", self_id=222)
        raw2["time"] = 1700000701
        await mh._archive_event("bot_002", _group_event(raw2), raw2)
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert len(merged) == 1 and merged[0]["message_id"] == 888
        await _close_writers(mh)
        print("[8] bot 发言副本跳过 + 旁路消息保留 OK")


async def test_sent_claim_release_restores_inbound():
    """发送未发生(取消/被拦)时释放登记, 入站消息必须照常归档。"""
    from mohobot.history_dedup import SentMessageClaims, signature_for_send

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp)
        claims = SentMessageClaims()
        mh._sent_claims = claims
        text = "被拦截的发言"
        sig = signature_for_send("send_group_msg", {"message": text})
        claims.claim(20002, "111", sig)
        claims.release(20002, "111", sig)
        raw = _group_raw(999, user_id=111, text=text, self_id=222)
        raw["time"] = 1700000800
        await mh._archive_event("bot_002", _group_event(raw), raw)
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert len(merged) == 1, "释放后入站消息应正常归档"
        await _close_writers(mh)
        print("[9] 登记释放后入站归档恢复 OK")


def test_message_signature_shapes():
    """指纹对文本拆段方式不敏感, 对非文本段内容敏感(严格档)。"""
    from mohobot.history_dedup import message_signature

    one = [{"type": "text", "data": {"text": "你好"}},
           {"type": "text", "data": {"text": "世界"}}]
    two = [{"type": "text", "data": {"text": "你好世界"}}]
    assert message_signature(one) == message_signature(two), "文本拆段方式不应影响指纹"
    assert message_signature("你好世界") == message_signature(two), "纯文本与段列表应等价"

    # reply 段各账号 id 不同, 不参与指纹
    quoted = [{"type": "reply", "data": {"id": "1"}}] + two
    assert message_signature(quoted) == message_signature(two)

    img_a = [{"type": "image", "data": {"file": "a.jpg"}}]
    img_b = [{"type": "image", "data": {"file": "b.jpg"}}]
    assert message_signature(img_a) == message_signature(img_b), "宽松档只比类型"
    assert message_signature(img_a, strict=True) != message_signature(img_b, strict=True), \
        "严格档必须区分不同图片"
    print("[10] 消息指纹规则 OK")


async def test_ws_sent_claim_blocks_inbound_copy():
    """真实发送链路: 发送前登记指纹, 其他 bot 收到的副本不再归档。"""
    from mohobot.history_dedup import message_signature

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws = _make_ws(tmp, bot_qq=111)
        mh = _make_handler(tmp)
        mh._sent_claims = ws._sent_claims
        text = "这条会被别的 bot 收到"
        try:
            # _send_tracked 会先登记再进入发送队列(此处只有 echo future 等待,
            # 归档任务在超时后用 local id 兜底写入, 不影响 claim 断言)
            await ws._send_tracked(
                "bot_001", "send_group_msg",
                {"group_id": 20002, "message": text},
                "group", 20002, wait_response=False,
            )
            # 另一只 bot 收到同一句(不同 message_id / self_id)
            raw = _group_raw(4242, user_id=111, text=text, self_id=222)
            raw["time"] = 1700000900
            await mh._archive_event("bot_002", _group_event(raw), raw)
            # 发送归档是后台任务: 等 echo 超时(0.05s)后落盘
            await asyncio.sleep(0.3)
            merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
            inbound = [r for r in merged if r.get("post_type") == "message"]
            assert inbound == [], f"bot 发言的入站副本不应重复归档: {inbound}"
            # 发送方自己的 message_sent 仍在(等待 echo 超时后以 local id 兜底)
            sent = [r for r in merged if r.get("post_type") == "message_sent"]
            assert len(sent) == 1 and sent[0]["bot_id"] == "bot_001"
            # 宽松指纹能配对发送内容与入站副本
            assert message_signature(text) == message_signature(raw["message"])
        finally:
            await ws.stop()
            await _close_writers(mh)
        print("[11] 发送侧登记拦截入站副本 OK")


async def test_same_account_same_second_repeat_kept():
    """同一 bot 连接同一秒收到两条相同文本 → 是两条真实消息, 都要保留。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mh = _make_handler(tmp)
        for mid in (11, 12):
            raw = _group_raw(mid, user_id=10001, text="hello")
            raw["time"] = 1700000650
            await mh._archive_event("bot_001", _group_event(raw), raw)
        merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
        assert len(merged) == 2, f"同账号重复文本不得被合并: {len(merged)}"
        await _close_writers(mh)
        print("[12] 同账号同秒重复文本保留 OK")


def _make_ws(tmp: Path, bot_qq: int = 111):
    from mohobot.bot_manager import BotInstance, BotManager
    from mohobot.models.config import BotConfig
    from mohobot.ws_server import WSServer

    class _Sock:
        async def send(self, payload: str) -> None:
            pass

    bm = BotManager(data_dir=str(tmp))
    inst = BotInstance("bot_001", _Sock(),
                       BotConfig(bot_id="bot_001", qq=bot_qq, nickname="天依"))
    bm._bots["bot_001"] = inst
    ws = WSServer(bot_manager=bm, data_dir=str(tmp), global_config=type("C", (), {})())
    ws._ARCHIVE_ECHO_TIMEOUT = 0.05
    return ws


async def test_ws_sent_archive_merged():
    """WSServer bot 发言: 群只写合并文件(带 bot_id), 私聊按 bot 分目录。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws = _make_ws(tmp)
        try:
            await ws._write_archive_line("bot_001", "group", 20002, "555",
                                         [{"type": "text", "data": {"text": "回复"}}])
            merged = _read_jsonl(tmp / "history" / "group" / "20002.jsonl")
            assert len(merged) == 1
            assert merged[0]["post_type"] == "message_sent"
            assert merged[0]["bot_id"] == "bot_001"
            assert merged[0]["message_id"] == "555"
            assert not (tmp / "history" / "bot_001" / "group" / "20002.jsonl").exists(), \
                "旧 per-bot 布局不应再产生新文件"

            await ws._write_archive_line("bot_001", "private", 10001, "556",
                                         [{"type": "text", "data": {"text": "私回"}}])
            priv = _read_jsonl(tmp / "history" / "bot_001" / "private" / "10001.jsonl")
            assert len(priv) == 1 and priv[0]["bot_id"] == "bot_001"
            assert not (tmp / "history" / "private").exists()
            print("[4] WSServer bot 发言归档路径 OK")
        finally:
            await ws.stop()


def test_migrate_script():
    """迁移脚本: 多 bot 旧群文件去重合并、bot_id 补全、时间排序、旧文件移入备份。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data = tmp / "data"
        gid = 20002
        # bot_001 归档: 3 条(1 条与 bot_002 重复)
        rows_a = [
            {"time": 100, "post_type": "message", "message_id": 1,
             "user_id": 11, "message": [{"type": "text", "data": {"text": "甲"}}]},
            {"time": 200, "post_type": "message", "message_id": 2,
             "user_id": 11, "message": [{"type": "text", "data": {"text": "乙"}}]},
            {"time": 400, "post_type": "message", "message_id": 3,
             "user_id": 11, "message": [{"type": "text", "data": {"text": "重复"}}]},
        ]
        # bot_002 归档: 2 条(1 条与 bot_001 重复, 1 条独有)
        rows_b = [
            {"time": 400, "post_type": "message", "message_id": 3,
             "user_id": 11, "message": [{"type": "text", "data": {"text": "重复"}}]},
            {"time": 300, "post_type": "message_sent", "message_id": 9,
             "user_id": 222, "message": [{"type": "text", "data": {"text": "bot 发言"}}]},
        ]
        for bot, rows in (("bot_001", rows_a), ("bot_002", rows_b)):
            p = data / "history" / bot / "group" / f"{gid}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                         encoding="utf-8")
        # 私聊不动
        priv = data / "history" / "bot_001" / "private" / "10001.jsonl"
        priv.parent.mkdir(parents=True, exist_ok=True)
        priv.write_text('{"post_type": "message"}\n', encoding="utf-8")

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_history_layout",
            Path(__file__).resolve().parent.parent / "scripts" / "migrate_history_layout.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 用参数调用 main 的逻辑: 直接以 argv 方式跑
        sys_argv = sys.argv
        try:
            sys.argv = ["migrate", "--data-dir", str(data)]
            rc = mod.main()
        finally:
            sys.argv = sys_argv
        assert rc == 0

        target = data / "history" / "group" / f"{gid}.jsonl"
        merged = _read_jsonl(target)
        assert len(merged) == 4, f"重复 mid=3 应去重: {len(merged)}"
        assert [r["time"] for r in merged] == [100, 200, 300, 400], "应按时间排序"
        bots = {r["time"]: r["bot_id"] for r in merged}
        assert bots[100] == "bot_001" and bots[300] == "bot_002"
        assert bots[400] == "bot_001", "重复行保留先见者(bot_001)"
        # 旧文件移入备份, 私聊不动, group/ 空目录被清掉
        assert not (data / "history" / "bot_001" / "group").exists()
        assert not (data / "history" / "bot_002" / "group").exists()
        assert priv.exists()
        backups = list(data.glob("history_legacy_*"))
        assert len(backups) == 1
        assert (backups[0] / "bot_001" / "group" / f"{gid}.jsonl").exists()

        # 幂等: 再跑一次无事可做
        before = target.read_text(encoding="utf-8")
        sys.argv = ["migrate", "--data-dir", str(data)]
        try:
            assert mod.main() == 0
        finally:
            sys.argv = sys_argv
        assert target.read_text(encoding="utf-8") == before
        print("[5] 迁移脚本去重合并 + 备份 + 幂等 OK")


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                if asyncio.iscoroutinefunction(_fn):
                    asyncio.run(_fn())
                else:
                    _fn()
                print(f"PASS {_name}")
            except Exception as e:  # noqa: BLE001
                _failed += 1
                print(f"FAIL {_name}: {e}")
                import traceback
                traceback.print_exc()
    print("---")
    assert _failed == 0, f"{_failed} 个测试失败"
    print("ALL HISTORY MERGED TESTS PASSED")
