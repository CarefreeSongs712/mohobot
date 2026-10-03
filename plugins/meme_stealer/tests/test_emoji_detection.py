"""OneBot 表情包识别：哪些图片段算表情包、商城表情 URL 提取。"""

from types import SimpleNamespace

import pytest

from meme_stealer_core.core.events.platform_detector import PlatformDetector
from meme_stealer_core.host import StealerEvent


def _event(segments):
    raw = {"group_id": 1, "user_id": 2, "message_id": 3, "message": segments}
    return StealerEvent("bot_001", SimpleNamespace(**raw), raw)


def _is_emoji(data: dict) -> bool:
    event = _event([{"type": "text", "data": {"text": "看"}}, {"type": "image", "data": data}])
    image = event.get_images()[0]
    return PlatformDetector().check_platform_emoji_metadata(image, event, img_index=0)


@pytest.mark.parametrize(
    "data",
    [
        {"file": "a.jpg", "sub_type": 1},
        {"file": "a.jpg", "sub_type": "1"},
        {"file": "a.jpg", "summary": "[动画表情]"},
        {"file": "a.jpg", "emoji_id": "123", "emoji_package_id": "9"},
        {"file": "a.jpg", "url": "https://gxh.vip.qq.com/club/item/parcel/1/raw300.gif"},
    ],
)
def test_onebot_sticker_markers_are_detected(data):
    assert _is_emoji(data)


@pytest.mark.parametrize(
    "data",
    [
        {"file": "photo.jpg", "sub_type": 0, "url": "https://multimedia.nt.qq.com.cn/x"},
        {"file": "photo.jpg", "summary": ""},
        {"file": "photo.webp"},
    ],
)
def test_ordinary_images_are_not_stickers(data):
    assert not _is_emoji(data)


def test_detection_without_explicit_index_matches_segment_by_file():
    event = _event(
        [
            {"type": "image", "data": {"file": "photo.jpg"}},
            {"type": "image", "data": {"file": "meme.gif", "sub_type": 1}},
        ]
    )
    photo, meme = event.get_images()
    detector = PlatformDetector()
    assert not detector.check_platform_emoji_metadata(photo, event)
    assert detector.check_platform_emoji_metadata(meme, event)


def test_store_emoji_urls_come_from_mface_segments():
    event = _event(
        [
            {"type": "mface", "data": {"url": "https://gxh.vip.qq.com/a.gif", "summary": "[比心]"}},
            {"type": "mface", "data": {"url": "https://gxh.vip.qq.com/a.gif"}},
            {"type": "image", "data": {"url": "https://example.test/photo.jpg"}},
        ]
    )
    assert PlatformDetector().extract_store_emoji_urls(event) == ["https://gxh.vip.qq.com/a.gif"]
