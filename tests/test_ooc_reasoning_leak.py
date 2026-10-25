"""OOC 治理回归 — 无标签自查/推理块外泄 (Issue #57)。

生产事故: bot 把"检查系统约束 / 编写回复 / 字数检查 / 符合要求"当成正文输出,
且没包在 <think> 标签里, 标签过滤器抓不到, 分段逻辑把它拆成 9 个气泡直发用户。

覆盖两层:
1. reply_filter.strip_unwrapped_reasoning: 无标签推理块的识别与正文提取;
2. MessageHandler._stream_llm_reply: 流式阶段识别到自查块后降级单条发送,
   不得把内心戏逐段发给用户。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.utils.reply_filter import (  # noqa: E402
    looks_like_unwrapped_reasoning,
    strip_reply_artifacts,
    strip_unwrapped_reasoning,
)

# 用户截图中的真实 badcase(9 个气泡的完整文本)
BAD_CASE = """检查系统约束：
- 严格控制在50字以内
- 不使用列表、Markdown语法、动作描述（不能用括号动词！
）、emoji表情
- 结尾不加句号
- 像在QQ上聊天，口语化，不要刻意卖萌
- 情绪真实、
自然，熟人模式

编写回复：
欢迎回来，上班辛苦啦。
我水都给你烧好了，快坐下歇会儿，累不累

字数检查：
"欢迎回来，上班辛苦啦。我水都给你烧好了，快坐下歇会儿，累不累" -> 30字，小于50字，
结尾无句号，无动作描述，无emoji，无markdown。符合要求。

