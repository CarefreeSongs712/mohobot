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



# ══ 自动回复(被@ / 自己说说被评论) ══════════════════════════════

from qzone_core.auto_reply import (  # noqa: E402
    AutoReplyStore,
    clean_reply_text,
    content_key,
    render_reply_prompt,
)


async def test_auto_reply_store_dedup_and_baseline():
    with tempfile.TemporaryDirectory() as td:
        store = AutoReplyStore(Path(td) / "auto_reply_bot_001.json")
        assert not await store.is_seen("k1")
        assert not await store.is_baselined()
        await store.mark_seen("k1")
        await store.mark_seen("k1")  # 幂等
        await store.set_baselined()
        await store.save()

        # 重新加载(新实例) 验证持久化
        store2 = AutoReplyStore(Path(td) / "auto_reply_bot_001.json")
        assert await store2.is_seen("k1")
        assert await store2.is_baselined()
        assert not await store2.is_seen("k2")


async def test_auto_reply_store_trim():
    with tempfile.TemporaryDirectory() as td:
        store = AutoReplyStore(Path(td) / "trim.json")
        for i in range(1100):
            await store.mark_seen(f"k{i}")
        await store.save()
        store2 = AutoReplyStore(Path(td) / "trim.json")
        assert not await store2.is_seen("k0")       # 最旧的被裁剪
        assert await store2.is_seen("k1099")


def test_content_key_stable():
    assert content_key(1, "abc", 123) == content_key(1, "abc", 123)
    assert content_key(1, "abc") != content_key(1, "abd")


def test_render_reply_prompt():
    text = render_reply_prompt(
        "好友 {nick} 说: {content} 说说: {post}",
        nick="小明", content="你好", post="今天真开心",
    )
    assert text == "好友 小明 说: 你好 说说: 今天真开心"


def test_clean_reply_text():
    assert clean_reply_text('"你好呀。"', 60) == "你好呀"
    assert clean_reply_text("  多  行\n文本  ", 60) == "多 行 文本"
    assert len(clean_reply_text("x" * 200, 60)) == 60
    assert clean_reply_text("", 60) == ""


def test_plugin_comment_key():
    plugin = _fresh_plugin()
    post = Post(uin=111, tid="42", name="a", text="hi")
    c1 = Comment(uin=222, nickname="b", content="赞", create_time=100, tid=7)
    c2 = Comment(uin=222, nickname="b", content="赞", create_time=100, tid=0)
    assert plugin._comment_key(post, c1) == "selfc:111:42:222:7"
    # tid 缺失 → 时间+内容哈希兜底
    k2 = plugin._comment_key(post, c2)
    assert k2.startswith("selfc:111:42:222:") and "selfc:111:42:222:0" != k2


def test_plugin_atme_feeds_candidates():
    plugin = _fresh_plugin()
    own = Post(uin=111, tid="1", name="me", text=f"@meBot 你好")
    other = Post(uin=222, tid="2", name="friend", text=f"来玩 @meBot")
    plain = Post(uin=333, tid="3", name="c", text="没有@")
    botpost = Post(uin=999, tid="5", name="bot", text=f"@meBot 别的bot")
    commented = Post(
        uin=444, tid="4", name="d", text="随便说说",
        comments=[
            Comment(uin=555, nickname="e", content=f"@meBot 带我一个", create_time=1, tid=9),
            Comment(uin=111, nickname="me", content=f"@meBot 自己", create_time=2, tid=8),
            Comment(uin=999, nickname="bot", content=f"@meBot bot评论", create_time=3, tid=7),
        ],
    )
    cands = plugin._atme_feeds_candidates(
        [own, other, plain, botpost, commented], "@meBot", 111, bot_qqs={999},
    )
    # 自己的说说/其它 bot 的说说与评论均跳过; 只剩 他人正文1条 + 他人评论1条
    assert len(cands) == 2
    keys = [k for _, _, k in cands]
    # 去重 key 统一按帖子(一次唤醒一次), 与 api 模式同空间
    assert keys[0] == "atme:222:2"
    assert keys[1] == "atme:444:4"


