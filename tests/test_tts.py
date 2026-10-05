"""TTS 语音(MiniMax t2a_v2)测试:
1. <tts> 标记过滤器: 剥标签/跨 chunk 撕裂/未闭合/超长截标点
2. MinimaxTTSClient: hex 解码/业务错误/HTTP 错误/voice_id 覆盖/计费字符
3. TTSService 队列: 满时丢最新
4. /tts 指令: 字数上限/冷却/管理员豁免/per-bot 开关
5. TTSConfig/BotConfig 配置 round-trip
"""

import asyncio
import sys
import tempfile
import json
import binascii
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.config import BotConfig, GlobalConfig, TTSConfig
from mohobot.models.onebot import GroupMessageEvent, Sender
from mohobot.utils.tts_marker import (
    TTSMarkerFilter,
    strip_and_extract,
)


# ── 1. 标记过滤器 ─────────────────────────────────────────────


def test_filter_no_tags_passthrough() -> None:
    f = TTSMarkerFilter()
    out = f.feed("今天天气")
    out += f.feed("真好。")
    rest, tts = f.finish()
    assert out + rest == "今天天气真好。"
    assert tts == ""


def test_filter_closed_tag_stripped_content_shown() -> None:
    f = TTSMarkerFilter()
    display = f.feed("你好呀。<tts>今天真开心</tts>明天见。")
    rest, tts = f.finish()
    full = display + rest
    assert "<tts>" not in full and "</tts>" not in full
    assert full == "你好呀。今天真开心明天见。"
    assert tts == "今天真开心"


def test_filter_tag_split_across_chunks() -> None:
    """标签被流式 chunk 撕裂: 半截标签不得泄漏进显示文本。"""
    f = TTSMarkerFilter()
    chunks = ["前文。", "<t", "ts>", "要读的", "话</t", "ts>后文"]
    collected = ""
    for c in chunks:
        collected += f.feed(c)
    rest, tts = f.finish()
    full = (collected + rest).replace("\n", "")
    assert "<" not in full.replace("好", "")  # 无残留半截标签
    assert full == "前文。要读的话后文"
    assert tts == "要读的话"


def test_filter_unclosed_tag() -> None:
    """忘写闭标签: 内容仍显示, 且计入朗读文本。"""
    f = TTSMarkerFilter()
    display = f.feed("开头。<tts>忘记闭合的话")
    rest, tts = f.finish()
    full = display + rest
    assert "<tts>" not in full
    assert full == "开头。忘记闭合的话"
    assert tts == "忘记闭合的话"


def test_filter_multiple_spans_joined() -> None:
    """多段标注按出现顺序合并为一条朗读文本。"""
    f = TTSMarkerFilter()
    f.feed("<tts>第一句</tts>中间<tts>第二句</tts>")
    _, tts = f.finish()
    assert tts == "第一句\n第二句"


def test_no_length_limit() -> None:
    """标注不限字数: 长内容完整保留不截断。"""
    long_text = "这是一段特别长的标注内容，早就超过了原来二十字的限制，但现在是完整朗读的，甚至可以把整条回复都标注上。"
    display, tts = strip_and_extract(f"前<tts>{long_text}</tts>后")
    assert display == f"前{long_text}后"
    assert tts == long_text


def test_strip_and_extract() -> None:
    # 标注内容仍显示(只剥标签), 朗读文本取标注
    display, tts = strip_and_extract("A<tts>朗读</tts>B")
    assert display == "A朗读B"
    assert tts == "朗读"
    display, tts = strip_and_extract("无标注")
    assert display == "无标注" and tts == ""


# ── 2. MinimaxTTSClient ──────────────────────────────────────


def _ok_response(audio: bytes, chars: int = 12) -> dict:
    return {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "data": {"audio": binascii.hexlify(audio).decode()},
        "extra_info": {"usage_characters": chars},
    }


