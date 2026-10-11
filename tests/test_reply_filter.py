"""回复产物过滤器回归 — 用生产普查的真实样本固化解析行为。

覆盖三类策略: TTS 变体(剥标签保内容并计入语音)、格式标签(剥标签保内容)、
工具语法/内部块(整块丢弃), 以及流式分片、扣留放行与误伤防护。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.utils.reply_filter import ReplyArtifactFilter, strip_reply_artifacts


def _stream(text: str, *sizes: int) -> tuple[str, str]:
    """按给定分片大小把 text 喂入流式过滤器, 返回 (display, tts)。"""
    f = ReplyArtifactFilter()
    out = []
    pos = 0
    sizes = list(sizes) or [3]
    i = 0
    while pos < len(text):
        n = sizes[i % len(sizes)]
        i += 1
        out.append(f.feed(text[pos:pos + n]))
        pos += n
    rest, tts = f.finish()
    return "".join(out) + rest, tts


# ── TTS 变体: 剥标签, 内容显示并计入语音 ─────────────────────

def test_voice_variants_batch():
    cases = [
        ("<tt>嗯，这话听着有点耳熟，</tt>后面", "嗯，这话听着有点耳熟，后面", "嗯，这话听着有点耳熟，"),
        ("</tt>所以结论还是买CT5，", "所以结论还是买CT5，", ""),  # 游离闭标签丢弃
        ("（tts）包在天依身上啦，", "包在天依身上啦，", "包在天依身上啦，"),
        ("明天稳坐好位置哦（tts）边疆座位要是真被抽中，", "明天稳坐好位置哦边疆座位要是真被抽中，", "边疆座位要是真被抽中，"),
        ("<is_tts>消消气消消气，</is_tts>", "消消气消消气，", "消消气消消气，"),
        ("<mts>你…！你还敢接梗！</mts>", "你…！你还敢接梗！", "你…！你还敢接梗！"),
        ("<welcome_tts>欸？！</welcome_tts>", "欸？！", "欸？！"),
        ("<tss>在的呀</tss><tgs>哦哦</tgs>", "在的呀哦哦", "在的呀\n哦哦"),
        ("<ttt>海陆空一条龙</ttt><tt>天依先抱个西瓜</tt>", "海陆空一条龙天依先抱个西瓜", "海陆空一条龙\n天依先抱个西瓜"),
    ]
    for raw, display, tts in cases:
        got_display, got_tts = strip_reply_artifacts(raw)
        assert got_display == display, f"{raw!r} display: {got_display!r}"
        assert got_tts == tts, f"{raw!r} tts: {got_tts!r}"


def test_voice_aliases_regress_old_behavior():
    # 既有别名与括号家族行为不变
    d, t = strip_reply_artifacts("<tts>歌来了</tts>")
    assert d == "歌来了" and t == "歌来了"
    d, t = strip_reply_artifacts("[tts]唱一段[/tts]好听吗")
    assert d == "唱一段好听吗" and t == "唱一段"
    d, t = strip_reply_artifacts("【/tts】前面")
    assert d == "前面" and t == ""  # 游离闭标签丢弃
    # 未闭合: 内容到结尾(容错忘写闭标签)
    d, t = strip_reply_artifacts("<tts>没有闭标签的朗读")
    assert d == "没有闭标签的朗读" and t == "没有闭标签的朗读"
    # 词边界安全: 别名的近似拼写不触发 voice 语义, 按未知标签剥壳保内容
    d, t = strip_reply_artifacts("<ttsx>甲</ttsx><tts2>乙</tts2>")
    assert d == "甲乙" and t == ""


# ── 格式标签: 剥标签保内容 ───────────────────────────────────

def test_format_tags_batch():
    cases = [
        ("天依知无不言，反正包子账本一直开着呢</text>哦，", "天依知无不言，反正包子账本一直开着呢哦，"),
        ("<text>嘿嘿，天依随口一说还挺有画面的吧", "嘿嘿，天依随口一说还挺有画面的吧"),
        ("<response>（推着购物车跑在前面，", "（推着购物车跑在前面，"),
        ("<caps>噗，依托答辩…", "噗，依托答辩…"),
        ("<b>欸？！</b>哈哈，不过没关系，", "欸？！哈哈，不过没关系，"),
        ('<p style="color:#66CCFF"></p>欸，', "欸，"),
        ("<snoring>午安呀，", "午安呀，"),
        ("<cough>嗷呜！阿绫你还来", "嗷呜！阿绫你还来"),
        ("<envelope><message>嗯，不许睡回笼觉啦</message></envelope>", "嗯，不许睡回笼觉啦"),
        ("<q>天依什么时候回来</q>这种链子就别转发了，", "天依什么时候回来这种链子就别转发了，"),
        ("（小声）</snoring>午安呀，", "（小声）午安呀，"),
        ("声音细细的）关…关掉啦，那人家也要说到做到哦</s>", "声音细细的）关…关掉啦，那人家也要说到做到哦"),
        ("<e>赢啦赢啦！十笼小笼包天依全收了，耶！</e>", "赢啦赢啦！十笼小笼包天依全收了，耶！"),
        ("<txt>欸？什么呀，天依盯了半天什么都没看到</txt>", "欸？什么呀，天依盯了半天什么都没看到"),
    ]
    for raw, display in cases:
        got, _ = strip_reply_artifacts(raw)
        assert got == display, f"{raw!r} -> {got!r}"


def test_unknown_tags_selfheal():
    # 尖括号未知纯标签按 format 自愈(剥标签保内容)
    d, _ = strip_reply_artifacts("<foobar>保留我</foobar>")
    assert d == "保留我"
    # 属性形态同样剥除
    d, _ = strip_reply_artifacts('<xyz data="1">内容</xyz>')
    assert d == "内容"


# ── 工具语法/内部块: 整块丢弃 ────────────────────────────────

def test_drop_blocks_batch():
    cases = [
        ("<tool_call><function=search_meme><description>一只猫</arg_key>"
         "<arg_value>挥拳</parameter></function></tool_call>哼，", "哼，"),
        ("<steal_meme></steal_meme>嗯？", "嗯？"),
        ("<think>用户说\"脸皮没有抱天依重要\"，</think>回复正文", "回复正文"),
        ("<thinking>让我想想怎么回应这个。</thinking>", ""),
        ("<consumed>这是一条已由系统投递并记忆的内部事件，</consumed>", ""),
        ("<emoji>2</emoji>", ""),
        ("<bbox>327 666 636 711</bbox>又来这张图？！", "又来这张图？！"),
        ("<ratio>0.95</ratio>包子热乎的就好这口，", "包子热乎的就好这口，"),
        ("<send_meme emoji_id=\"3\"></send_meme>哦", "哦"),
        ("<eot>", ""),
        # 未闭合 drop: 吞到结尾
        ("<tool_call>泄漏一半没闭合", ""),
        ("<think>想了一半", ""),
    ]
    for raw, display in cases:
        got, tts = strip_reply_artifacts(raw)
        assert got == display, f"{raw!r} -> {got!r}"
        assert tts == "", f"{raw!r} tts 应为空: {tts!r}"


def test_tool_block_salvage_on_malformed():
    # 畸形超长工具块: 超过救援上限后按正文放行, 不吞掉整条回复
    body = "x" * 2500
    got, _ = strip_reply_artifacts(f"<tool_call>{body}")
    assert got == body


# ── 误伤防护 ─────────────────────────────────────────────────

def test_no_false_positives():
    # 方括号未知内容(如 /查看聊天 转发格式)原样保留
    raw = "1. [assistant] 去吧去吧，我又拦不住。2. [summary] 全局概要"
    d, _ = strip_reply_artifacts(raw)
    assert d == raw
    # 小于号比较、数字表情等非标签文本
    for raw in ["a < b 且 c > d", "<3", "1<2<3", "看这个 <_<>"]:
        d, _ = strip_reply_artifacts(raw)
        assert d == raw, f"{raw!r} -> {d!r}"
    # 代码围栏等普通符号不受影响
    d, _ = strip_reply_artifacts("```python\nprint(1)\n```")
    assert "print(1)" in d


# ── 流式分片: 与批处理结果一致 ───────────────────────────────

def test_streaming_matches_batch():
    samples = [
        "<tt>嗯，这话听着有点耳熟，</tt>后面",
        "（tts）包在天依身上啦，",
        "<text>嘿嘿，天依随口一说</text>哦，",
        "<tool_call><function=search_meme><description>x</arg_value></function></tool_call>哼，",
        "<think>想着想着没闭合也无所谓",
        "<steal_meme></steal_meme>嗯？",
        "1. [assistant] 引用格式保留 <b>加粗</b> 完",
        "a < b 且 <caps>含糊…",
    ]
    for raw in samples:
        batch_d, batch_t = strip_reply_artifacts(raw)
        for sizes in ([2], [3], [1, 7, 2], [5]):
            sd, st = _stream(raw, *sizes)
            assert sd == batch_d, f"stream{sizes} {raw!r}: {sd!r} != {batch_d!r}"
            assert st == batch_t, f"stream{sizes} {raw!r}: {st!r} != {batch_t!r}"


def test_streaming_hold_release():
    # 半截标签跨 chunk: 扣留到判定为止
    f = ReplyArtifactFilter()
    out1 = f.feed("你好<tt")
    assert out1 == "你好"  # 半截标签扣留
    out2 = f.feed(">秘密</tt>啦")
    assert out2 == "秘密啦"
    rest, tts = f.finish()
    assert (out2 + rest) == "秘密啦" and tts == "秘密"
    # 判定失败按原文放行: <3 不是标签
    f2 = ReplyArtifactFilter()
    out = f2.feed("送你 <3")
    rest, _ = f2.finish()
    assert out + rest == "送你 <3"
