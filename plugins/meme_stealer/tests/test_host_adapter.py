"""mohobot 适配层：事件视图、消息组件、配置存档与模型网关的回退规则。"""

import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meme_stealer_core.core.events.image_download_service import ImageDownloadService
from meme_stealer_core.host import ConfigStore, Image, ModelGateway, Plain, StealerEvent
from meme_stealer_core.host.components import to_onebot_segments

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


class FakeBridge:
    def __init__(self, *, image_uri: str = "", quoted: list | None = None):
        self.image_uri = image_uri
        self.quoted = quoted or []
        self.image_requests: list[tuple[str, str]] = []
        self.message_requests: list[tuple[str, str]] = []
        self.sent: list[tuple] = []

    async def send_segments(self, bot_id, chat_type, chat_id, segments):
        self.sent.append((bot_id, chat_type, chat_id, segments))
        return True

    async def fetch_image_data_uri(self, bot_id, file_ref):
        self.image_requests.append((bot_id, file_ref))
        return self.image_uri

    async def fetch_message_segments(self, bot_id, message_id):
        self.message_requests.append((bot_id, message_id))
        return self.quoted


def _group_event(message, bridge=None, **overrides):
    raw = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123,
        "user_id": 42,
        "self_id": 10001,
        "message_id": 555,
        "sender": {"user_id": 42, "nickname": "小明", "card": "群名片"},
        "message": message,
    }
    raw.update(overrides)
    event = SimpleNamespace(**{k: v for k, v in raw.items() if k != "sender"})
    event.sender = SimpleNamespace(**raw["sender"])
    return StealerEvent("bot_001", event, raw, bridge=bridge)


# ── StealerEvent ────────────────────────────────────────


def test_group_event_identity_and_session_keys():
    event = _group_event([{"type": "text", "data": {"text": " 看这个 "}}])
    assert event.chat_type == "group"
    assert event.chat_id == "123"
    assert event.get_sender_name() == "群名片"
    assert event.get_message_str() == "看这个"
    assert event.unified_msg_origin == "GroupMessage:123"
    assert event.message_obj.raw_message["message"][0]["type"] == "text"


def test_private_event_session_is_per_bot():
    raw = {"user_id": 42, "message_id": 1, "message": "你好", "sender": {"nickname": "小明"}}
    event = StealerEvent("bot_002", SimpleNamespace(user_id=42, message="你好"), raw)
    assert event.chat_type == "private"
    assert event.chat_id == "42"
    assert event.unified_msg_origin == "bot_002:FriendMessage:42"
    assert event.is_private_chat()


def test_cq_string_message_is_parsed_into_components():
    event = _group_event("[CQ:reply,id=99]看[CQ:image,file=abc.jpg,sub_type=1,url=https://x.test/a.jpg]")
    comps = event.get_messages()
    assert [type(c).__name__ for c in comps] == ["Segment", "Plain", "Image"]
    image = event.get_images()[0]
    assert image.url == "https://x.test/a.jpg"
    assert str(image.sub_type) == "1"
    assert event.get_reply_id() == "99"


@pytest.mark.asyncio
async def test_image_without_url_resolves_through_get_image():
    bridge = FakeBridge(image_uri=PNG_DATA_URI)
    event = _group_event([{"type": "image", "data": {"file": "ABC.jpg"}}], bridge=bridge)
    image = event.get_images()[0]

    assert not image.has_fetchable_ref()
    assert await image.resolve_url() == PNG_DATA_URI
    assert image.url == PNG_DATA_URI
    assert bridge.image_requests == [("bot_001", "ABC.jpg")]

    path = await image.convert_to_file_path()
    try:
        with open(path, "rb") as handle:
            assert handle.read() == PNG_BYTES
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_quoted_images_come_from_get_msg():
    bridge = FakeBridge(quoted=[{"type": "image", "data": {"url": "https://x.test/q.gif"}}])
    event = _group_event([{"type": "reply", "data": {"id": "77"}}, {"type": "text", "data": {"text": "偷"}}], bridge)

    images = await event.get_quoted_images()

    assert [img.url for img in images] == ["https://x.test/q.gif"]
    assert bridge.message_requests == [("bot_001", "77")]


@pytest.mark.asyncio
async def test_send_routes_through_bridge_as_onebot_segments(tmp_path):
    bridge = FakeBridge()
    event = _group_event([], bridge=bridge)
    path = tmp_path / "a.png"
    path.write_bytes(PNG_BYTES)

    assert await event.send(event.plain_result("hi").file_image(str(path)))

    _bot, chat_type, chat_id, segments = bridge.sent[0]
    assert (chat_type, chat_id) == ("group", "123")
    assert segments[0] == {"type": "text", "data": {"text": "hi"}}
    assert segments[1]["data"]["file"] == "base64://" + base64.b64encode(PNG_BYTES).decode()


def test_to_onebot_segments_keeps_remote_urls_and_extra_segments():
    segments = to_onebot_segments(
        [Plain("a"), Image.fromURL("https://x.test/a.png"), {"type": "face", "data": {"id": "1"}}]
    )
    assert segments == [
        {"type": "text", "data": {"text": "a"}},
        {"type": "image", "data": {"file": "https://x.test/a.png"}},
        {"type": "face", "data": {"id": "1"}},
    ]