async def test_minimax_client_ok() -> None:
    import httpx
    from mohobot.services.minimax_tts import MinimaxTTSClient

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization", "")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_response(b"ID3fake_mp3", chars=7))

    factory = lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=kw.get("timeout", 5)
    )
    c = MinimaxTTSClient(
        "https://api.minimax.cn", "sk-test-key", model="speech-2.8-hd",
        voice_id="lty01", speed=1.2, http_client_factory=factory,
    )
    audio, chars = await c.synthesize("你好")
    assert audio == b"ID3fake_mp3"
    assert chars == 7
    assert captured["path"] == "/v1/t2a_v2"
    assert captured["auth"] == "Bearer sk-test-key"
    assert captured["body"]["voice_setting"]["voice_id"] == "lty01"
    assert abs(captured["body"]["voice_setting"]["speed"] - 1.2) < 1e-9
    assert captured["body"]["audio_setting"]["format"] == "mp3"
    assert captured["body"]["stream"] is False
    await c.close()


async def test_minimax_client_business_error_and_http_error() -> None:
    import httpx
    from mohobot.services.minimax_tts import MinimaxTTSClient

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "坏" in body.get("text", ""):
            return httpx.Response(200, json={
                "base_resp": {"status_code": 1004, "status_msg": "invalid params"},
            })
        if "拒" in body.get("text", ""):
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, json=_ok_response(b"x"))

    factory = lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=kw.get("timeout", 5)
    )
    c = MinimaxTTSClient(
        "https://api.minimax.cn", "sk-k", voice_id="v1", http_client_factory=factory,
    )
    audio, chars = await c.synthesize("坏文本")
    assert audio is None and chars == 0
    audio, chars = await c.synthesize("拒绝文本")
    assert audio is None and chars == 0
    await c.close()


async def test_minimax_client_voice_id_override() -> None:
    """请求级 voice_id(per-bot)覆盖全局默认。"""
    import httpx
    from mohobot.services.minimax_tts import MinimaxTTSClient

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_response(b"x"))

    factory = lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=kw.get("timeout", 5)
    )
    c = MinimaxTTSClient(
        "https://api.minimax.cn", "sk-k", voice_id="global_voice",
        http_client_factory=factory,
    )
    await c.synthesize("A")                      # 无覆盖 → 全局
    assert captured["body"]["voice_setting"]["voice_id"] == "global_voice"
    await c.synthesize("B", voice_id="bot002_voice")  # per-bot 覆盖
    assert captured["body"]["voice_setting"]["voice_id"] == "bot002_voice"
    await c.close()


def test_minimax_client_configured() -> None:
    from mohobot.services.minimax_tts import MinimaxTTSClient
    assert MinimaxTTSClient("https://api.minimax.cn", "k", voice_id="v").configured
    assert not MinimaxTTSClient("https://api.minimax.cn", "", voice_id="v").configured
    assert not MinimaxTTSClient("https://api.minimax.cn", "k", voice_id="").configured


# ── 3. TTSService 队列(丢最新) ────────────────────────────────


def test_tts_queue_drop_newest() -> None:
    from mohobot.services.tts import TTSJob, TTSService

    cfg = TTSConfig(enabled=True, queue_maxsize=2)
    svc = TTSService(cfg)  # 不 start worker, 只测队列

    assert svc.submit(TTSJob("b", "group", "1", "第一条")) is True
    assert svc.submit(TTSJob("b", "group", "1", "第二条")) is True
    # 队列满 → 丢最新
    assert svc.submit(TTSJob("b", "group", "1", "第三条")) is False
    assert svc.queued == 2
    assert svc.stats["dropped"] == 1


# ── 4. /tts 指令 ──────────────────────────────────────────────


class FakeBotManager:
    def __init__(self, config: BotConfig):
        self._config = config

    def get(self, bot_id):
        return SimpleNamespace(config=self._config)


class FakeWS:
    def __init__(self, config: BotConfig):
        self._bot_manager = FakeBotManager(config)