async def test_plugin_post_reply_limit():
    plugin = _fresh_plugin()
    with tempfile.TemporaryDirectory() as td:
        from qzone_core.auto_reply import AutoReplyStore
        store = AutoReplyStore(Path(td) / "limit.json")
        plugin.plugin_config["max_replies_per_post"] = 3
        assert not await plugin._post_limit_reached(store, 1, "t")
        for _ in range(3):
            await store.incr_post_reply(plugin._post_key(1, "t"))
        assert await plugin._post_limit_reached(store, 1, "t")
        # 其它说说不受影响; 0=不限
        assert not await plugin._post_limit_reached(store, 2, "u")
        plugin.plugin_config["max_replies_per_post"] = 0
        assert not await plugin._post_limit_reached(store, 1, "t")


async def test_plugin_actor_blocked():
    with tempfile.TemporaryDirectory() as td:
        plugin = _fresh_plugin()
        plugin._data_dir = td
        # 伪造全局封禁名单(banall_list.json)
        import json as _json
        ban_dir = Path(td) / "ban"
        ban_dir.mkdir(parents=True)
        (ban_dir / "banall_list.json").write_text(
            _json.dumps([{"uid": "555", "time": 0, "reason": "捣乱"}], ensure_ascii=False),
            encoding="utf-8",
        )
        # 普通 QQ: 不拦截
        blocked, why = await plugin._actor_blocked(123456)
        assert not blocked
        # 封禁用户: 拦截
        blocked, why = await plugin._actor_blocked(555)
        assert blocked and "封禁" in why
        # bot 之间不互动(需要 ws 注入)
        class _BM:
            all_bots = [type("B", (), {"qq": 2192362623})(), type("B", (), {"qq": 3831097597})()]
        class _WS:
            _bot_manager = _BM()
        plugin._ws_server = _WS()
        blocked, why = await plugin._actor_blocked(2192362623)
        assert blocked and "bot" in why


async def test_plugin_tick_disabled_noop():
    plugin = _fresh_plugin()
    plugin.plugin_config["comment_reply_enabled"] = False
    plugin.plugin_config["atme_reply_enabled"] = False
    await plugin.on_tick()  # 未启用: 直接返回, 不需 ws


def test_plugin_interval_property():
    plugin = _fresh_plugin()
    plugin.plugin_config["scan_interval_sec"] = 30
    assert plugin.interval_sec == 60  # 下限 60
    plugin.plugin_config["scan_interval_sec"] = 600
    assert plugin.interval_sec == 600


async def test_plugin_generate_auto_text_fallback():
    plugin = _fresh_plugin()
    plugin._llm_service = None  # 无 LLM → 兜底文案
    post = Post(uin=1, tid="1", name="a", text="hi")
    text = await plugin._generate_auto_text("bot_001", "小明", "你好", post)
    assert text  # 非空
    assert len(text) <= 60


async def test_plugin_generate_auto_text_llm():
    class _FakeLLM:
        async def complete_text(self, prompt, **kw):
            assert "小明" in prompt
            return '"这是一条生成的回复呀。"'
    plugin = _fresh_plugin()
    plugin._llm_service = _FakeLLM()
    post = Post(uin=1, tid="1", name="a", text="hi")
    text = await plugin._generate_auto_text("bot_001", "小明", "你好", post)
    assert text == "这是一条生成的回复呀"


