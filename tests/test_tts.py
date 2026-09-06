"""TTS 语音(GPT-SoVITS)测试:
1. <tts> 标记过滤器: 剥标签/跨 chunk 撕裂/未闭合/超长截标点
2. GsvTTSClient: httpx MockTransport 200/400
3. TTSService 队列: 满时丢最新
4. /tts 指令: 字数上限/冷却/管理员豁免/per-bot 开关
5. TTSConfig 配置 round-trip
"""

import asyncio
import sys
import tempfile
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


# ── 2. GsvTTSClient ──────────────────────────────────────────


async def test_gsv_client_200_and_400() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        if b"bad" in payload:
            return httpx.Response(400, json={"message": "ref audio not found"})
        return httpx.Response(200, content=b"RIFFfake_audio_bytes")

    factory = lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=kw.get("timeout", 5)
    )
    from mohobot.services.gsv_tts import GsvTTSClient

    client = GsvTTSClient(
        "http://127.0.0.1:9880", ref_audio_path="/ref.wav",
        prompt_text="原文", http_client_factory=factory,
    )
    ok = await client.synthesize("你好")
    assert ok == b"RIFFfake_audio_bytes"
    bad = await client.synthesize("bad text")
    assert bad is None
    await client.close()


# ── 3. TTSService 队列(丢最新) ────────────────────────────────


def test_tts_queue_drop_newest() -> None:
    from mohobot.services.gsv_tts import TTSJob, TTSService

    cfg = TTSConfig(enabled=True, queue_maxsize=2)
    svc = TTSService(cfg)  # 不 start worker, 只测队列

    assert svc.submit(TTSJob("b", "group", "1", "第一条")) is True
    assert svc.submit(TTSJob("b", "group", "1", "第二条")) is True
    # 队列满 → 丢最新
    assert svc.submit(TTSJob("b", "group", "1", "第三条")) is False
    assert svc.queued == 2


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
    from mohobot.services.gsv_tts import TTSService

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


# ── 5. 配置 round-trip ───────────────────────────────────────


def test_tts_config_roundtrip() -> None:
    cfg = GlobalConfig()
    cfg.tts = TTSConfig(
        enabled=True, base_url="http://10.0.0.5:9880", queue_maxsize=8,
        ref_audio_path="D:/refs/v.wav", prompt_text="原文内容",
        speed_factor=1.1, cmd_max_chars=50, cmd_cooldown=60,
        top_k=10, top_p=0.9, temperature=0.8, fragment_interval=0.5,
        text_split_method="cut3", timeout=300,
        service_command="/x/bin/python api_v2.py -p 9880",
        service_cwd="/opt/gsv", service_log_path="/tmp/gsv.log",
        gsv_config_path="/opt/gsv/tts_infer.yaml", stop_wait_seconds=15,
    )
    with tempfile.TemporaryDirectory(prefix="tts_cfg_") as tmp:
        path = Path(tmp) / "global.yaml"
        cfg.save(path)
        loaded = GlobalConfig.load(path)
    assert loaded.tts.enabled is True
    assert loaded.tts.base_url == "http://10.0.0.5:9880"
    assert loaded.tts.queue_maxsize == 8
    assert loaded.tts.ref_audio_path == "D:/refs/v.wav"
    assert loaded.tts.prompt_text == "原文内容"
    assert abs(loaded.tts.speed_factor - 1.1) < 1e-6
    assert loaded.tts.cmd_max_chars == 50
    assert loaded.tts.cmd_cooldown == 60
    assert loaded.tts.top_k == 10
    assert abs(loaded.tts.top_p - 0.9) < 1e-6
    assert abs(loaded.tts.temperature - 0.8) < 1e-6
    assert abs(loaded.tts.fragment_interval - 0.5) < 1e-6
    assert loaded.tts.text_split_method == "cut3"
    assert loaded.tts.timeout == 300
    assert loaded.tts.service_command == "/x/bin/python api_v2.py -p 9880"
    assert loaded.tts.service_cwd == "/opt/gsv"
    assert loaded.tts.service_log_path == "/tmp/gsv.log"
    assert loaded.tts.gsv_config_path == "/opt/gsv/tts_infer.yaml"
    assert loaded.tts.stop_wait_seconds == 15
    # BotConfig tts_enabled
    bot = BotConfig(bot_id="bot_001", tts_enabled=True)
    assert bot.to_dict()["tts_enabled"] is True
    with tempfile.TemporaryDirectory(prefix="tts_bot_") as tmp:
        path = Path(tmp) / "config.json"
        bot.save(path)
        loaded_bot = BotConfig.load(path)
    assert loaded_bot.tts_enabled is True


# ── 新增: sync_config 热同步 + 进程管理分支 ─────────────────────


def test_sync_config_hot_update() -> None:
    """TTSService.sync_config: 字段原位拷入运行对象 + 客户端 payload 热同步。"""
    from mohobot.services.gsv_tts import TTSService

    new_cfg = TTSConfig(
        enabled=True, base_url="http://10.0.1.9:9880", timeout=300,
        ref_audio_path="/new/ref.wav", prompt_text="新原文",
        top_k=7, text_split_method="cut2",
    )
    svc = TTSService(TTSConfig())  # 旧 cfg(默认): 已构造 client
    old_url = svc._client.tts_url
    svc.sync_config(new_cfg)
    assert svc.cfg.base_url == new_cfg.base_url
    assert svc.cfg.ref_audio_path == "/new/ref.wav"
    assert svc.cfg.top_k == 7
    # 客户端 payload 已热同步
    assert svc._client._payload["ref_audio_path"] == "/new/ref.wav"
    assert svc._client._payload["top_k"] == 7
    assert svc._client._payload["text_split_method"] == "cut2"
    assert svc._client.tts_url.endswith(":9880/tts")
    assert svc._client.tts_url != old_url


async def test_gsc_client_payload_extended() -> None:
    """新采样参数出现在 /tts 请求体内。"""
    import httpx

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"RIFFx")

    factory = lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=kw.get("timeout", 5))
    from mohobot.services.gsv_tts import GsvTTSClient

    c = GsvTTSClient(
        "http://127.0.0.1:9880", ref_audio_path="/r.wav", prompt_text="pp",
        top_k=3, top_p=0.7, temperature=0.9, fragment_interval=0.6,
        text_split_method="cut1", http_client_factory=factory,
    )
    await c.synthesize("你好")
    await c.close()
    b = captured["body"]
    assert b["top_k"] == 3
    assert abs(b["top_p"] - 0.7) < 1e-9
    assert abs(b["temperature"] - 0.9) < 1e-9
    assert abs(b["fragment_interval"] - 0.6) < 1e-9
    assert b["text_split_method"] == "cut1"
    assert b["streaming_mode"] is False


async def test_service_management_branches() -> None:
    """进程管理分支: 未配置命令/未运行停止。"""
    from mohobot.services.gsv_tts import TTSService

    svc = TTSService(TTSConfig())  # service_command 为空

    ok, msg = await svc.start_service()
    assert not ok and "未配置" in msg
    ok, msg = await svc.stop_service()
    assert not ok and "未在运行" in msg
