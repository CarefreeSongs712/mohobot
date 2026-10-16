"""Regression for /查看聊天 with partial merged-forward failures.

When at least one batch succeeds and at least one fails, cmd_view_chat formatted
``len(nodes)`` even though ``nodes`` is undefined in that scope, raising NameError
instead of returning the intended partial-failure summary.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PLUGIN = ROOT / "plugins" / "chat_manager"
sys.path.insert(0, str(PLUGIN))
spec = importlib.util.spec_from_file_location("chat_manager_partial_failure", PLUGIN / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class _WS:
    async def send_to_bot(self, *args, **kwargs):
        return {"status": "ok", "retcode": 0, "data": []}


def _event(text):
    from mohobot.models.onebot import GroupMessageEvent
    return GroupMessageEvent.from_dict({
        "time": 1, "self_id": 111, "post_type": "message", "message_type": "group",
        "sub_type": "normal", "message_id": 1, "group_id": 999001,
        "user_id": 10001, "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text, "sender": {"user_id": 10001, "nickname": "admin"},
    })


async def test_partial_batch_failure_returns_summary_without_name_error():
    with TemporaryDirectory() as td:
        module.Plugin.inject_data_dir(td)
        module.Plugin.inject_ws_server(_WS())
        module.Plugin.inject_admin_ids(["10001"])
        plugin = module.Plugin()
        target = module.Target("group", "123456")
        module.resolve_by_history = lambda *args: target
        messages = [{"type": "node", "data": {"content": str(i)}} for i in range(3)]
        async def fake_read_recent(*args, **kwargs):
            return messages
        async def partial_forward(*args, **kwargs):
            return 2, 1
        module.read_recent = fake_read_recent
        module.forward_messages = partial_forward

        handled, reply = await plugin.on_message("bot_001", _event("/查看聊天 123456 3"), {})

    assert handled is True
    # 不得把"读取条数"说成"已转发条数": 只报批次 + 读取条数, 并提示失败批次可能未送达
    assert "成功 2 批 / 失败 1 批" in reply, reply
    assert "本次读取 3 条" in reply, reply
    assert "未送达" in reply, reply
    assert "已转发 3 条" not in reply, reply


def main():
    asyncio.run(test_partial_batch_failure_returns_summary_without_name_error())
    print("ALL CHAT MANAGER PARTIAL FAILURE TESTS PASSED")


if __name__ == "__main__":
    main()
