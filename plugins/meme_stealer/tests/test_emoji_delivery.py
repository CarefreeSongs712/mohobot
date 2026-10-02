import base64
import json
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from meme_stealer_core.core.config.config import PluginConfig
from meme_stealer_core.core.events import emoji_delivery
from meme_stealer_core.host import StealerEvent


class FakeBridge:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[tuple[str, str, str, list]] = []

    async def send_segments(self, bot_id, chat_type, chat_id, segments):
        self.sent.append((bot_id, chat_type, chat_id, segments))
        return self.ok

    async def fetch_image_data_uri(self, bot_id, file_ref):
        return ""

    async def fetch_message_segments(self, bot_id, message_id):
        return []


@pytest.fixture
def delivery(tmp_path):
    bridge = FakeBridge()
    raw = {"group_id": 123, "user_id": 42, "message_id": 7, "message": []}
    event = StealerEvent("bot_001", types.SimpleNamespace(**raw), raw, bridge=bridge)
    path = tmp_path / "meme.png"
    path.write_bytes(b"image")
    return event, bridge, str(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [True, None])
@pytest.mark.parametrize("as_gif", [False, True])
async def test_qq_sticker_type_with_enabled_or_legacy_config(delivery, setting, as_gif):
    event, bridge, path = delivery
    renderer = types.SimpleNamespace(file_to_gif_base64=AsyncMock(return_value="gif"))
    plugin = types.SimpleNamespace(send_meme_as_gif=as_gif, image_render_service=renderer)
    if setting is not None:
        plugin.send_meme_as_qq_sticker = setting

    assert await emoji_delivery.send_qq_image_as_sticker(event, path, plugin=plugin) == "qq_sticker"

    expected = "gif" if as_gif else base64.b64encode(b"image").decode()
    assert bridge.sent == [
        (
            "bot_001",
            "group",
            "123",
            [
                {
                    "type": "image",
                    "data": {
                        "file": f"base64://{expected}",
                        "sub_type": 1,
                        "summary": "[动画表情]",
                    },
                }
            ],
        )
    ]


@pytest.mark.asyncio
async def test_disabled_sticker_sends_plain_image(delivery):
    event, bridge, path = delivery
    plugin = types.SimpleNamespace(send_meme_as_qq_sticker=False)

    assert await emoji_delivery.send_qq_image_as_sticker(event, path, plugin=plugin) == "image"
    segment = bridge.sent[0][3][0]
    assert segment["data"] == {"file": f"base64://{base64.b64encode(b'image').decode()}"}


@pytest.mark.asyncio
async def test_private_chat_goes_to_private_target(tmp_path):
    bridge = FakeBridge()
    raw = {"user_id": 42, "message_id": 7, "message": []}
    event = StealerEvent("bot_001", types.SimpleNamespace(**raw), raw, bridge=bridge)
    path = tmp_path / "meme.png"
    path.write_bytes(b"image")

    assert await emoji_delivery.send_qq_image_as_sticker(event, str(path))
    assert bridge.sent[0][:3] == ("bot_001", "private", "42")


@pytest.mark.asyncio
async def test_send_failure_returns_none(delivery, monkeypatch):
    event, bridge, path = delivery
    bridge.ok = False
    assert await emoji_delivery.send_qq_image_as_sticker(event, path) is None


@pytest.mark.asyncio
async def test_bridge_exception_logs_and_returns_none(delivery, monkeypatch):
    event, bridge, path = delivery

    async def boom(*_args):
        raise RuntimeError("send failed")

    bridge.send_segments = boom
    logger = Mock()
    monkeypatch.setattr(emoji_delivery, "logger", logger)

    assert await emoji_delivery.send_qq_image_as_sticker(event, path) is None
    logger.warning.assert_called_once()
    assert "send failed" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_missing_file_is_not_sent(delivery):
    event, bridge, _path = delivery
    assert await emoji_delivery.send_qq_image_as_sticker(event, "/nonexistent/meme.png") is None
    assert bridge.sent == []


def test_sticker_setting_schema_and_config_default():
    root = Path(__file__).parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["send_meme_as_qq_sticker"]["type"] == "bool"
    assert schema["send_meme_as_qq_sticker"]["default"] is True
    assert PluginConfig.model_fields["send_meme_as_qq_sticker"].default is True