def _make_event(user_id: int = 10001, group_id: int = 20001) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=0, self_id=1, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=group_id,
        message=[{"type": "text", "data": {"text": "x"}}],
        sender=Sender(user_id=user_id, nickname="测试"),
    )


def _make_handler(bot_cfg: BotConfig, tts_cfg: TTSConfig, admins=None):
    from mohobot.interceptors.command_handler import CommandHandler
    from mohobot.services.tts import TTSService

    ws = FakeWS(bot_cfg)
    svc = TTSService(tts_cfg)  # 不 start worker
    handler = CommandHandler(
        context_manager=None, llm_service=None, ws_server=ws,
        tts_service=svc, admins=admins or [],
    )
    return handler, svc


async def test_cmd_tts_guards_and_limits() -> None:
    cfg = GlobalConfig()
    cfg.tts = TTSConfig(enabled=True, cmd_max_chars=30, cmd_cooldown=120)
    bot_cfg = BotConfig(bot_id="bot_001", tts_enabled=True)
    handler, svc = _make_handler(bot_cfg, cfg.tts)

    # 全局开关关 → 未开启
    cfg.tts.enabled = False
    reply = await handler._cmd_tts("bot_001", _make_event(), ["你好"])
    assert reply is not None and "未开启" in reply
    cfg.tts.enabled = True

    # per-bot 开关关 → 未开启
    bot_cfg.tts_enabled = False
    reply = await handler._cmd_tts("bot_001", _make_event(), ["你好"])
    assert reply is not None and "未开启" in reply
    bot_cfg.tts_enabled = True

    # 空文本 → 用法
    reply = await handler._cmd_tts("bot_001", _make_event(), [])
    assert reply is not None and "用法" in reply

    # 非管理员超长 → 拒绝
    reply = await handler._cmd_tts("bot_001", _make_event(), ["字" * 31])
    assert reply is not None and "30" in reply

    # 非管理员正常 → 入队成功, 无回复(语音异步到)
    reply = await handler._cmd_tts("bot_001", _make_event(), ["你好呀"])
    assert reply is None
    assert svc.queued == 1

    # 冷却期内 → 提示
    reply = await handler._cmd_tts("bot_001", _make_event(), ["再来一次"])
    assert reply is not None and "冷却" in reply


async def test_cmd_tts_admin_bypass() -> None:
    cfg = GlobalConfig()
    cfg.tts = TTSConfig(enabled=True, cmd_max_chars=30, cmd_cooldown=120)
    bot_cfg = BotConfig(bot_id="bot_001", tts_enabled=True)
    handler, svc = _make_handler(bot_cfg, cfg.tts, admins=[10001])
    ev = _make_event(user_id=10001)

    # 管理员: 超长不限
    reply = await handler._cmd_tts("bot_001", ev, ["字" * 100])
    assert reply is None
    # 管理员: 无冷却(连续两次都入队)
    reply = await handler._cmd_tts("bot_001", ev, ["第二条"])
    assert reply is None
    assert svc.queued == 2


async def test_cmd_tts_voice_id_resolution() -> None:
    """per-bot tts_voice_id 解析进任务; 留空为 None(用全局)。"""
    cfg = GlobalConfig()
    cfg.tts = TTSConfig(enabled=True, voice_id="global_voice")
    bot_cfg = BotConfig(bot_id="bot_001", tts_enabled=True, tts_voice_id="bot001_voice")
    handler, svc = _make_handler(bot_cfg, cfg.tts)

    await handler._cmd_tts("bot_001", _make_event(), ["有覆盖"])
    assert svc._queue.qsize() == 1
    assert svc._queue.get_nowait().voice_id == "bot001_voice"

    bot_cfg.tts_voice_id = ""
    # 换一个用户(避免命中上一条的 /tts 冷却)
    await handler._cmd_tts("bot_001", _make_event(user_id=10002), ["无覆盖"])
    assert svc._queue.get_nowait().voice_id is None


# ── 5. 配置 round-trip ───────────────────────────────────────


