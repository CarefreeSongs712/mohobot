"""praise 点赞前即时提示回归测试。

覆盖:
1. 群聊真正点赞前先发一句即时提示, 且只由一条消息的首个 bot 发一次
2. 同一条消息的兄弟 bot 不再发提示, 但仍各自点赞并返回原有汇报
3. 节流/当日上限命中的路径不发提示(没有真正点赞)
4. 私聊走 send_private_msg
5. pending_msg 置空可关闭提示
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PENDING = "正在点赞中，一次点赞就是一天的量，请勿重复点赞"


def _load_plugin():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "praise_ack_test", Path(__file__).resolve().parent.parent / "plugins" / "praise" / "main.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Plugin


def _reset(Plugin):
    Plugin._throttle_cache.clear()
    Plugin._event_owner_cache.clear()
    Plugin._like_limit_cache.clear()


def _group_event(user_id, text, message_id=101, group_id=888, at_time=1000):
    from mohobot.models.onebot import GroupMessageEvent, Sender

    return GroupMessageEvent(
        time=at_time, self_id=1000, post_type="message", message_type="group",
        message_id=message_id, user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def _private_event(user_id, text, message_id=201, at_time=1000):
    from mohobot.models.onebot import PrivateMessageEvent, Sender

    return PrivateMessageEvent(
        time=at_time, self_id=1000, post_type="message", message_id=message_id,
        user_id=user_id, sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def _raw(event, message_id, at_time=1000):
    """模拟 OneBot 原始事件; message_id 按账号分配, 兄弟 bot 可不同。"""
    payload = {
        "message_id": message_id,
        "time": at_time,
        "user_id": event.user_id,
        "message": event.message,
    }
    if getattr(event, "group_id", None):
        payload["group_id"] = event.group_id
    return payload


class RecordingWS:
    def __init__(self):
        self.order = []

    async def send_group_msg(self, bot_id, group_id, message):
        self.order.append(("notice_group", bot_id, int(group_id), message))

    async def send_private_msg(self, bot_id, user_id, message):
        self.order.append(("notice_private", bot_id, int(user_id), message))

    async def send_to_bot(self, bot_id, action, params, wait_response=False, timeout=5.0):
        self.order.append(("like", bot_id, params["user_id"], params.get("times")))
        return {"status": "ok", "retcode": 0, "data": {}}


async def test_group_notice_once_before_likes():
    Plugin = _load_plugin()
    _reset(Plugin)
    ws = RecordingWS()
    Plugin._ws_server = ws
    inst = Plugin()
    inst.plugin_config = {"like_total": 10, "like_times_per_call": 10,
                          "success_msg": "✅ {count}"}

    ev = _group_event(2001, "/赞我", message_id=101, at_time=1000)
    handled, reply = await inst.on_message("bot_001", ev, _raw(ev, 101))
    assert handled and reply == "✅ 10", reply
    # 提示在第一批点赞之前
    assert ws.order[0][0] == "notice_group", ws.order
    assert ws.order[0][1] == "bot_001" and ws.order[0][2] == 888
    assert ws.order[0][3] == PENDING
    assert [c[0] for c in ws.order[1:]] == ["like"], ws.order

    # 同一条消息的兄弟 bot: message_id 按账号不同, 但内容/时间相同 →
    # 不重复提示, 仍点赞并保留原有汇报。
    before = len(ws.order)
    sibling = _group_event(2001, "/赞我", message_id=222, at_time=1000)
    handled2, reply2 = await inst.on_message("bot_002", sibling, _raw(sibling, 222))
    assert handled2 and reply2 == "✅ 10", reply2
    new_calls = ws.order[before:]
    assert [c[0] for c in new_calls] == ["like"], new_calls


async def test_throttle_and_limit_paths_skip_notice():
    Plugin = _load_plugin()
    _reset(Plugin)
    ws = RecordingWS()
    Plugin._ws_server = ws
    inst = Plugin()
    inst.plugin_config = {"like_total": 10, "like_times_per_call": 10}

    # 首条消息正常: 发提示 + 点赞
    first = _group_event(2001, "/赞我", message_id=101, at_time=1000)
    await inst.on_message("bot_001", first, _raw(first, 101))
    assert ws.order and ws.order[0][0] == "notice_group"

    # 60 秒内新消息(内容相同但时间不同): 节流, 不点赞也不发提示
    before = len(ws.order)
    second = _group_event(2001, "/赞我", message_id=102, at_time=1061)
    handled, reply = await inst.on_message("bot_001", second, _raw(second, 102, 1061))
    assert handled and "不需要发送多次" in reply, reply
    assert len(ws.order) == before, ws.order[before:]

    # 当日上限缓存命中: 同样不发提示
    from mohobot.utils.time_utils import format_utc8
    Plugin._throttle_cache.clear()
    Plugin._like_limit_cache["bot_001:2001"] = format_utc8("%Y-%m-%d")
    before = len(ws.order)
    third = _group_event(2001, "/赞我", message_id=103, at_time=2000)
    handled, reply = await inst.on_message("bot_001", third, _raw(third, 103, 2000))
    assert handled and "上限" in reply, reply
    assert len(ws.order) == before, ws.order[before:]


async def test_private_notice_and_disable():
    Plugin = _load_plugin()
    _reset(Plugin)
    ws = RecordingWS()
    Plugin._ws_server = ws
    inst = Plugin()
    inst.plugin_config = {"like_total": 10, "like_times_per_call": 10}

    ev = _private_event(2001, "赞我", message_id=201, at_time=1000)
    await inst.on_message("bot_001", ev, _raw(ev, 201))
    assert ws.order[0] == ("notice_private", "bot_001", 2001, PENDING), ws.order
    assert ws.order[1][0] == "like"

    # pending_msg 置空: 不提示, 但点赞照常
    _reset(Plugin)
    ws.order.clear()
    inst.plugin_config["pending_msg"] = ""
    ev2 = _private_event(2002, "赞我", message_id=202, at_time=1100)
    await inst.on_message("bot_001", ev2, _raw(ev2, 202, 1100))
    assert [c[0] for c in ws.order] == ["like"], ws.order


async def test_default_pending_text():
    assert "正在点赞中" in PENDING and "请勿重复点赞" in PENDING


async def main():
    await test_group_notice_once_before_likes()
    await test_throttle_and_limit_paths_skip_notice()
    await test_private_notice_and_disable()
    await test_default_pending_text()
    print("ALL PASSED")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
