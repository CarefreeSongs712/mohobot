"""QQ空间说说插件(qzone)回归测试。

覆盖: 序号/范围解析、@ 提取、评论/回评参数解析、响应解析(json 降级)、
g_tk 计算、ApiResponse、per-bot 会话 cookies 获取/缓存/重置、插件命令路由
(管理员命令群聊忽略)与插件配置 float 强转。
"""

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.interceptors.plugin_system import PluginSystem  # noqa: E402
from mohobot.models.onebot import (  # noqa: E402
    GroupMessageEvent,
    PrivateMessageEvent,
    Sender,
)

_REPO = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _REPO / "plugins" / "qzone"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from qzone_core.model import Comment, Post  # noqa: E402
from qzone_core.qzone.model import ApiResponse, QzoneContext  # noqa: E402
from qzone_core.qzone.parser import QzoneParser  # noqa: E402
from qzone_core.qzone.session import QzoneSession  # noqa: E402
from qzone_core.utils import (  # noqa: E402
    extract_at_ids,
    parse_comment_args,
    parse_range,
    parse_range_from_tokens,
    parse_reply_args,
)


def _load_plugin_class():
    spec = importlib.util.spec_from_file_location(
        "mohobot_plugin_qzone", _PLUGIN_DIR / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Plugin


def _group_event(message, user_id=111, group_id=222):
    return GroupMessageEvent(
        time=0, self_id=0, post_type="message", message_type="group",
        user_id=user_id, group_id=group_id, message=message, raw_message="",
        sender=Sender(user_id=user_id),
    )


def _private_event(message, user_id=111):
    return PrivateMessageEvent(
        time=0, self_id=0, post_type="message", message_type="private",
        sub_type="friend", user_id=user_id, message=message, raw_message="",
        sender=Sender(user_id=user_id),
    )


# ── 序号/范围解析 ─────────────────────────────────────────────


def test_parse_range_single():
    assert parse_range_from_tokens(["看说说", "0"]) == (0, 1, 1)
    assert parse_range_from_tokens(["1"]) == (0, 1, 0)
    assert parse_range_from_tokens(["3"]) == (2, 1, 0)


def test_parse_range_span():
    assert parse_range_from_tokens(["2~4"]) == (1, 3, 0)
    assert parse_range_from_tokens(["0~3"]) == (0, 4, 0)


def test_parse_range_not_found():
    assert parse_range_from_tokens(["看说说", "abc"]) == (0, 1, None)


def test_parse_range_reversed_skipped():
    # 4~2 非法 → 跳过继续找
    assert parse_range_from_tokens(["4~2", "2"]) == (1, 1, 1)


def test_parse_range_full_text():
    assert parse_range("/看说说 0~2") == (0, 3)
    assert parse_range("/看说说") == (0, 1)


# ── @ 提取 ────────────────────────────────────────────────────


def test_extract_at_ids_segments():
    msg = [
        {"type": "text", "data": {"text": "看说说 "}},
        {"type": "at", "data": {"qq": "123"}},
        {"type": "at", "data": {"qq": "all"}},
        {"type": "at", "data": {"qq": "999"}},
    ]
    assert extract_at_ids(msg, bot_qq="999") == ["123"]


def test_extract_at_ids_text_tokens():
    msg = [{"type": "text", "data": {"text": "看说说 @456 第0条"}}]
    assert extract_at_ids(msg, bot_qq="999") == ["456"]


# ── 评论/回评参数 ─────────────────────────────────────────────


def test_parse_comment_args():
    target, pos, num, content = parse_comment_args(
        "/评说说 @123 0 路过 看看", ["123"]
    )
    assert target == "123"
    assert (pos, num) == (0, 1)
    assert content == "路过 看看"


def test_parse_comment_args_no_content():
    target, pos, num, content = parse_comment_args("/评说说 2", [])
    assert target is None
    assert (pos, num) == (1, 1)
    assert content == ""


def test_parse_reply_args():
    target, pos, idx, content = parse_reply_args("/回评 0 2 谢谢", [])
    assert target is None
    assert pos == 0
    assert idx == 2
    assert content == "谢谢"


def test_parse_reply_args_negative_index():
    target, pos, idx, content = parse_reply_args("/回评 0 -1 好耶", [])
    assert idx == -1
    assert content == "好耶"


# ── 响应解析 ──────────────────────────────────────────────────


def test_parse_response_empty():
    data = QzoneParser.parse_response("")
    assert data["code"] == -9999
    assert data["message"] == "empty_response"


def test_parse_response_jsonp_wrapper():
    data = QzoneParser.parse_response('_preloadCallback({"code":0,"msglist":[{"tid":1}]})')
    assert data["code"] == 0
    assert data["msglist"][0]["tid"] == 1


def test_parse_response_undefined_to_null():
    data = QzoneParser.parse_response('{"code":0,"name":undefined}')
    assert data["code"] == 0
    assert data["name"] is None


def test_parse_response_invalid():
    data = QzoneParser.parse_response("<html>not json</html>")
    assert data["code"] == -9999
    assert data["message"] in ("invalid_response", "json_parse_error")


def test_parse_feeds_basic():
    msglist = [{
        "tid": 42, "uin": 123, "name": "测试", "content": "你好",
        "created_time": 1700000000,
        "pic": [{"url3": "http://img/3"}],
        "commentlist": [
            {"tid": 7, "uin": 555, "name": "路人", "content": "赞",
             "create_time": 1700000100, "list_3": [
                 {"tid": 8, "uin": 556, "name": "乙", "content": "[em]e1[/em]同意",
                  "create_time": 1700000200}]},
        ],
    }]
    posts = QzoneParser.parse_feeds(msglist)
    assert len(posts) == 1
    post = posts[0]
    assert post.tid == "42" and post.uin == 123
    assert post.images == ["http://img/3"]
    assert len(post.comments) == 2
    assert post.comments[1].parent_tid == 7
    text = post.to_str()
    assert "测试(123)" in text and "0. 路人" in text


# ── g_tk 与 ApiResponse ───────────────────────────────────────


def test_qzone_context_gtk2():
    ctx = QzoneContext(uin=1, skey="x", p_skey="@")
    assert ctx.gtk2 == "177637"


def test_api_response_ok_and_error():
    ok = ApiResponse.from_raw({"code": 0, "msglist": [{"tid": 1}], "extra": 2})
    assert ok.ok and ok.data["msglist"][0]["tid"] == 1
    err = ApiResponse.from_raw({"code": -3000, "message": "请先登录"})
    assert not err.ok and err.message == "请先登录"
    ret = ApiResponse.from_raw({"ret": 0, "msg": "done"}, code_key="ret", msg_key="msg")
    assert ret.ok and ret.message is None  # ok 路径 message 置空(原版行为)


# ── per-bot 会话 ──────────────────────────────────────────────


class _FakeWS:
    def __init__(self, resp):
        self.resp = resp
        self.calls: list[tuple] = []
        self._bot_manager = None

    async def send_to_bot(self, bot_id, action, params=None, wait_response=False, timeout=10.0):
        self.calls.append((bot_id, action, params))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def _cookie_resp(uin=123456):
    return {"status": "ok", "retcode": 0,
            "data": {"cookies": f"uin=o{uin}; skey=abc; p_skey=xyz"}}


def test_session_fetch_and_cache():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            ws = _FakeWS(_cookie_resp(123456))
            sess = QzoneSession(bot_id="bot_001", ws_server=ws, data_dir=td,
                                cfg_get=lambda k, d: d)
            ctx = await sess.get_ctx()
            assert ctx.uin == 123456
            assert ws.calls and ws.calls[0][1] == "get_cookies"
            cache = Path(td) / "plugins_data" / "qzone" / "cookies_bot_001.json"
            assert cache.exists()

            # 协议端不可用时复用缓存
            ws2 = _FakeWS(RuntimeError("offline"))
            sess2 = QzoneSession(bot_id="bot_001", ws_server=ws2, data_dir=td,
                                 cfg_get=lambda k, d: d)
            ctx2 = await sess2.get_ctx()
            assert ctx2.uin == 123456
            assert not ws2.calls
    return run()


def test_session_reset_clears_cache():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            ws = _FakeWS(_cookie_resp(123456))
            sess = QzoneSession(bot_id="bot_001", ws_server=ws, data_dir=td,
                                cfg_get=lambda k, d: d)
            await sess.get_ctx()
            await sess.reset_login_state(clear_cookies=True)
            cache = Path(td) / "plugins_data" / "qzone" / "cookies_bot_001.json"
            assert not cache.exists()
            # 重新获取需要协议端
            ws.resp = {"status": "ok", "retcode": 0, "data": {}}
            try:
                await sess.get_ctx()
                raise AssertionError("应当因空 Cookies 抛错")
            except RuntimeError as e:
                assert "Cookies" in str(e)
    return run()


def test_session_invalid_uin():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            ws = _FakeWS({"status": "ok", "retcode": 0,
                          "data": {"cookies": "skey=abc"}})
            sess = QzoneSession(bot_id="bot_001", ws_server=ws, data_dir=td,
                                cfg_get=lambda k, d: d)
            try:
                await sess.get_ctx()
                raise AssertionError("应当因缺少 uin 抛错")
            except RuntimeError as e:
                assert "uin" in str(e)
    return run()


def test_session_no_o_prefix_uin():
    """uin cookie 无 o 前缀时不应砍掉首位数字(原版 [1:] 的缺陷)。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            ws = _FakeWS({"status": "ok", "retcode": 0,
                          "data": {"cookies": "uin=987654; skey=a; p_skey=b"}})
            sess = QzoneSession(bot_id="bot_001", ws_server=ws, data_dir=td,
                                cfg_get=lambda k, d: d)
            ctx = await sess.get_ctx()
            assert ctx.uin == 987654
    return run()


# ── 插件命令路由 ──────────────────────────────────────────────


def _fresh_plugin(admins=(3831097597,)):
    plugin_cls = _load_plugin_class()
    plugin_cls._admin_ids = [str(a) for a in admins]
    plugin_cls._ws_server = None
    plugin_cls._llm_service = None
    plugin_cls._data_dir = tempfile.mkdtemp()
    return plugin_cls()


def test_plugin_class_shape():
    plugin = _fresh_plugin()
    assert callable(plugin.on_message)
    assert callable(plugin.on_shutdown)
    assert "/看说说" in plugin.global_triggers
    assert plugin._COMMANDS["看说说"] == ("view", True)
    assert plugin._COMMANDS["评说说"] == ("comment", True)
    assert plugin._COMMANDS["赞说说"] == ("like", True)
    assert plugin._COMMANDS["发说说"] == ("publish", True)
    assert plugin._COMMANDS["重置qqcookies"] == ("reset_cookies", True)


async def test_plugin_admin_command_ignored_in_group():
    plugin = _fresh_plugin()
    ev = _group_event([{"type": "text", "data": {"text": "/发说说 你好"}}])
    handled, reply = await plugin.on_message("bot_001", ev, {})
    assert handled is True and reply is None  # 群聊静默忽略


async def test_plugin_admin_command_private_non_admin():
    plugin = _fresh_plugin(admins=())
    ev = _private_event([{"type": "text", "data": {"text": "/发说说 你好"}}], user_id=111)
    handled, reply = await plugin.on_message("bot_001", ev, {})
    assert handled is True and reply and "管理员" in reply


async def test_plugin_publish_empty_content_private_admin():
    plugin = _fresh_plugin()
    ev = _private_event([{"type": "text", "data": {"text": "/发说说"}}], user_id=3831097597)
    handled, reply = await plugin.on_message("bot_001", ev, {})
    assert handled is True and reply and "请提供说说内容" in reply


async def test_plugin_unknown_command_passthrough():
    plugin = _fresh_plugin()
    ev = _private_event([{"type": "text", "data": {"text": "/不存在的命令"}}])
    handled, reply = await plugin.on_message("bot_001", ev, {})
    assert handled is False


async def test_plugin_reset_cookies_route():
    """管理员私聊重置 cookies: 未创建 API 实例时也不应报错。"""
    plugin = _fresh_plugin()
    ev = _private_event([{"type": "text", "data": {"text": "/重置QQCookies"}}],
                        user_id=3831097597)
    handled, reply = await plugin.on_message("bot_001", ev, {})
    assert handled is True and reply and "已重置" in reply


async def test_plugin_on_shutdown_empty():
    plugin = _fresh_plugin()
    await plugin.on_shutdown()  # 无实例时安全


# ── 插件配置 float 强转 ───────────────────────────────────────


def test_config_float_coercion():
    schema = {"request_interval": {"type": "float", "default": 0.8}}
    assert PluginSystem._coerce_config(schema, {"request_interval": "1.5"}) == {"request_interval": 1.5}
    assert PluginSystem._coerce_config(schema, {"request_interval": "abc"}) == {"request_interval": 0.8}
    assert PluginSystem._coerce_config(schema, {"request_interval": 2}) == {"request_interval": 2.0}
    assert PluginSystem._schema_defaults(schema) == {"request_interval": 0.8}


def test_post_model_copy_and_comments():
    post = Post(uin=1, name="a", text="hi", comments=[Comment(uin=2, nickname="b", content="c", create_time=0)])
    copied = post.model_copy(deep=True)
    copied.comments[0].content = "changed"
    assert post.comments[0].content == "c"


async def main() -> None:
    fns = [(n, getattr(sys.modules[__name__], n)) for n in sorted(dir(sys.modules[__name__]))
           if n.startswith("test_") and callable(getattr(sys.modules[__name__], n))]
    for name, fn in fns:
        result = fn()
        if asyncio.iscoroutine(result):
            await result
        print(f"PASS {name}")
    print("\nALL QZONE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