def test_tts_config_roundtrip() -> None:
    cfg = GlobalConfig()
    cfg.tts = TTSConfig(
        enabled=True, base_url="https://api.minimaxi.com", api_key="sk-round",
        model="speech-2.8-turbo", voice_id="ltyclone01",
        speed=1.1, vol=1.2, pitch=2, sample_rate=24000, bitrate=96000,
        format="mp3", queue_maxsize=8, timeout=90,
        cmd_max_chars=50, cmd_cooldown=60,
    )
    with tempfile.TemporaryDirectory(prefix="tts_cfg_") as tmp:
        path = Path(tmp) / "global.yaml"
        cfg.save(path)
        loaded = GlobalConfig.load(path)
    assert loaded.tts.enabled is True
    assert loaded.tts.base_url == "https://api.minimaxi.com"
    assert loaded.tts.api_key == "sk-round"
    assert loaded.tts.model == "speech-2.8-turbo"
    assert loaded.tts.voice_id == "ltyclone01"
    assert abs(loaded.tts.speed - 1.1) < 1e-6
    assert abs(loaded.tts.vol - 1.2) < 1e-6
    assert loaded.tts.pitch == 2
    assert loaded.tts.sample_rate == 24000
    assert loaded.tts.bitrate == 96000
    assert loaded.tts.format == "mp3"
    assert loaded.tts.queue_maxsize == 8
    assert loaded.tts.timeout == 90
    assert loaded.tts.cmd_max_chars == 50
    assert loaded.tts.cmd_cooldown == 60
    # BotConfig tts_voice_id
    bot = BotConfig(bot_id="bot_001", tts_enabled=True, tts_voice_id="bot001_voice")
    assert bot.to_dict()["tts_voice_id"] == "bot001_voice"
    with tempfile.TemporaryDirectory(prefix="tts_bot_") as tmp:
        path = Path(tmp) / "config.json"
        bot.save(path)
        loaded_bot = BotConfig.load(path)
    assert loaded_bot.tts_enabled is True
    assert loaded_bot.tts_voice_id == "bot001_voice"


# ── 6. sync_config 热同步 ────────────────────────────────────


def test_sync_config_hot_update() -> None:
    """TTSService.sync_config: 字段原位拷入运行对象 + 客户端参数热同步。"""
    from mohobot.services.tts import TTSService

    new_cfg = TTSConfig(
        enabled=True, base_url="http://10.0.0.9:9890", api_key="tok-new",
        timeout=90, convert_to_mp3=False,
    )
    svc = TTSService(TTSConfig())  # 旧 cfg(默认 http): 已构造 client
    svc.sync_config(new_cfg)
    assert svc.cfg.base_url == "http://10.0.0.9:9890"
    assert svc.cfg.api_key == "tok-new"
    assert svc.cfg.convert_to_mp3 is False
    # http 客户端参数已原位热同步
    assert svc._client._base_url == "http://10.0.0.9:9890"
    assert svc._client._api_key == "tok-new"
    assert svc._client._timeout == 90.0


# ── 7. 模糊识别(空格/别名/方括号/纯标签) ──────────────────────


def test_fuzzy_whitespace() -> None:
    display, tts = strip_and_extract("你好呀。< tts >今天真开心< / tts >明天见。")
    assert display == "你好呀。今天真开心明天见。"
    assert tts == "今天真开心"
    # 闭标签空格变体 </ tts >
    display, tts = strip_and_extract("A< tts >读</ tts >B")
    assert display == "A读B" and tts == "读"


def test_fuzzy_aliases() -> None:
    for open_tag, close_tag in (("<voice>", "</voice>"), ("<SPEECH>", "</SPEECH>"), ("<Say>", "</Say>")):
        display, tts = strip_and_extract(f"前{open_tag}朗读内容{close_tag}后")
        assert display == "前朗读内容后", (open_tag, display)
        assert tts == "朗读内容"