def test_parse_atme_items_shapes():
    from qzone_core.qzone.api import parse_atme_items
    # 实测形态: data.data 条目数组, html 含动作文本与 mood 链接
    items = parse_atme_items({"data": {"data": [
        {"uin": "1936995136", "nickname": "依伴", "appid": "403", "abstime": "1790314429",
         "html": '<a href="http://user.qzone.qq.com/1936995136">依伴</a> 访问了我的主页 13:33'},
        {"uin": "2644672227", "nickname": "都督白忧", "appid": "217", "abstime": "1790313937",
         "html": '<a href="http://user.qzone.qq.com/2644672227">都督白忧</a> 赞了我的说说 '
                 '<a href="http://user.qzone.qq.com/3831097597/mood/fde859e4005aa96aed5b0300.1">…</a>'},
        # 实测被@条目: 动作文本"提到我"(正文@)/"评论提到我"(评论@), tid 以 "." 结尾
        {"uin": "3831097597", "nickname": "墨染荷韵", "appid": "311", "abstime": "1790354977",
         "html": '<a href="http://user.qzone.qq.com/3831097597">墨染荷韵</a> 提到我 00:50 '
                 '<a href="http://user.qzone.qq.com/3831097597/mood/fde859e461a6b66ac2ab0100.">说说</a> '
                 '@洛天依bot-5 晚安'},
        {"uin": "3831097597", "nickname": "墨染荷韵", "appid": "311", "abstime": "1790354978",
         "html": '<a href="http://user.qzone.qq.com/3831097597">墨染荷韵</a> 评论提到我 00:51 '
                 '<a href="http://user.qzone.qq.com/3831097597/mood/fde859e461a6b66ac2ab0100.">说说</a> '
                 '评论里 @洛天依bot-5'},
        # 线程普通"回复"条目: 正文预览含 @ 但不是被@
        {"uin": "3900533653", "nickname": "洛水天依", "appid": "311", "abstime": "1790355168",
         "html": '<a href="http://user.qzone.qq.com/3900533653">洛水天依</a> 回复 00:52 '
                 '<a href="http://user.qzone.qq.com/3831097597/mood/fde859e461a6b66ac2ab0100.">说说</a> '
                 '@洛天依bot-5 晚安 洛水天依 : 666'},
    ]}})
    assert items[0]["post_tid"] is None  # 访问主页无 mood 链接
    assert items[0]["action"] == "other"
    # 点赞条目: tid 截去评论锚点后缀 .1
    assert items[1]["post_uin"] == "3831097597"
    assert items[1]["post_tid"] == "fde859e4005aa96aed5b0300"
    assert items[1]["action"] == "other"
    # 被@条目: tid 以点结尾也归一化为基础 tid(否则 get_detail 报 -8 原文已删除)
    assert items[2]["post_tid"] == "fde859e461a6b66ac2ab0100"
    assert items[2]["action"] == "mention"
    # 评论中@ → comment_mention
    assert items[3]["action"] == "comment_mention"
    assert items[3]["post_tid"] == "fde859e461a6b66ac2ab0100"
    # 线程"回复"条目不是被@(即使正文预览含 @)
    assert items[4]["action"] == "other"
    # 空/异形响应
    assert parse_atme_items({}) == []
    assert parse_atme_items({"data": {"data": [None, "x"]}}) == []


def test_find_mention_comment():
    plugin = _fresh_plugin()
    post = Post(uin=222, tid="t1", name="a", text="post", comments=[
        Comment(uin=3831097597, nickname="墨染荷韵", content="路过", create_time=1, tid=11),
        Comment(uin=3831097597, nickname="墨染荷韵", content="@洛天依bot-5 晚安", create_time=2, tid=12),
        Comment(uin=3831097597, nickname="墨染荷韵", content="又路过", create_time=3, tid=13),
    ])
    # 定位该用户最新一条含 @ 的评论
    idx = plugin._find_mention_comment(post, 3831097597, "洛天依bot-5")
    assert idx == 1
    # 昵称匹配兜底
    post.comments[1].content = "洛天依bot-5 你好"
    assert plugin._find_mention_comment(post, 3831097597, "洛天依bot-5") == 1
    # 无 @ 评论 → None
    post2 = Post(uin=222, tid="t2", name="a", comments=[
        Comment(uin=3831097597, nickname="x", content="来看看不说话", create_time=1, tid=21),
    ])
    assert plugin._find_mention_comment(post2, 3831097597, "洛天依bot-5") is None


async def test_plugin_atme_debug():
    plugin = _fresh_plugin()
    ev = _private_event([{"type": "text", "data": {"text": "/与我相关"}}], user_id=3831097597)
    # 未登录(无注入)时报错但命令已消费
    handled, reply = await plugin.on_message("bot_001", ev, {})
    assert handled is True


