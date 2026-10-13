"""回复产物过滤器嵌套回归 — DROP 优先级与尖括号误判。

覆盖两个缺陷（Issue #5）：

1. VOICE 态曾经把非 voice 标签连同正文一起按正文输出，绕过 DROP 策略，
   导致模型写成 ``<tts>正文<think>…</think><tool_call>…</tool_call></tts>``
   时思考/工具块会同时泄漏到显示文本与朗读文本。
2. 尖括号标签正则的属性段曾允许任意 ASCII，把 ``a < b && c > d`` 这类
   比较表达式误判为带属性标签并剥除，正文被改写。

全部输入为合成字符串，不访问网络与运行时数据；原 tests/test_reply_filter.py 未改动。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.utils.reply_filter import (  # noqa: E402
    VOICE_TAGS,
    ReplyArtifactFilter,
    strip_reply_artifacts,
)


def _split(raw, split):
    """把输入切成两片喂入, 返回 (显示文本, 朗读文本)。"""
    parser = ReplyArtifactFilter()
    out = parser.feed(raw[:split]) + parser.feed(raw[split:])
    rest, speech = parser.finish()
    return out + rest, speech


def _assert_all_splits(raw, expected):
    """全文、所有二段切分点、逐字符流式的结果必须与预期完全一致。"""
    assert strip_reply_artifacts(raw) == expected, (raw, strip_reply_artifacts(raw), expected)
    for split in range(len(raw) + 1):
        assert _split(raw, split) == expected, (raw, split, _split(raw, split), expected)
    parser = ReplyArtifactFilter()
    out = "".join(parser.feed(char) for char in raw)
    rest, speech = parser.finish()
    assert (out + rest, speech) == expected


def test_drop_dominates_every_voice_alias_and_bracket():
    """DROP 不受 VOICE 包裹影响: 各朗读别名 × 五种括号都不泄漏内部块。"""
    for alias in sorted(VOICE_TAGS):
        for left, right in (("<", ">"), ("[", "]"), ("【", "】"), ("(", ")"), ("（", "）")):
            raw = (f"{left}{alias}{right}可见<think>INTERNAL_REASON</think>"
                   "<tool_call>TOOL_SECRET</tool_call><b>正文</b>"
                   f"{left}/{alias}{right}后缀")
            _assert_all_splits(raw, ("可见正文后缀", "可见正文"))


def test_nested_drop_stack_returns_to_the_same_voice_span():
    """嵌套 DROP 逐层压栈, 仅在最外层闭合后恢复原 VOICE 片段。"""
    raw = "<tts>A<think>one<think>two</think>three<tool_call>four</tool_call></think>B</tts>C"
    _assert_all_splits(raw, ("ABC", "AB"))
    raw = "X<tts>A<tool_call><function=search_meme><parameter>secret</parameter></function></tool_call>B</tts>Y"
    _assert_all_splits(raw, ("XABY", "AB"))
    _assert_all_splits("<think><tts>internal</tts></think>visible", ("visible", ""))


def test_unclosed_drop_inside_voice_never_releases_internal_text():
    """voice 内未闭合的 DROP 一直吞到结尾, 不把内部文本放给显示或朗读。"""
    for tag in ("think", "reasoning", "tool_call", "function"):
        _assert_all_splits(f"<tts>prefix<{tag}>internal</tts>still internal", ("prefix", "prefix"))
    _assert_all_splits("<tts>prefix<think>internal", ("prefix", "prefix"))


def test_comparisons_and_non_attribute_ascii_stay_verbatim():
    """ASCII 比较表达式不是标签, 必须原样透传(此前被改成 "a  d")。"""
    for raw in ("a < b && c > d", "a < b || c > d", "a < b ? x : y > z", "a<b&&c>d", "a < foo bar > c"):
        _assert_all_splits(raw, (raw, ""))
    for raw in ("<tts>a < b && c > d</tts>", "[tts]a < b ? x : y > z[/tts]"):
        body = raw[5:-6]
        _assert_all_splits(raw, (body, body))


def test_quoted_attributes_and_legacy_drop_syntax_are_compatible():
    """带引号属性仍按标签处理; 旧工具产物 <function=name> 仍属于 DROP。"""
    _assert_all_splits('<tts><b style="color:red">A</b><think data="x">secret</think>B</tts>', ("AB", "AB"))
    _assert_all_splits("<function=search_meme>secret</function>visible", ("visible", ""))
    _assert_all_splits('<unknown data="1">visible</unknown>', ("visible", ""))
    _assert_all_splits("<unknown=value>ordinary</unknown=value>", ("<unknown=value>ordinary</unknown=value>", ""))


def main():
    """部分测试文件带 __main__ 入口的仓库惯例。"""
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
    print("\nALL REPLY FILTER NESTING TESTS PASSED")


if __name__ == "__main__":
    main()