def test_fuzzy_brackets() -> None:
    # 中文方括号
    display, tts = strip_and_extract("前【tts】中文括号读【/tts】后")
    assert display == "前中文括号读后" and tts == "中文括号读"
    # ASCII 方括号(含空格)
    display, tts = strip_and_extract("前[tts] ascii读[ /tts ]后")
    assert display == "前 ascii读后" and tts == "ascii读"
    # 大小写
    display, tts = strip_and_extract("【TTS】大写【/TTS】")
    assert tts == "大写"


def test_fuzzy_attributes_not_tag() -> None:
    """带属性的标签不算标签: 原样显示(游离闭标签仍被剥), 不进朗读。"""
    display, tts = strip_and_extract('A<tts speed="2">属性</tts>B')
    assert display == 'A<tts speed="2">属性B'
    assert tts == ""


def test_fuzzy_word_boundary_not_tag() -> None:
    display, tts = strip_and_extract("<ttsx>不是标签</ttsx>")
    assert display == "<ttsx>不是标签</ttsx>"
    assert tts == ""


def test_fuzzy_math_passthrough() -> None:
    """正文中的 < > 不是标签, 原样保留; 后面的真标签照常识别。"""
    display, tts = strip_and_extract("1 < 2 而且 3 > 2 <tts>读</tts>")
    assert display == "1 < 2 而且 3 > 2 读"
    assert tts == "读"


def test_fuzzy_mismatched_close() -> None:
    """开闭标签名不一致(如 <voice>...</tts>)也容忍收口。"""
    display, tts = strip_and_extract("<voice>内容</tts>尾")
    assert display == "内容尾" and tts == "内容"


def test_fuzzy_empty_first_span_picks_next() -> None:
    """空标注(只有空格)跳过, 取后面真正有内容的标注。"""
    display, tts = strip_and_extract("< tts >   </ tts >真正的<tts>读我</tts>")
    assert tts == "读我"
    assert display == "   真正的读我"


def test_fuzzy_orphan_close_dropped() -> None:
    display, tts = strip_and_extract("前</ tts >中<tts>读</tts>后")
    assert display == "前中读后" and tts == "读"


def test_fuzzy_streaming_split_with_spaces() -> None:
    """空格标签 + 方括号内容 跨 chunk 撕裂。"""
    f = TTSMarkerFilter()
    chunks = ["前文< t", "ts >读的", "话< / tts", " >后文"]
    collected = ""
    for c in chunks:
        collected += f.feed(c)
    rest, tts = f.finish()
    assert (collected + rest) == "前文读的话后文"
    assert tts == "读的话"


def test_fuzzy_streaming_bracket_placeholder() -> None:
    """正文里的 [图片] 等方括号占位不被误剥, 之后的真标签正常。"""
    f = TTSMarkerFilter()
    collected = f.feed("看[图片]啊")
    rest, tts = f.finish()
    assert (collected + rest) == "看[图片]啊" and tts == ""


def test_fuzzy_streaming_math_never_flushes_wrong() -> None:
    """数学式跨 chunk: 内容一个字符都不能丢。"""
    f = TTSMarkerFilter()
    chunks = ["a < b ", "和 c > d", " <tts>", "读", "</tts>"]
    collected = ""
    for c in chunks:
        collected += f.feed(c)
    rest, tts = f.finish()
    assert (collected + rest) == "a < b 和 c > d 读"
    assert tts == "读"