async def test_plugin_atme_api_filter():
    """api 模式: 只处理"提到我/评论提到我"; 正文@→评论说说, 评论@→回评该评论。"""
    plugin = _fresh_plugin()
    plugin.plugin_config["atme_mode"] = "api"
    plugin.plugin_config["atme_reply_enabled"] = True

    class _FakeAPI:
        def __init__(self, items, comments=None):
            self._items = items
            self._comments = comments or []
            self.atme_calls = 0
            self.details = []
            self.session = None

        async def get_atme_list(self):
            self.atme_calls += 1
            return {"data": {"data": self._items}}

        async def get_detail(self, post):
            self.details.append((post.uin, post.tid))
            from qzone_core.model import Post as P
            return type("R", (), {"ok": True, "data": {
                "tid": post.tid, "uin": post.uin, "content": "带@的内容",
                "commentlist": self._comments,
            }})()

    class _FakeSession:
        async def get_uin(self):
            return 111

    from qzone_core.model import Comment
    # 实测动作文案: "提到我"=正文@, "评论提到我"=评论中@
    mention_html = ('<a href="http://user.qzone.qq.com/222">小明</a> 提到我 '
                    '<a href="http://user.qzone.qq.com/222/mood/abc111.">说说</a> @墨染荷韵 来玩')
    cmention_html = ('<a href="http://user.qzone.qq.com/333">阿三</a> 评论提到我 '
                     '<a href="http://user.qzone.qq.com/333/mood/def222.">说说</a> '
                     '评论里 @墨染荷韵 看看')
    like_html = ('<a href="http://user.qzone.qq.com/444">老四</a> 赞了我的说说 '
                 '<a href="http://user.qzone.qq.com/111/mood/ghi333.1">…</a>')
    reply_html = ('<a href="http://user.qzone.qq.com/445">老五</a> 回复 00:52 '
                  '<a href="http://user.qzone.qq.com/222/mood/abc111.">说说</a> '
                  '@墨染荷韵 晚安 洛水天依 : 666')
    own_mention_html = ('<a href="http://user.qzone.qq.com/555">老六</a> 提到我 '
                        '<a href="http://user.qzone.qq.com/111/mood/jkl444.">说说</a> @墨染荷韵')

    # def222 的详情里: 阿三(333)的最新 @ 评论是 tid=7 那条(应被回评)
    detail_comments = [
        {"tid": 6, "uin": "333", "name": "阿三", "content": "普通评论", "create_time": 100},
        {"tid": 7, "uin": "333", "name": "阿三", "content": "@墨染荷韵 看看", "create_time": 101},
        {"tid": 8, "uin": "999", "name": "别人", "content": "@墨染荷韵 不是我", "create_time": 102},
    ]
    api = _FakeAPI([
        {"uin": "222", "nickname": "小明", "appid": "311", "abstime": "100", "html": mention_html},
        {"uin": "333", "nickname": "阿三", "appid": "311", "abstime": "101", "html": cmention_html},
        {"uin": "444", "nickname": "老四", "appid": "217", "abstime": "102", "html": like_html},
        {"uin": "445", "nickname": "老五", "appid": "311", "abstime": "103", "html": reply_html},
        {"uin": "555", "nickname": "老六", "appid": "311", "abstime": "104", "html": own_mention_html},
    ], comments=detail_comments)
    api.session = _FakeSession()

    plugin._apis["bot_001"] = api
    # LLM 返回固定文案
    class _FakeLLM:
        async def complete_text(self, prompt, **kw):
            return "来啦来啦"
    plugin._llm_service = _FakeLLM()

    comments_sent, replied = [], []
    class _FakeService:
        async def comment_posts(self, post, content):
            comments_sent.append((post.uin, post.tid, content))
        async def reply_comment(self, post, idx, content):
            replied.append((post.uin, post.tid, idx, content))

    plugin._services["bot_001"] = _FakeService()

    await plugin._auto_reply_once("bot_001")
    # 正文@(222): 评论说说; 评论@(333): 回评其最新 @ 评论(idx=1, tid=7)
    assert comments_sent == [(222, "abc111", "来啦来啦")]
    assert replied == [(333, "def222", 1, "来啦来啦")]
    # 赞(444)/线程回复(445)/自己说说上的@(555) 都没触发
    assert api.details == [(222, "abc111"), (333, "def222")]
    # 第二轮: 同一说说已去重, 不再回复
    await plugin._auto_reply_once("bot_001")
    assert comments_sent == [(222, "abc111", "来啦来啦")]
    assert replied == [(333, "def222", 1, "来啦来啦")]



# ══ 主动发说说 ══════════════════════════════════════════════════

from qzone_core.auto_publish import AutoPublishStore, build_daily_plan  # noqa: E402