# ── ConfigStore ─────────────────────────────────────────


def test_config_store_incremental_save_keeps_panel_written_keys(tmp_path):
    path = tmp_path / "plugins_config" / "meme_stealer.json"
    store = ConfigStore(path, {"steal_meme": False})
    store.save_config()
    # 面板在插件不知情时写入了新键
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["meme_chance"] = 0.5
    path.write_text(json.dumps(on_disk), encoding="utf-8")

    assert store.save_config({"steal_meme": True})

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {"steal_meme": True, "meme_chance": 0.5}
    assert store["steal_meme"] is True


# ── ModelGateway ────────────────────────────────────────


def _gateway(plugin_cfg: dict, llm: dict | None = None):
    cfg = SimpleNamespace(**plugin_cfg)
    llm_cfg = SimpleNamespace(
        **{
            "chat_model": "chat-m",
            "chat_base_url": "https://chat.example/v1",
            "chat_api_key": "chat-key",
            "vision_model": "vl-m",
            "vision_base_url": "https://vl.example/v1",
            "vision_api_key": "",
            **(llm or {}),
        }
    )
    service = SimpleNamespace(_cfg=SimpleNamespace(llm=llm_cfg))
    return ModelGateway(lambda: cfg, lambda: service)


def test_vision_falls_back_to_global_model_and_key_chain(monkeypatch):
    monkeypatch.delenv("MOHOBOT_VISION_API_KEY", raising=False)
    endpoint = _gateway({"vision_model": ""}).endpoint("vision")
    assert (endpoint.model, endpoint.base_url, endpoint.api_key) == (
        "vl-m", "https://vl.example/v1", "chat-key"
    )
    monkeypatch.setenv("MOHOBOT_VISION_API_KEY", "env-vl-key")
    assert _gateway({}).endpoint("vision").api_key == "env-vl-key"


def test_plugin_base_url_never_reuses_global_key():
    endpoint = _gateway(
        {"vision_model": "my-vl", "vision_base_url": "https://other.example/v1", "vision_api_key": ""}
    ).endpoint("vision")
    assert (endpoint.model, endpoint.base_url, endpoint.api_key) == (
        "my-vl", "https://other.example/v1", ""
    )


def test_rewrite_and_embedding_resolution():
    gw = _gateway({"embedding_model": "emb-m"})
    rewrite = gw.endpoint("rewrite")
    assert (rewrite.model, rewrite.base_url, rewrite.api_key) == (
        "chat-m", "https://chat.example/v1", "chat-key"
    )
    emb = gw.endpoint("embedding")
    assert (emb.model, emb.base_url, emb.api_key) == ("emb-m", "https://chat.example/v1", "chat-key")
    assert _gateway({}).endpoint("embedding") is None
    assert _gateway({}).embedding_provider() is None


@pytest.mark.asyncio
async def test_embed_passes_dimensions_and_tracks_vector_size():
    gw = _gateway({"embedding_model": "emb-m", "embedding_dimensions": 4})
    create = AsyncMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3, 0.4])], usage=None)
    )
    gw._client = lambda endpoint: SimpleNamespace(embeddings=SimpleNamespace(create=create))

    provider = gw.embedding_provider()
    assert await provider.get_embedding("文本") == [0.1, 0.2, 0.3, 0.4]
    assert create.await_args.kwargs == {"model": "emb-m", "input": "文本", "dimensions": 4}
    assert provider.get_dim() == 4
    assert await provider.probe() == 4


@pytest.mark.asyncio
async def test_vision_sends_image_as_data_uri_and_records_usage(tmp_path):
    gw = _gateway({"vision_model": "my-vl"})
    recorded = []

    async def record(model, usage, bot_id, event, module, kind):
        recorded.append((model, module, kind))

    gw._llm_service_getter().__dict__["_record_usage"] = record
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=' {"approved": true} '))],
            usage={"total_tokens": 3},
        )
    )
    gw._client = lambda endpoint: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    path = tmp_path / "a.png"
    path.write_bytes(PNG_BYTES)

    assert await gw.vision("PROMPT", str(path)) == '{"approved": true}'
    content = create.await_args.kwargs["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "PROMPT"}
    assert content[1]["image_url"]["url"] == PNG_DATA_URI
    assert recorded == [("my-vl", "meme_stealer", "vision")]


# ── 下载服务的内嵌图片 ──────────────────────────────────


@pytest.mark.asyncio
async def test_download_service_handles_inline_and_resolved_images():
    service = ImageDownloadService()
    for img in (
        Image(url=PNG_DATA_URI),
        Image(file="base64://" + base64.b64encode(PNG_BYTES).decode()),
    ):
        path, is_gif = await service.download_original_image(img)
        try:
            assert path.endswith(".png") and not is_gif
            with open(path, "rb") as handle:
                assert handle.read() == PNG_BYTES
        finally:
            os.unlink(path)

    async def resolver(_image):
        return PNG_DATA_URI

    path, _ = await service.download_original_image(Image(file="ABC.jpg", resolver=resolver))
    try:
        assert path and os.path.getsize(path) == len(PNG_BYTES)
    finally:
        os.unlink(path)

    assert await service.download_original_image(Image(file="ABC.jpg")) == (None, False)
    await service.close()