def test_ts_alias_all_chunk_boundaries() -> None:
    cases = [
        ("前<ts>朗读内容</ts>后", "前朗读内容后", "朗读内容"),
        ("前< TS >朗读< / ts >后", "前朗读后", "朗读"),
        ("[ts]第一段[/ts]【TS】第二段【/TS】", "第一段第二段", "第一段\n第二段"),
        ("<ts>混合闭标签</tts>尾", "混合闭标签尾", "混合闭标签"),
        ("<tts>混合闭标签</ts>尾", "混合闭标签尾", "混合闭标签"),
        ("前<ts>未闭合", "前未闭合", "未闭合"),
        ("前</ts>后", "前后", ""),
        ("<tsx>原样</tsx><ts2>保留</ts2>", "<tsx>原样</tsx><ts2>保留</ts2>", ""),
        ('<ts speed="2">原样</ts>', '<ts speed="2">原样', ""),
    ]
    for raw, display, speech in cases:
        assert strip_and_extract(raw) == (display, speech), raw
        chunkings = [[raw[:cut], raw[cut:]] for cut in range(len(raw) + 1)]
        chunkings.append(list(raw))
        for chunks in chunkings:
            marker = TTSMarkerFilter()
            output = "".join(marker.feed(chunk) for chunk in chunks)
            rest, actual_speech = marker.finish()
            assert (output + rest, actual_speech) == (display, speech), chunks


async def test_tts_reply_modes_keep_display_speech_and_context_consistent() -> None:
    from mohobot.message_handler import MessageHandler
    from unittest.mock import AsyncMock

    for tag in ("tts", "ts"):
        for stream, segment in ((True, True), (False, True), (True, False), (False, False)):
            speech = "这是一段需要完整朗读的语音内容。"
            raw = f"前文<{tag}>{speech}</{tag}>后文[尾"
            expected = f"前文{speech}后文[尾"
            handler = MessageHandler.__new__(MessageHandler)
            handler._stream = stream
            handler._segment_reply = segment
            handler._reply_quote = False
            handler._seg_min_len = 12
            handler._seg_max_len = 60
            handler._seg_delay_min = handler._seg_delay_max = 0
            handler._global_config = GlobalConfig()
            handler._global_config.tts.enabled = True
            bot_config = BotConfig(bot_id="bot_001", tts_enabled=True)
            handler._bot_config = lambda bot_id: bot_config
            jobs = []
            handler._tts = SimpleNamespace(submit=lambda job: jobs.append(job) or True)

            async def chunks(**kwargs):
                for index, char in enumerate(raw):
                    yield char, index == len(raw) - 1

            handler._llm = SimpleNamespace(
                chat=AsyncMock(return_value=(raw, None)), chat_stream=chunks,
            )
            handler._send_message = AsyncMock()
            reply = await handler._stream_llm_reply("bot_001", _make_event(), [], {})
            displayed = "".join(call.args[2] for call in handler._send_message.await_args_list)
            assert displayed == expected, (tag, stream, segment, displayed)
            assert reply == expected, (tag, stream, segment, reply)
            assert len(jobs) == 1 and jobs[0].text == speech
            assert jobs[0].bot_id == "bot_001" and jobs[0].source == "llm"


# ── 8. 自建 HTTP 后端(http)与后端切换 ─────────────────────────


async def test_http_client_ok() -> None:
    import httpx
    from mohobot.services.tts import HttpTTSClient

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization", "")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"RIFFxxxxwavdata")

    factory = lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=kw.get("timeout", 5)
    )
    c = HttpTTSClient("http://127.0.0.1:9890", "tok-abc", timeout=30, http_client_factory=factory)
    audio, chars = await c.synthesize("你好", voice_id="ignored")
    assert audio == b"RIFFxxxxwavdata"
    assert chars == len("你好")
    assert captured["path"] == "/tts"
    assert captured["auth"] == "Bearer tok-abc"
    assert captured["body"] == {"text": "你好"}
    await c.close()


async def test_http_client_errors() -> None:
    import httpx
    from mohobot.services.tts import HttpTTSClient

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "错" in body.get("text", ""):
            return httpx.Response(401, text="unauthorized")
        if "json" in body.get("text", ""):
            # 200 但返回 JSON 错误体 → 不当音频
            return httpx.Response(200, json={"error": "boom"})
        if "空" in body.get("text", ""):
            return httpx.Response(200, content=b"")
        return httpx.Response(200, content=b"RIFFok")

    factory = lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=kw.get("timeout", 5)
    )
    c = HttpTTSClient("http://127.0.0.1:9890", "tok", http_client_factory=factory)
    for text in ("错误文本", "json错误体", "空响应"):
        audio, chars = await c.synthesize(text)
        assert audio is None and chars == 0, text
    audio, chars = await c.synthesize("正常")
    assert audio == b"RIFFok"
    await c.close()


