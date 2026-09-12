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
    normalize_tts_content,
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


def test_filter_multiple_spans_take_first() -> None:
    f = TTSMarkerFilter()
    f.feed("<tts>第一句</tts>中间<tts>第二句</tts>")
    _, tts = f.finish()
    assert tts == "第一句"


def test_normalize_truncate_at_punctuation() -> None:
    assert normalize_tts_content("短句") == "短句"
    # 超过 20 字 → 截到第一个句末标点(含标点)
    long_text = "这是一段特别长的标注内容已经超过了二十个字的限制。后面不该被读"
    result = normalize_tts_content(long_text)
    assert result == "这是一段特别长的标注内容已经超过了二十个字的限制。"
    # 无标点 → 硬截 20
    no_punct = "啊" * 30
    assert normalize_tts_content(no_punct) == "啊" * 20


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
    from mohobot.services.minimax_tts import TTSJob, TTSService

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
    from mohobot.services.minimax_tts import TTSService

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
    from mohobot.services.minimax_tts import TTSService

    new_cfg = TTSConfig(
        enabled=True, base_url="https://api.minimaxi.com", api_key="sk-new",
        model="speech-2.8-turbo", voice_id="new_voice", speed=0.9,
    )
    svc = TTSService(TTSConfig())  # 旧 cfg(默认): 已构造 client
    svc.sync_config(new_cfg)
    assert svc.cfg.base_url == new_cfg.base_url
    assert svc.cfg.voice_id == "new_voice"
    assert svc.cfg.api_key == "sk-new"
    # 客户端参数已热同步
    assert svc._client._model == "speech-2.8-turbo"
    assert svc._client._voice_id == "new_voice"
    assert svc._client._api_key == "sk-new"
    assert abs(svc._client._voice_setting["speed"] - 0.9) < 1e-9