def test_build_daily_plan_basic():
    for _ in range(20):
        times = build_daily_plan(3, "09:00", "22:30", 7200)
        assert len(times) == 3
        mins = sorted(int(t[:2]) * 60 + int(t[3:]) for t in times)
        assert all(9 * 60 <= m <= 22 * 60 + 30 for m in mins)
        assert all(mins[i + 1] - mins[i] >= 120 for i in range(2))  # >= 7200s


def test_build_daily_plan_small_window():
    # 窗口只有 2 小时, 间隔 2 小时 → 最多 2 条(60/120+1)
    times = build_daily_plan(5, "09:00", "11:00", 7200)
    assert len(times) == 2
    mins = [int(t[:2]) * 60 + int(t[3:]) for t in times]
    assert mins[1] - mins[0] >= 120


def test_build_daily_plan_edges():
    assert build_daily_plan(0, "09:00", "22:30", 7200) == []
    assert len(build_daily_plan(1, "09:00", "22:30", 7200)) == 1
    # 非法时间回退默认窗
    times = build_daily_plan(1, "xx", "yy", 0)
    assert len(times) == 1


async def test_pub_store_plan_and_history():
    with tempfile.TemporaryDirectory() as td:
        store = AutoPublishStore(Path(td) / "pub_bot_001.json")
        created = await store.ensure_plan("2026-09-26", ["10:00", "15:00"])
        assert created
        await store.mark_done(0)
        await store.save()
        assert await store.due_indices("09:59") == []      # 未到点
        assert await store.due_indices("15:00") == [1]     # 15:00 到点
        # 同日不重建
        store2 = AutoPublishStore(Path(td) / "pub_bot_001.json")
        assert not await store2.ensure_plan("2026-09-26", ["11:00"])
        assert await store2.due_indices("16:00") == [1]
        # 跨日重建
        assert await store2.ensure_plan("2026-09-27", ["09:30"])
        assert await store2.due_indices("23:59") == [0]
        # 历史上限 20
        for i in range(25):
            await store2.add_history({"time": f"t{i}", "text": f"x{i}", "tid": str(i)})
        assert len(store2._data["history"]) == 20
        assert store2.recent_texts(3) == ["x22", "x23", "x24"]  # 时间正序


async def test_pub_store_topics_ttl():
    with tempfile.TemporaryDirectory() as td:
        import time as _time
        store = AutoPublishStore(Path(td) / "topics.json")
        await store.replace_topics(["月饼", "考试"])
        assert store.valid_topics(43200) == ["月饼", "考试"]
        # 伪造过期
        store._data["topics"][0]["ts"] = _time.time() - 99999
        assert store.valid_topics(43200) == ["考试"]


async def test_pick_topic_extraction_and_fallback():
    plugin = _fresh_plugin()
    with tempfile.TemporaryDirectory() as td:
        plugin._data_dir = td
        # 情感记忆 + LLM 提炼
        class _FakeEmotion:
            def recent_interactions(self, bot_id, limit=20):
                return [{"user_msg": "今天吃了月饼", "ai_response": "好听", "timestamp": 1}]
        class _FakeLLM:
            calls = 0
            async def complete_text(self, prompt, **kw):
                _FakeLLM.calls += 1
                assert "月饼" in prompt  # 记忆进入提炼 prompt
                return '["月饼","开学"]'
        plugin._emotion_manager = _FakeEmotion()
        plugin._llm_service = _FakeLLM()
        topic = await plugin._pick_topic("bot_001")
        assert topic in ("月饼", "开学")
        assert _FakeLLM.calls == 1
        # 第二次: 池未过期 → 不再调用 LLM
        topic2 = await plugin._pick_topic("bot_001")
        assert topic2 in ("月饼", "开学")
        assert _FakeLLM.calls == 1
        # 无情感系统 → 无互动话题; 静态池兜底
        plugin2 = _fresh_plugin()
        plugin2._data_dir = td
        plugin2.plugin_config["auto_publish_topics"] = ["旅行"]
        assert await plugin2._pick_topic("bot_001") == "旅行"
        # 什么都没有 → None(自由发挥)
        plugin3 = _fresh_plugin()
        plugin3._data_dir = tempfile.mkdtemp()
        assert await plugin3._pick_topic("bot_001") is None


