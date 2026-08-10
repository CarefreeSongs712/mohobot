"""网易云点歌插件迁移测试:
1. 命令解析: /点歌 无参/带参/别名(/music /听歌 /网易云)/不误伤("/点歌机")
2. 搜索列表返回 + 等待会话设置
3. 数字选择: 群内无需 @(on_message_observed)/私聊/消费后不落 LLM
4. 多 bot 隔离: 只有发起 bot 能处理数字选择
5. 60s 过期 + 超范围数字 + 搜索失败/无结果降级
6. 播放: 详情文本 + 封面图 + record 语音(失败降级链接)
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent, Sender


def make_group_event(user_id, text, group_id=888888):
    return GroupMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="group",
        message_id=1, user_id=user_id, group_id=group_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def make_private_event(user_id, text):
    return PrivateMessageEvent(
        time=0, self_id=1000, post_type="message", message_type="private",
        message_id=1, user_id=user_id,
        sender=Sender(user_id=user_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def load_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "neteasemusic_plugin_main", "plugins/neteasemusic/main.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 搜索 API 的 mock: 挂在插件模块的 aiohttp 上(用假 session 替换 _http)
class FakeAPI:
    """模拟 NeteaseCloudMusicApi 响应(aiohttp session.get 返回可 async with 的 CM)。"""

    def __init__(self):
        self.calls = []
        self.song_ids = [101, 202, 303, 404, 505]

    def get(self, url, **kw):
        # 与 aiohttp 一致: get() 返回可 async with 的上下文管理器(非 coroutine)
        self.calls.append(url)
        if "/search?" in url:
            resp = FakeResp(self._search())
        elif "/song/detail?" in url:
            sid = int(url.split("ids=")[1])
            resp = FakeResp(self._detail(sid))
        elif "/song/url/v1?" in url:
            sid = int(url.split("id=")[1].split("&")[0])
            resp = FakeResp({"data": [{"url": f"http://audio.example.com/{sid}.mp3"}]})
        else:
            resp = FakeResp({})
        return FakeCM(resp)

    def _search(self):
        songs = []
        for i, sid in enumerate(self.song_ids):
            songs.append({
                "id": sid,
                "name": f"测试歌曲{i + 1}",
                "artists": [{"name": "歌手A"}, {"name": "歌手B"}],
                "album": {"name": "测试专辑"},
                "duration": 215000,
            })
        return {"result": {"songs": songs}}

    def _detail(self, sid):
        return {"songs": [{
            "id": sid,
            "name": "测试歌曲",
            "ar": [{"name": "歌手A"}],
            "al": {"name": "测试专辑", "picUrl": "http://cover.example.com/x.jpg"},
            "dt": 215000,
        }]}


class FakeCM:
    """模拟 aiohttp 的请求上下文管理器(内部持有响应)。"""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class FakeResp:
    def __init__(self, data):
        self._data = data

    @property
    def status(self):
        return 200

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data

    async def read(self):
        return b"fakeimage"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeWS:
    """记录所有发送内容。"""

    def __init__(self):
        self.texts = []
        self.images = []
        self.records = []
        self.record_fail = False

    async def send_group_msg(self, bot_id, group_id, message):
        if isinstance(message, list) and message and message[0]["type"] == "record":
            if self.record_fail:
                raise RuntimeError("record not supported")
            self.records.append((bot_id, group_id, message[0]["data"]["file"]))
        else:
            self.texts.append((bot_id, group_id, message))

    async def send_private_msg(self, bot_id, user_id, message):
        if isinstance(message, list) and message and message[0]["type"] == "record":
            if self.record_fail:
                raise RuntimeError("record not supported")
            self.records.append((bot_id, user_id, message[0]["data"]["file"]))
        else:
            self.texts.append((bot_id, user_id, message))

    async def send_image(self, bot_id, chat_type, chat_id, image_path):
        self.images.append((bot_id, chat_type, chat_id, image_path))


def make_plugin():
    mod = load_plugin()
    inst = mod.Plugin()
    ws = FakeWS()
    inst._ws_server = ws
    # 替换 API 会话为 mock
    inst._http_session = FakeAPI()
    return mod, inst, ws


# ── 1. 命令解析 ─────────────────────────────────────────────

async def test_command_parse():
    mod, inst, ws = make_plugin()
    # 无参数
    handled, reply = await inst.on_message("bot_001", make_group_event(2001, "/点歌"), {})
    assert handled and "白鸟过河滩" in reply
    # 带参数(空格分隔)
    handled, reply = await inst.on_message("bot_001", make_group_event(2001, "/点歌 白鸟过河滩"), {})
    assert handled and "为您找到了 5 首歌曲" in reply
    assert "1. 测试歌曲1 - 歌手A / 歌手B 《测试专辑》 [3:35]" in reply
    # 别名
    for t in ("/music 晴天", "/听歌 晴天", "/网易云 晴天"):
        handled, _ = await inst.on_message("bot_001", make_group_event(2001, t), {})
        assert handled, t
    # 不误伤: /点歌机 不是命令
    handled, _ = await inst.on_message("bot_001", make_group_event(2001, "/点歌机"), {})
    assert not handled
    # 非命令文本
    handled, _ = await inst.on_message("bot_001", make_group_event(2001, "来一首晴天"), {})
    assert not handled, "模糊匹配应被移除"
    print("[+] 命令解析 OK")


# ── 2. 数字选择(无需 @) ───────────────────────────────────

async def test_number_selection():
    mod, inst, ws = make_plugin()
    await inst.on_message("bot_001", make_group_event(2001, "/点歌 白鸟"), {})
    assert len(inst._waiting_users) == 1

    # 群内其他成员回数字(未 @) → 消费并播放
    handled, reply = await inst.on_message_observed("bot_001", make_group_event(3002, "2"), {})
    assert handled and reply is None, "数字选择应消费消息"
    # 发送了详情文本 + 封面 + record
    texts = [t for t in ws.texts if "遵命" in str(t[2])]
    assert texts, "应发送详情文本"
    assert ws.images and ws.images[-1][1] == "group", ws.images
    assert ws.records and ws.records[-1][2] == "http://audio.example.com/202.mp3"
    # 等待会话已清除
    assert not inst._waiting_users
    print("[+] 数字选择 OK")


async def test_number_selection_private():
    mod, inst, ws = make_plugin()
    await inst.on_message("bot_001", make_private_event(2001, "/点歌 晴天"), {})
    handled, _ = await inst.on_message_observed("bot_001", make_private_event(2001, "1"), {})
    assert handled
    assert ws.records and ws.records[-1][1] == "2001"
    print("[+] 私聊数字选择 OK")


# ── 3. 多 bot 隔离 ─────────────────────────────────────────

async def test_multi_bot_isolation():
    mod, inst, ws = make_plugin()
    # bot_001 发起搜索
    await inst.on_message("bot_001", make_group_event(2001, "/点歌 晴天"), {})
    # bot_002 收到数字 → 无等待会话, 不消费
    handled, reply = await inst.on_message_observed("bot_002", make_group_event(2002, "1"), {})
    assert not handled, "非发起 bot 不应消费数字"
    # bot_001 收到数字 → 正常消费
    handled, _ = await inst.on_message_observed("bot_001", make_group_event(2002, "1"), {})
    assert handled
    print("[+] 多 bot 隔离 OK")


# ── 4. 过期 / 超范围 / 异常降级 ────────────────────────────

async def test_expire_and_errors():
    mod, inst, ws = make_plugin()
    # 过期
    await inst.on_message("bot_001", make_group_event(2001, "/点歌 晴天"), {})
    key = (bot_id := "bot_001", "group", "888888")
    inst._waiting_users[key]["expire"] = 1  # 已过期
    handled, _ = await inst.on_message_observed("bot_001", make_group_event(2002, "1"), {})
    assert not handled, "过期会话不应消费"
    assert key not in inst._waiting_users, "过期会话应被清理"

    # 超范围数字
    await inst.on_message("bot_001", make_group_event(2001, "/点歌 晴天"), {})
    handled, _ = await inst.on_message_observed("bot_001", make_group_event(2002, "9"), {})
    assert handled, "超范围数字应消费并提示"
    assert any("数字不对" in str(t[2]) for t in ws.texts)

    # 搜索 API 异常 → 降级提示
    inst._http_session = FailingAPI()
    handled, reply = await inst.on_message("bot_001", make_group_event(2001, "/点歌 晴天"), {})
    assert handled and "连接断掉" in reply

    # 无结果
    class EmptyAPI(FakeAPI):
        def _search(self):
            return {"result": {"songs": []}}

    inst._http_session = EmptyAPI()
    handled, reply = await inst.on_message("bot_001", make_group_event(2001, "/点歌 不存在"), {})
    assert handled and "没能找到" in reply
    print("[+] 过期/异常降级 OK")


class FailingAPI:
    async def get(self, url, **kw):
        raise RuntimeError("connection refused")


# ── 5. record 失败降级链接 ─────────────────────────────────

async def test_record_fallback():
    mod, inst, ws = make_plugin()
    ws.record_fail = True
    await inst.on_message("bot_001", make_group_event(2001, "/点歌 晴天"), {})
    handled, _ = await inst.on_message_observed("bot_001", make_group_event(2002, "1"), {})
    assert handled
    assert any("点击播放" in str(t[2]) for t in ws.texts), "record 失败应降级为链接"
    print("[+] record 降级 OK")


# ── 6. global_triggers 声明 ────────────────────────────────

def test_global_triggers():
    mod, _, _ = make_plugin()
    assert mod.TRIGGERS == {"/点歌", "/music", "/听歌", "/网易云"}
    assert getattr(mod.Plugin, "global_triggers") == mod.TRIGGERS, "应声明全局指令(多 bot 去重)"
    print("[+] global_triggers OK")


async def _main() -> int:
    import asyncio as _a
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if _a.iscoroutinefunction(fn):
                    await fn()
                else:
                    fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    return failed


if __name__ == "__main__":
    import asyncio
    failed = asyncio.run(_main())
    total = len([n for n in globals() if n.startswith("test_") and callable(globals()[n])])
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
