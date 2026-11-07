"""ctx_restore 普通单条引用回退回归（PR #110 复审 P2/N2）。

- 非合并转发（普通单条引用）→ 回退为单个 node（不再 UnboundLocalError）
- 畸形 sender（非 dict）→ 不得抛 AttributeError，用空 sender 兜底
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "ctx_restore" / "main.py"


def _load_plugin_class():
    spec = importlib.util.spec_from_file_location("ctx_restore_plugin_test", _PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Plugin


class _FakeWS:
    def __init__(self, data):
        self._data = data

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=None):
        return {"status": "ok", "retcode": 0, "data": self._data}


async def _fetch(data):
    Plugin = _load_plugin_class()
    Plugin._ws_server = _FakeWS(data)
    p = Plugin()
    return await p._fetch_forward("bot_001", "123")


async def test_plain_quote_falls_back_to_single_node() -> None:
    data = {
        "message": [{"type": "text", "data": {"text": "被引用的普通消息"}}],
        "sender": {"user_id": 42, "nickname": "甲", "card": "甲卡"},
    }
    nodes = await _fetch(data)
    assert isinstance(nodes, list) and len(nodes) == 1, nodes
    node = nodes[0]
    assert node["sender"]["user_id"] == 42
    assert node["sender"]["nickname"] == "甲卡"
    assert node["message"] == data["message"]


async def test_malformed_sender_does_not_raise() -> None:
    data = {
        "message": [{"type": "text", "data": {"text": "被引用"}}],
        "sender": "bad",  # 非 dict，协议异常/测试替身
    }
    nodes = await _fetch(data)
    assert isinstance(nodes, list) and len(nodes) == 1, nodes
    assert nodes[0]["sender"]["user_id"] is None, nodes[0]
    assert nodes[0]["sender"]["nickname"] is None, nodes[0]
    assert nodes[0]["message"] == data["message"]


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_plain_quote_falls_back_to_single_node())
    asyncio.run(test_malformed_sender_does_not_raise())
    print("ALL CTX_RESTORE FALLBACK TESTS PASSED")