async def test_generate_publish_text_clean():
    class _FakeLLM:
        async def complete_text(self, prompt, **kw):
            return '"今天吃到超好吃的蛋黄月饼，幸福到转圈圈。"'
    plugin = _fresh_plugin()
    plugin._llm_service = _FakeLLM()
    plugin.plugin_config["auto_publish_topics"] = ["美食"]
    text = await plugin._generate_publish_text("bot_001", "美食")
    assert text == "今天吃到超好吃的蛋黄月饼，幸福到转圈圈。"  # keep_tail 保留结尾标点


async def test_generate_publish_text_fail_raises():
    class _FakeLLM:
        async def complete_text(self, prompt, **kw):
            return ""
    plugin = _fresh_plugin()
    plugin._llm_service = _FakeLLM()
    try:
        await plugin._generate_publish_text("bot_001", None)
        raise AssertionError("应当抛错")
    except RuntimeError as e:
        assert "生成失败" in str(e)


class _PubFakeAPI:
    def __init__(self):
        self.session = None
        self.published = []

    async def get_atme_list(self):  # 未启用回复时不会被调
        raise RuntimeError("should not be called")

    class _S:
        async def get_uin(self):
            return 111
    session = _S()

    def __init__(self):
        self.session = type("S", (), {"get_uin": staticmethod(lambda: asyncio.sleep(0, result=111))})()


async def test_auto_publish_tick_end_to_end():
    plugin = _fresh_plugin()
    with tempfile.TemporaryDirectory() as td:
        plugin._data_dir = td
        plugin.plugin_config["auto_publish_enabled"] = True
        plugin.plugin_config["auto_publish_daily_count"] = 2
        plugin.plugin_config["auto_publish_window_start"] = "00:00"
        plugin.plugin_config["auto_publish_window_end"] = "23:59"
        plugin.plugin_config["auto_publish_min_gap_sec"] = 0

        class _FakeLLM:
            async def complete_text(self, prompt, **kw):
                return "今天天气不错，出去走走。"
        plugin._llm_service = _FakeLLM()

        published = []
        class _FakeService:
            async def publish_post(self, text=None, images=None):
                published.append(text)
                return Post(uin=111, tid=f"tid{len(published)}", name="b", text=text)

        class _S:
            async def get_uin(self):
                return 111
        api = type("A", (), {"session": type("S", (), {})()})()
        api.session = _S()
        plugin._apis["bot_001"] = api
        plugin._services["bot_001"] = _FakeService()

        # 预置当日计划: 一个已到点(00:01), 一个未来(23:59 之外不存在 → 用 23:59 但可能已过)
        store = plugin._get_pub_store("bot_001")
        from mohobot.utils.time_utils import format_utc8
        today = format_utc8("%Y-%m-%d")
        now_hhmm = format_utc8("%H:%M")
        past = "00:01" if now_hhmm > "00:01" else "23:58"
        future = "23:59" if now_hhmm <= "23:58" else "00:01"
        await store.ensure_plan(today, [past, future])
        await store.save()

        await plugin._auto_publish_tick("bot_001")
        assert len(published) == 1
        assert published[0] == "今天天气不错，出去走走。"
        hist = store._data["history"]
        assert len(hist) == 1 and hist[0]["text"] == published[0]
        # 已到点条目标记 done: 再 tick 不重复发布
        await plugin._auto_publish_tick("bot_001")
        assert len(published) == 1
        # 排除名单生效
        plugin.plugin_config["auto_publish_exclude_bots"] = ["bot_001"]
        await store.ensure_plan(today, [past])  # 重建计划(同日不重建 → 手动重置)
        store._data["plan"]["done"] = [False]
        await plugin._auto_publish_tick("bot_001")
        assert len(published) == 1


async def test_cmd_publish_log_shape():
    plugin = _fresh_plugin()
    with tempfile.TemporaryDirectory() as td:
        plugin._data_dir = td
        store = plugin._get_pub_store("bot_001")
        await store.ensure_plan("2026-09-26", ["10:00"])
        await store.mark_done(0)
        await store.add_history({"time": "10:00", "text": "你好呀", "tid": "t1"})
        ev = _private_event([{"type": "text", "data": {"text": "/发布记录"}}], user_id=3831097597)
        handled, reply = await plugin.on_message("bot_001", ev, {})
        assert handled is True
        assert "10:00" in reply and "你好呀" in reply


if __name__ == "__main__":
    asyncio.run(main())
