"""长引文(歌词/台词)分段保护回归测试。

背景: LLM 回复里引用歌词时, 按标点+长度切分会把引号内的一句歌词切碎,
如 "那就唱副歌吧：“乘破冰的船，/ 将涌动的爱收揽…" 应改为引文整体一段。

覆盖:
1. 完整回复: 引文整体成段, 引文前(冒号结尾)与引文后各自成段
2. 流式未闭合: 引文未收到右引号前绝不在引文中间切分
3. 短引语: 引号内长度不足阈值时走普通分段(不触发保护)
4. 引号前无边界: 引文与前面的文字黏在一起成段
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.message_handler import MessageHandler


def _handler(min_len: int = 12, max_len: int = 60) -> MessageHandler:
    h = MessageHandler.__new__(MessageHandler)  # 仅测试切分方法绑定
    h._seg_min_len = min_len
    h._seg_max_len = max_len
    return h


def test_full_reply_quote_isolation():
    h = _handler()
    text = (
        "欸，《越冰船》呀！天依超喜欢这首的。\n"
        "那就唱副歌吧：“乘破冰的船，\n"
        "将涌动的爱收揽，以热泪解封沉眠河水，\n"
        "风暴无法将我阻拦”…好听吗？"
    )
    flushed = h._flush_ready_segments(text)
    segments = flushed["segments"] + ([flushed["rest"]] if flushed["rest"].strip() else [])
    joined = "\n".join(s.strip() for s in segments)
    assert "乘破冰的船，\n将涌动的爱收揽" in segments[1] or any(
        "乘破冰的船" in s and "风暴无法将我阻拦" in s for s in segments
    ), segments  # 引文整体在一段内
    assert any("那就唱副歌吧" in s and "乘" not in s for s in segments), segments
    assert any("好听吗" in s for s in segments), segments
    # 引文不得跨段拆开: 不存在一段以 引文开头却无右引号 的段
    for s in segments:
        if s.count("\u201c") > s.count("\u201d"):
            raise AssertionError(f"引文被切断: {s!r}")
    print("[1] 完整回复引文整体成段 OK")


def test_streaming_unclosed_quote_hold():
    h = _handler()
    # 流式中途: 右引号未到 → 引文部分必须留在缓冲, 只允许引号前文字成段
    flushed = h._flush_ready_segments("那就唱副歌吧：“乘破冰的船，\n将涌动的爱收揽，")
    assert flushed["segments"] == ["那就唱副歌吧："], flushed
    assert flushed["rest"].startswith("“乘破冰的船"), flushed
    # 补上右引号与结尾 → 引文整体成段
    flushed2 = h._flush_ready_segments(flushed["rest"] + "风暴无法将我阻拦”…好听吗？")
    assert any(s.startswith("“乘破冰的船") and s.endswith("”") for s in flushed2["segments"]), flushed2
    print("[2] 流式未闭合引文保持完整 OK")


def test_short_quote_not_triggered():
    h = _handler()
    text = '她说“你好呀”然后潇洒地转身就离开了，留下我一个人在原地愣愣地站着不动。'
    flushed = h._flush_ready_segments(text)
    joined = "".join(flushed["segments"]) + flushed["rest"]
    assert joined == text, joined  # 短引语不触发保护, 内容不丢
    print("[3] 短引语走普通分段 OK")


def test_quote_glued_with_prefix():
    h = _handler()
    # 引号前文字无边界 → 与引文黏在一起成段(不在句中硬切)
    text = "我最近一直在单曲循环“乘破冰的船将涌动的爱收揽以热泪解封沉眠河水风暴无法将我阻拦”这首作品"
    flushed = h._flush_ready_segments(text)
    all_text = "".join(flushed["segments"]) + flushed["rest"]
    assert all_text == text, all_text
    # 引文不能与前后文字断开: 引文所在段应包含完整引文
    quote_seg = [s for s in flushed["segments"] if "乘破冰的船" in s]
    assert quote_seg and quote_seg[0].count("\u201c") == quote_seg[0].count("\u201d"), flushed
    print("[4] 引号前无边界黏合 OK")


async def main() -> None:
    test_full_reply_quote_isolation()
    test_streaming_unclosed_quote_hold()
    test_short_quote_not_triggered()
    test_quote_glued_with_prefix()
    print("\nALL QUOTE SEGMENT TESTS PASSED")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())