def test_backend_selection_and_hot_switch() -> None:
    """TTSService 按 cfg.backend 构造客户端; sync_config 热切换后端。"""
    from mohobot.services.minimax_tts import MinimaxTTSClient
    from mohobot.services.tts import HttpTTSClient, TTSService

    svc = TTSService(TTSConfig(backend="http"))
    assert isinstance(svc._client, HttpTTSClient)
    # 切到 minimax
    svc.sync_config(TTSConfig(backend="minimax", voice_id="v1"))
    assert isinstance(svc._client, MinimaxTTSClient)
    assert svc.cfg.backend == "minimax"
    # 切回 http
    svc.sync_config(TTSConfig(backend="http", base_url="http://10.0.0.9:9890"))
    assert isinstance(svc._client, HttpTTSClient)
    assert svc._client._base_url == "http://10.0.0.9:9890"


def test_tts_config_backend_roundtrip() -> None:
    cfg = GlobalConfig()
    cfg.tts = TTSConfig(
        enabled=True, backend="http", base_url="http://180.171.52.55:9890",
        api_key="tok-xyz", convert_to_mp3=False,
    )
    with tempfile.TemporaryDirectory(prefix="tts_be_") as tmp:
        path = Path(tmp) / "global.yaml"
        cfg.save(path)
        loaded = GlobalConfig.load(path)
    assert loaded.tts.backend == "http"
    assert loaded.tts.base_url == "http://180.171.52.55:9890"
    assert loaded.tts.api_key == "tok-xyz"
    assert loaded.tts.convert_to_mp3 is False
    # 非法 backend 回落 http
    cfg2 = GlobalConfig()
    cfg2.tts = TTSConfig(backend="http")
    with tempfile.TemporaryDirectory(prefix="tts_be2_") as tmp:
        path = Path(tmp) / "global.yaml"
        cfg2.save(path)
        text = path.read_text(encoding="utf-8").replace("backend: http", "backend: bogus")
        path.write_text(text, encoding="utf-8")
        loaded2 = GlobalConfig.load(path)
    assert loaded2.tts.backend == "http"


async def test_worker_mp3_convert_fallback() -> None:
    """worker: 转码成功发 mp3 字节; 转码失败降级发原始 wav。"""
    from mohobot.services.tts import TTSJob, TTSService

    class FakeClient:
        def __init__(self, audio):
            self.audio = audio
        def sync_config(self, cfg): pass
        async def synthesize(self, text, voice_id=None):
            return self.audio, len(text)
        async def close(self): pass

    sent = []
    class FakeWS:
        async def send_group_msg(self, bot_id, chat_id, msg):
            sent.append(msg)

    svc = TTSService(TTSConfig(enabled=True, convert_to_mp3=True))
    svc.set_ws(FakeWS())

    async def fake_to_mp3(wav):
        return b"MP3BYTES" if wav == b"WAVSRC" else None

    svc._to_mp3 = fake_to_mp3

    # 转码成功
    svc._client = FakeClient(b"WAVSRC")
    await svc._process(TTSJob("b", "group", "1", "hi"))
    f = sent[-1][0]["data"]["file"]
    assert f.startswith("base64://")
    import base64 as b64mod
    assert b64mod.b64decode(f[len("base64://"):]) == b"MP3BYTES"

    # 转码失败 → 降级 wav
    svc._to_mp3 = lambda wav: _none_async(wav)
    await svc._process(TTSJob("b", "group", "1", "hi"))
    f = sent[-1][0]["data"]["file"]
    assert b64mod.b64decode(f[len("base64://"):]) == b"WAVSRC"
    assert svc.stats["done"] == 2

def _none_async(wav):
    async def inner():
        return None
    return inner()