欢迎回来，上班辛苦啦。我热水都给你倒好了，快坐下歇会儿，今天累不累"""

EXPECTED = "欢迎回来，上班辛苦啦。我热水都给你倒好了，快坐下歇会儿，今天累不累"


# ── 1. 识别层 ────────────────────────────────────────────────

def test_detects_reasoning_prefix():
    assert looks_like_unwrapped_reasoning(BAD_CASE)
    assert looks_like_unwrapped_reasoning("检查系统约束：\n- 50字")
    assert looks_like_unwrapped_reasoning("思考过程：\n用户问好")
    assert looks_like_unwrapped_reasoning("字数检查：\n30字")


def test_normal_replies_are_not_flagged():
    # 冒号是必要条件: "检查系统约束很严格" 这种正常口语不得误判
    normals = [
        "欢迎回来，上班辛苦啦。我热水都给你倒好了，快坐下歇会儿，今天累不累",
        "检查系统约束很严格呢，不过天依觉得还好",
        "编写回复这个功能我还没用过",
        "字数检查是做什么的呀",
        "今天天气不错，出去走走。",
        "嗯嗯，我在听",
        "",
    ]
    for text in normals:
        assert not looks_like_unwrapped_reasoning(text), text
        assert strip_unwrapped_reasoning(text) == text, text


# ── 2. 清洗层 ────────────────────────────────────────────────

def test_badcase_extracts_final_reply():
    """真实 badcase: 只保留模型最后重新输出的那条正文。"""
    assert strip_unwrapped_reasoning(BAD_CASE) == EXPECTED


def test_variant_draft_only():
    """只有"编写回复"草稿、没有后置字数检查: 取草稿正文。"""
    text = "检查系统约束：\n- 50字以内\n- 不用括号\n\n编写回复：\n那就这样吧，明天见"
    assert strip_unwrapped_reasoning(text) == "那就这样吧，明天见"


def test_variant_draft_then_tail_check():
    """草稿后面跟字数检查但没有重新输出: 剥掉尾部检查块, 保留草稿。"""
    text = (
        "检查系统约束：\n- 50字\n\n"
        "编写回复：\n今天也要加油呀\n\n"
        "字数检查：\n14字，小于50字。符合要求。"
    )
    assert strip_unwrapped_reasoning(text) == "今天也要加油呀"


def test_variant_no_draft_marker():
    """没有草稿标记: 剔除开头自查块, 保留后续正文。"""
    text = "思考过程：\n用户在问好，应该回问候\n\n早上好呀，今天也要加油"
    assert strip_unwrapped_reasoning(text) == "早上好呀，今天也要加油"


def test_only_reasoning_returns_empty():
    """整条都是自查、没有任何正文: 返回空串(宁可这条不发, 也不发内心戏)。"""
    text = "检查系统约束：\n- 严格控制在50字以内\n- 不使用列表\n\n字数检查：\n符合要求。"
    assert strip_unwrapped_reasoning(text) == ""


def test_strip_reply_artifacts_integrates_reasoning_cleanup():
    """strip_reply_artifacts 是主链路入口: 标签剥除后必须继续清洗无标签自查块。"""
    display, tts = strip_reply_artifacts(BAD_CASE)
    assert display == EXPECTED, display
    assert tts == ""


def test_strip_reply_artifacts_keeps_tts_and_cleans_reasoning():
    """自查块 + TTS 标注混合时, 正文保留、朗读文本只含 tts 内容。"""
    text = "检查约束：\n- 简短\n\n编写回复：\n<tts>好的呀</tts>我们出发吧"
    display, tts = strip_reply_artifacts(text)
    assert display == "好的呀我们出发吧", display
    assert tts == "好的呀", tts


# ── 3. 流式集成层 ────────────────────────────────────────────

def _make_handler(sent: list, chunks: list):
    """构造只替换 LLM 流与发送边界的真实 MessageHandler。"""
    from mohobot.message_handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._ws = None
    handler._segment_reply = True
    handler._stream = True
    handler._reply_quote = False
    handler._seg_delay_min = 0
    handler._seg_delay_max = 0
    handler._seg_min_len = 12
    handler._seg_max_len = 60

    class _Llm:
        async def chat_stream(self, **kwargs):
            for chunk, is_final in chunks:
                yield chunk, is_final

    async def send_segment(bot_id, event, text, first_sent, *, quote=True):
        sent.append(text)
        return True

    handler._llm = _Llm()
    handler._send_segment = send_segment
    handler._submit_tts = lambda *args: None
    return handler


async def test_streaming_reasoning_leak_sends_single_cleaned_message():
    """流式输出自查块时: 只发 1 条清洗后的正文, 不得发成 9 个气泡。"""
    sent: list[str] = []
    chunks = [(BAD_CASE[i:i + 7], False) for i in range(0, len(BAD_CASE), 7)]
    chunks[-1] = (chunks[-1][0], True)
    handler = _make_handler(sent, chunks)

    result = await handler._stream_llm_reply("bot", None, [], {})

    assert len(sent) == 1, f"应只发一条, 实际 {len(sent)} 条: {sent}"
    assert sent[0] == EXPECTED, sent[0]
    assert result == EXPECTED


async def test_streaming_normal_reply_still_segments():
    """回归: 正常回复不受影响, 仍按分段配置拆分发送。"""
    sent: list[str] = []
    text = "第一句话说得足够长一些了吧。第二句话也说得足够长一些了吧。"
    chunks = [(text[i:i + 5], False) for i in range(0, len(text), 5)]
    chunks[-1] = (chunks[-1][0], True)
    handler = _make_handler(sent, chunks)

    await handler._stream_llm_reply("bot", None, [], {})

    assert len(sent) >= 2, f"正常回复应分段发送, 实际 {sent}"


async def test_non_streaming_path_cleans_reasoning():
    """非流式路径(strip_reply_artifacts)同样清洗, 不依赖流式门控。"""
    sent: list[str] = []
    chunks = [(BAD_CASE, True)]
    handler = _make_handler(sent, chunks)
    handler._segment_reply = False

    result = await handler._stream_llm_reply("bot", None, [], {})

    assert sent == [], "非分段路径走 _send_single_reply, 由 _send_message 发出"
    assert result == EXPECTED, result




# ── 4. 采样惩罚参数(复读/模板化治理) ────────────────────────

def test_penalties_absent_by_default():
    """默认 0.0 时不下发惩罚参数, 保持旧请求形状(部分网关不接受该参数)。"""
    from mohobot.llm_service import _optional_penalties
    from mohobot.models.config import LLMConfig

    assert _optional_penalties(LLMConfig()) == {}


def test_penalties_passed_when_configured():
    from mohobot.llm_service import _optional_penalties
    from mohobot.models.config import LLMConfig

    llm = LLMConfig(frequency_penalty=0.3, presence_penalty=0.1)
    assert _optional_penalties(llm) == {
        "frequency_penalty": 0.3, "presence_penalty": 0.1,
    }
    # 只配一个时只下发一个
    llm2 = LLMConfig(frequency_penalty=0.2)
    assert _optional_penalties(llm2) == {"frequency_penalty": 0.2}


def test_penalty_config_roundtrip(tmp_path=None):
    """配置读写往返: frequency/presence penalty 必须保存后能读回。"""
    import tempfile
    from pathlib import Path as _P

    from mohobot.models.config import GlobalConfig

    with tempfile.TemporaryDirectory() as td:
        cfg = GlobalConfig()
        cfg.llm.frequency_penalty = 0.35
        cfg.llm.presence_penalty = 0.15
        path = _P(td) / "config.yaml"
        cfg.save(path)
        loaded = GlobalConfig.load(path)
        assert loaded.llm.frequency_penalty == 0.35
        assert loaded.llm.presence_penalty == 0.15
        # to_dict 也要带上, 否则 WebUI 看不到
        data = cfg.to_dict()
        llm_dict = data["llm"] if "llm" in data else data
        assert llm_dict["frequency_penalty"] == 0.35
        assert llm_dict["presence_penalty"] == 0.15


async def test_chat_request_includes_penalties_when_configured():
    """真实 chat_stream 调用: 配置非 0 时请求必须带 penalty 参数。"""
    captured: dict = {}

    class _Stream:
        def __aiter__(self):
            async def gen():
                class _Choice:
                    delta = type("D", (), {"content": "好", "tool_calls": None})()
                    finish_reason = "stop"
                class _Chunk:
                    choices = [_Choice()]
                    usage = None
                yield _Chunk()
            return gen()

    class _Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Stream()

    from mohobot.llm_service import LLMService
    from mohobot.models.config import GlobalConfig

    svc = object.__new__(LLMService)
    cfg = GlobalConfig()
    cfg.llm.frequency_penalty = 0.4
    cfg.llm.presence_penalty = 0.2
    svc._cfg = cfg
    svc._available = True
    svc._vision_available = False
    svc._vision_client = None
    svc._image_cache = None
    svc._song_annotator = None
    svc._retired_clients = []
    svc._chat_client = type("C", (), {"chat": type("Ch", (), {"completions": _Completions()})()})()
    svc._current_tools_schemas = lambda: None

    async def _build(*a, **k):
        return [{"role": "system", "content": "p"}, {"role": "user", "content": "u"}]
    svc._build_messages = _build

    async def _record(*a, **k):
        return None
    svc._record_usage = _record

    async for _ in svc.chat_stream("bot", None, [], {}):
        pass

    assert captured.get("frequency_penalty") == 0.4, captured
    assert captured.get("presence_penalty") == 0.2, captured


async def main() -> None:
    import asyncio
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
        print(f"PASS {name}")
    print("\nALL OOC REASONING LEAK TESTS PASSED")




# ── 5. 中英混杂清理 (REQ-7) ──────────────────────────────────

def test_strip_english_fillers_basic():
    from mohobot.utils.reply_filter import strip_english_fillers

    cases = [
        ("Well, 我觉得今天天气不错", "我觉得今天天气不错"),
        ("Okay so 那个，明天见吧", "那个，明天见吧"),
        ("So, actually, 这个我也不太清楚", "这个我也不太清楚"),
        ("Well then, 我们走吧", "我们走吧"),
        ("As an AI language model, 我不能这样做", "我不能这样做"),
        ("I'm sorry, but 我不能帮你这个", "我不能帮你这个"),
    ]
    for raw, expected in cases:
        got = strip_english_fillers(raw)
        assert got == expected, f"{raw!r} -> {got!r} (want {expected!r})"


def test_strip_english_fillers_preserves_legit_english():
    """不误伤: 句中英文、专有名词、英文歌名/Vocaloid 术语都保留。"""
    from mohobot.utils.reply_filter import strip_english_fillers

    keep = [
        "我觉得 Well 这个词没问题",
        "Vocaloid 的新歌很好听",
        "这首歌 BPM 好高",
        "明天见",
        "OK，那就这样吧",          # 中文里正当使用的 OK, 不动
        "天依的英文名是 Luo Tianyi",
        "",
    ]
    for text in keep:
        assert strip_english_fillers(text) == text, text


def test_reply_config_exposes_filler_switch():
    from mohobot.models.config import GlobalConfig

    cfg = GlobalConfig()
    assert cfg.reply.strip_english_fillers is True, "默认开启(保守白名单)"
    cfg.reply.strip_english_fillers = False
    assert cfg.to_dict()["reply"]["strip_english_fillers"] is False


def _real_send_handler(sent: list[str]):
    """构造使用**真实 _send_segment** 的 handler(只替换最终发送边界)。"""
    from mohobot.message_handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._seg_delay_min = 0
    handler._seg_delay_max = 0
    handler._reply_quote = False
    handler._strip_english_fillers = True

    async def _send_message(bot_id, event, message, source="llm"):
        sent.append(message if isinstance(message, str) else str(message))
        return None

    handler._send_message = _send_message
    return handler


async def test_send_segment_strips_fillers():
    """发送口统一清理: 分段文本里的 "Well," 不得发给用户。"""
    sent: list[str] = []
    handler = _real_send_handler(sent)
    await handler._send_segment("bot", None, "Well, 我觉得今天天气不错", False)
    assert sent == ["我觉得今天天气不错"], sent


async def test_filler_cleanup_can_be_disabled():
    """开关关闭时不清理(保持原样)。"""
    sent: list[str] = []
    handler = _real_send_handler(sent)
    handler._strip_english_fillers = False
    await handler._send_segment("bot", None, "Well, 我觉得不错", False)
    assert sent == ["Well, 我觉得不错"], sent


async def test_send_segment_skips_when_only_filler():
    """整段都是英文填充词时, 清理后为空 → 不发送该段。"""
    sent: list[str] = []
    handler = _real_send_handler(sent)
    await handler._send_segment("bot", None, "Well, ", False)
    assert sent == [], sent


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())


# ── 6. OOC 三防(2026-10 生产样本驱动): 乱码符号/内心独白/整段英文漂移 ──
# 样本全部取自 2026-10-23~25 生产 history 的真实 badcase。

class _Ev:
    """最小事件替身: 只带 message(供英文漂移判定提取触发文本)。"""
    def __init__(self, text: str):
        self.message = [{"type": "text", "data": {"text": text}}]


def test_strip_exotic_glyphs_removes_garbage():
    from mohobot.utils.reply_filter import strip_exotic_glyphs

    cases = [  # (生产样本, 清理后)
        ("同款CT5别رخ被借，", "同款CT5别被借，"),          # 阿拉伯 10-24 bot_011
        ("بالمأنح有话直说", "有话直说"),                    # 阿拉伯 10-24 bot_006
        ("ค", ""),                                        # 泰文 10-23 bot_011
        ("biting my mic? Is this some kind of wolf pack joke?؋",
         "biting my mic? Is this some kind of wolf pack joke?"),  # 10-24 bot_002
    ]
    for raw, want in cases:
        got = strip_exotic_glyphs(raw)
        assert got == want, f"{raw!r} -> {got!r} (want {want!r})"


def test_strip_exotic_glyphs_preserves_legit_scripts():
    """希腊/西里尔是合法内容(俄语歌名等), 不在删除范围; 中文/emoji/英文保留。"""
    from mohobot.utils.reply_filter import strip_exotic_glyphs

    keep = [
        "Песня о тревожной молодости",   # 俄语歌名(歌曲库真实内容)
        "5 1 13 τιμώ",                    # 希腊字母(漂移但不删, 属另一类)
        "компози那种歌你还记得啊！",       # 句中夹俄语词不删(误伤风险大)
        "Golf GTI概念版四百多马力",
        "今天天气不错😊",
        "",
    ]
    for text in keep:
        assert strip_exotic_glyphs(text) == text, text


def test_looks_like_monologue_matches_production_samples():
    from mohobot.utils.reply_filter import looks_like_monologue

    hit = [
        'Wait, no — the user sent "（咬天钿）" pretending to bite 天钿',  # 10-24 bot_002
        "Wait, it's noisy because it wants to eat, right?",              # 10-23 bot_001
        "Wait,",                                                          # 10-24 bot_001
        "the user wants me to write a song",
        "user sent a sticker just now",
    ]
    for text in hit:
        assert looks_like_monologue(text), text
    miss = [
        "Wait a moment 我想想",        # wait+空格+词, 不是标号紧跟
        "等等，我想想",
        "今天天气不错",
        "waiting for you 这首歌很好听",  # 词中 wait 不匹配(有词首边界)
    ]
    for text in miss:
        assert not looks_like_monologue(text), text


def test_foreign_language_reply_detection():
    from mohobot.utils.reply_filter import is_foreign_language_reply

    zh_trigger = "帮我写一首歌吧"
    # 生产样本: bot_004 10-23 21:29 的整段英文
    hit = [
        "Since you asked me to write a song for you,",
        "but it is my own decision. Let me now decide on my own terms.",
        "I won't write a song for you at this time, and I won't write a",
    ]
    for seg in hit:
        assert is_foreign_language_reply(seg, zh_trigger), seg
    miss = [
        ("Golf GTI概念版四百多马力的前驱怪", zh_trigger),   # 大量汉字
        ("Model Y保有量大、", zh_trigger),
        # 拉丁已够多但汉字没被压到 1/4 以下(混排正常内容, 生产 bot_011 聊车样本)
        ("这首歌 BPM 很高，Never Gonna Give You Up 你听过吗？一起听听吧", zh_trigger),
        # 歌曲卡片模板(生产 bot_002 真实输出): 拉丁远超汉字, 只有模板豁免能放行
        ("1. Never Gonna Give You Up - Rick Astley 《Whenever You Need Somebody》",
         "放一下那首 never gonna give you up"),
        ("为您找到了 5 首歌曲 Never Gonna Give You Up", zh_trigger),  # 模板
        ("Since you asked me to write a song", "write me a song please"),  # 英文会话
        ("Since you asked me to write a song", ""),                     # 无触发文本不拦
        ("ok", zh_trigger),                                             # 太短
    ]
    for seg, trig in miss:
        assert not is_foreign_language_reply(seg, trig), seg


async def test_send_segment_ooc_guard_end_to_end():
    """发送口三防集成: 乱码删除、独白丢弃、英文漂移丢弃、开关可关。"""
    sent: list[str] = []
    handler = _real_send_handler(sent)
    ev = _Ev("（咬天钿）")

    await handler._send_segment("bot", ev, "同款CT5别رخ被借，", False)
    assert sent == ["同款CT5别被借，"], sent
    sent.clear()

    await handler._send_segment("bot", ev, "Wait, no — the user sent something pretending", False)
    assert sent == [], sent
    await handler._send_segment("bot", ev, "Since you asked me to write a song for you,", False)
    assert sent == [], sent
    # 正常中文照发
    await handler._send_segment("bot", ev, "那就这么定了，明天见！", False)
    assert sent == ["那就这么定了，明天见！"], sent
    sent.clear()

    # 英文触发消息下英文回复放行
    ev_en = _Ev("please write me a song in english")
    await handler._send_segment("bot", ev_en, "Since you asked, I will write it now ok", False)
    assert len(sent) == 1, sent
    sent.clear()

    # event=None(测试/无消息上下文): 不炸, 且英文漂移不拦(无触发语言可判)
    await handler._send_segment("bot", None, "Since you asked me to write a song now", False)
    assert len(sent) == 1, sent
    sent.clear()

    # 总开关关闭: 独白/乱码原样发出
    handler._ooc_guard = False
    await handler._send_segment("bot", ev, "Wait, no — raw passthrough ؋", False)
    assert sent == ["Wait, no — raw passthrough ؋"], sent


def test_reply_config_exposes_ooc_guard_switch():
    from mohobot.models.config import ReplyConfig
    import inspect

    assert "ooc_guard_enabled" in inspect.getsource(ReplyConfig)
    assert ReplyConfig().ooc_guard_enabled is True
