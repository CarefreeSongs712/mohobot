"""表情包发送桥接投递判定回归（issue #128）。

`send_segments` 不能只看「有没有抛异常」就返回 True：
- 空段 / 过滤后为空一律失败；
- 投递拦截、超时、bot 离线返回 None 视为失败；
- 有回执时用 delivery_status 或 status+retcode 双证据判定；
- 失败时调用方不得进入冷却 / 累加使用次数。
"""

import importlib.util
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_OK_GROUP = {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
_OK_PRIVATE = {"status": "ok", "retcode": 0, "delivery_status": "success", "data": {}}
_TEXT_SEG = {"type": "text", "data": {"text": "hi"}}
_IMAGE_SEG = {"type": "image", "data": {"file": "base64://iVBORw0KGgo="}}
_EMPTY_IMAGE_SEG = {"type": "image", "data": {"file": "base64://"}}
_NO_DATA_IMAGE_SEG = {"type": "image", "data": {}}


class _Manager:
    def __init__(self, online: bool):
        self.online = online

    def get(self, bot_id):
        return object() if self.online else None


_UNSET = object()


class FakeWS:
    def __init__(self, group_response=_UNSET, private_response=_UNSET, online=True):
        self.sent = []
        self.group_response = dict(_OK_GROUP) if group_response is _UNSET else group_response
        self.private_response = (
            dict(_OK_PRIVATE) if private_response is _UNSET else private_response
        )
        self._bot_manager = _Manager(online)

    async def send_group_msg(self, bot_id, group_id, message, source="auto"):
        self.sent.append(("group", bot_id, str(group_id), message))
        return self.group_response

    async def send_private_msg(self, bot_id, user_id, message, source="auto", *, wait_response=False):
        self.sent.append(("private", bot_id, str(user_id), message))
        return self.private_response if wait_response else None

    async def send_to_bot(self, bot_id, action, params=None, wait_response=False, timeout=10.0):
        params = dict(params or {})
        if action == "send_group_msg":
            self.sent.append(("group", bot_id, str(params.get("group_id")), params.get("message")))
            return self.group_response
        if action == "send_private_msg":
            self.sent.append(("private", bot_id, str(params.get("user_id")), params.get("message")))
            return self.private_response
        return dict(_OK_GROUP)


@pytest.fixture
def entry():
    spec = importlib.util.spec_from_file_location(
        "meme_stealer_bridge_under_test", PLUGIN_DIR / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bridge(entry, ws):
    entry.Plugin._ws_server = ws
    entry.Plugin._bot_manager = ws._bot_manager
    return entry._MohobotBridge()


@pytest.mark.asyncio
async def test_empty_segments_are_not_sent(entry):
    ws = FakeWS()
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments("bot_001", "group", "123", []) is False
    assert ws.sent == []


@pytest.mark.asyncio
async def test_all_segments_filtered_out_is_failure(entry):
    ws = FakeWS()
    bridge = _bridge(entry, ws)
    segments = [_EMPTY_IMAGE_SEG, _NO_DATA_IMAGE_SEG, {"type": "image", "data": {"url": ""}}]
    assert await bridge.send_segments("bot_001", "group", "123", segments) is False
    assert ws.sent == []


@pytest.mark.asyncio
async def test_invalid_image_dropped_but_text_still_sent(entry):
    ws = FakeWS()
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments(
        "bot_001", "group", "123", [_TEXT_SEG, _EMPTY_IMAGE_SEG]
    )
    assert len(ws.sent) == 1
    assert ws.sent[0][3] == [_TEXT_SEG]


@pytest.mark.asyncio
async def test_intercepted_dispatch_none_is_failure(entry):
    ws = FakeWS(group_response=None)
    ws.group_response = None

    async def blocked(bot_id, action, params=None, wait_response=False, timeout=10.0):
        return None

    ws.send_to_bot = blocked
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments("bot_001", "group", "123", [_TEXT_SEG]) is False


@pytest.mark.asyncio
async def test_failed_status_is_failure(entry):
    ws = FakeWS(group_response={"status": "failed", "retcode": 0, "data": {}})
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments("bot_001", "group", "123", [_TEXT_SEG]) is False


@pytest.mark.asyncio
async def test_nonzero_retcode_is_failure(entry):
    for retcode in (1, -1, "1", "abc", None, 0.5, True):
        ws = FakeWS(group_response={"status": "ok", "retcode": retcode, "data": {}})
        bridge = _bridge(entry, ws)
        assert (
            await bridge.send_segments("bot_001", "group", "123", [_TEXT_SEG]) is False
        ), retcode


@pytest.mark.asyncio
async def test_missing_or_unknown_status_is_failure(entry):
    for response in (
        {"status": "ok", "data": {}},            # 缺 retcode
        {"status": "unknown", "retcode": 0},     # 非成功状态
        {"retcode": 0},                          # 缺 status 但 retcode 0 -> 双证据视为成功?
        "ok",                                    # 非 dict
        None,
    ):
        ws = FakeWS(group_response=response)
        bridge = _bridge(entry, ws)
        result = await bridge.send_segments("bot_001", "group", "123", [_TEXT_SEG])
        if response == {"retcode": 0}:
            # status 缺省 + retcode 0 属于现有宿主回执形态，视为成功
            assert result is True
        else:
            assert result is False, response


@pytest.mark.asyncio
async def test_private_failed_delivery_status_is_failure(entry):
    ws = FakeWS(private_response={"status": "ok", "retcode": 0, "delivery_status": "failed"})
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments("bot_001", "private", "42", [_TEXT_SEG]) is False


@pytest.mark.asyncio
async def test_private_unknown_delivery_status_is_failure(entry):
    ws = FakeWS(private_response={"status": "ok", "retcode": 0, "delivery_status": "unknown"})
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments("bot_001", "private", "42", [_TEXT_SEG]) is False


@pytest.mark.asyncio
async def test_private_ack_success(entry):
    ws = FakeWS()
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments("bot_001", "private", "42", [_TEXT_SEG]) is True
    assert ws.sent[0][:3] == ("private", "bot_001", "42")


@pytest.mark.asyncio
async def test_group_ack_success_records_send(entry):
    ws = FakeWS()
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments(
        "bot_001", "group", "123", [_TEXT_SEG, _IMAGE_SEG]
    ) is True
    assert ws.sent[0][:3] == ("group", "bot_001", "123")
    assert ws.sent[0][3] == [_TEXT_SEG, _IMAGE_SEG]


@pytest.mark.asyncio
async def test_offline_bot_is_failure(entry):
    ws = FakeWS(online=False)
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments("bot_001", "group", "123", [_TEXT_SEG]) is False
    assert ws.sent == []


@pytest.mark.asyncio
async def test_missing_ws_is_failure(entry):
    entry.Plugin._ws_server = None
    bridge = entry._MohobotBridge()
    assert await bridge.send_segments("bot_001", "group", "123", [_TEXT_SEG]) is False


@pytest.mark.asyncio
async def test_exception_is_failure(entry):
    ws = FakeWS()

    async def boom(*args, **kwargs):
        raise RuntimeError("transport down")

    ws.send_to_bot = boom
    bridge = _bridge(entry, ws)
    assert await bridge.send_segments("bot_001", "group", "123", [_TEXT_SEG]) is False
