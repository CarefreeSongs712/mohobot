"""空白/纯符号消息不得进入 LLM 请求(网关 400)回归测试。

背景: 上游网关对"内容为空或只有空白/标点"的请求直接返回
`400 invalid_request_error`, 整轮回复变成 `[LLM 调用失败: ...]`。
生产实测触发输入: `？` `👍` `👀` `😔` `?` `╥﹏╥` `(╥_╥)`。

覆盖:
1. has_meaningful_text: 字母/数字/汉字 → True; 纯标点/emoji/空白 → False
2. _prepare_messages: 纯符号与空白输入都被替换为标注, 请求内容非空且有语义
3. 上下文中的纯符号历史条目在重放时被跳过, 不影响已有正常历史
4. _build_user_store_content: 写回上下文的内容不会留下纯符号条目
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.message_handler import MessageHandler
from mohobot.utils.cq_code import (
    empty_message_placeholder, has_meaningful_text, symbol_only_placeholder,
)


def test_meaningful_text_detection():
    """只有字母/数字/汉字算有实质内容; 纯标点、emoji、空白都不算。"""
    meaningful = ["你好", "hello", "123", "？好的", "a", "第3集", "😔难过"]
    meaningless = ["", "   ", "？", "?", "👍", "👀", "😔", "╥﹏╥", "(╥_╥)",
                   "…", "。", "!!!", "（嘴一撇，没绷住）…".replace("嘴一撇，没绷住", "")]
    for text in meaningful:
        assert has_meaningful_text(text), f"应判定为有内容: {text!r}"
    for text in meaningless:
        assert not has_meaningful_text(text), f"应判定为无实质内容: {text!r}"
    print("[1] has_meaningful_text 判定 OK")


def test_placeholder_keeps_request_valid():
    """纯符号/空白消息的替代文本本身必须有语义, 否则仍然会被网关拒绝。"""
    for chat_type in ("group", "private"):
        for text in ("", "？", "👍", "😔", "╥﹏╥"):
            if text.strip():
                out = symbol_only_placeholder(chat_type, text)
            else:
                out = empty_message_placeholder(chat_type, "")
            assert out.strip(), f"替代文本不能为空: {text!r}"
            assert has_meaningful_text(out), f"替代文本必须含语义: {out!r}"
    print("[2] 替代文本自身合法 OK")


class _FakeEvent:
    """_prepare_messages 需要的最小事件形状。"""

    def __init__(self, message, group_id=None):
        self.message = message
        self.user_id = 10001
        self.self_id = 111
        self.time = 1700000000
        self.sender = SimpleNamespace(card="", nickname="测试用户")
        if group_id is None:
            from mohobot.models.onebot import PrivateMessageEvent
            self.__class__ = PrivateMessageEvent
            self.message_type = "private"
        else:
            from mohobot.models.onebot import GroupMessageEvent
            self.__class__ = GroupMessageEvent
            self.message_type = "group"
            self.group_id = group_id


async def _prepare(service, message, context=None, group_id=None):
    event = _FakeEvent(message, group_id=group_id)
    return await service._prepare_messages("bot_001", event, context or [])


class _StubLLM:
    """只借用 LLMService._prepare_messages, 避免构造真实客户端。"""

    def __init__(self):
        self._cfg = SimpleNamespace(tts=SimpleNamespace(enabled=False))
        self._image_cache = None
        self._song_annotator = None
        self._vision_available = False
        self._vision_client = None

    async def _describe_image_for_text(self, url):
        return ""


async def test_symbol_message_replaced_in_request():
    """只有标点/emoji 的当前消息 → 请求里是带语义的标注, 不再是纯符号。"""
    service = _StubLLM()
    from mohobot.llm_service import LLMService

    service._prepare_messages = LLMService._prepare_messages.__get__(service)

    for text in ("？", "?", "👍", "👀", "😔", "╥﹏╥", "(╥_╥)"):
        for group_id in (None, 2001):
            messages = await _prepare(
                service, [{"type": "text", "data": {"text": text}}], group_id=group_id,
            )
            user_messages = [m for m in messages if m["role"] == "user"]
            assert user_messages, f"缺少 user 消息: {text!r}"
            content = user_messages[-1]["content"]
            assert has_meaningful_text(content), f"纯符号未被替换: {text!r} → {content!r}"
            assert text in content or "符号" in content or "表情" in content, content
    print("[3] 纯符号消息替换为标注 OK")


async def test_empty_message_replaced_in_request():
    """真空/仅 @ 消息 → 使用占位标注(原有行为, 防止回退)。"""
    service = _StubLLM()
    from mohobot.llm_service import LLMService

    service._prepare_messages = LLMService._prepare_messages.__get__(service)
    for message in ([], [{"type": "at", "data": {"qq": "111"}}]):
        messages = await _prepare(service, message, group_id=2001)
        user_messages = [m for m in messages if m["role"] == "user"]
        assert user_messages and has_meaningful_text(user_messages[-1]["content"])
    print("[4] 空白/仅 @ 消息占位 OK")


async def test_symbol_history_entries_replaced_not_sent_raw():
    """历史里的纯符号条目不能原样发给网关(会 400); 正常历史不受影响。

    注意: 条目是被**替换为带语义的标注**而不是丢弃, 理由见
    test_symbol_history_keeps_role_alternation_and_bot_replies。
    """
    service = _StubLLM()
    from mohobot.llm_service import LLMService

    service._prepare_messages = LLMService._prepare_messages.__get__(service)
    context = [
        {"role": "user", "content": "？"},
        {"role": "assistant", "content": "怎么了呀"},
        {"role": "user", "content": "👍"},
        {"role": "3831097597-墨染荷韵", "content": "今天好累"},
        {"role": "assistant", "content": "辛苦啦"},
    ]
    messages = await _prepare(
        service, [{"type": "text", "data": {"text": "在吗"}}],
        context=context, group_id=2001,
    )
    contents = [m["content"] for m in messages if m["role"] in ("user", "assistant")]
    assert "？" not in contents, "纯符号历史条目不得原样发送"
    assert "👍" not in contents, "纯 emoji 历史条目不得原样发送"
    assert all(has_meaningful_text(c) for c in contents), contents
    assert any("怎么了呀" in c for c in contents), "正常历史不得被丢弃"
    assert any("辛苦啦" in c for c in contents), "正常历史不得被丢弃"
    assert any("今天好累" in c for c in contents), "具名发言历史不得被丢弃"
    print("[5] 纯符号历史条目替换而非原样发送 OK")


async def test_symbol_history_keeps_role_alternation_and_bot_replies():
    """历史里的纯符号条目必须"替换"而不是"丢弃"。

    丢弃会带来两个后果(实测):
    1. bot 自己的回复(如「……」「~~~」「♪」)消失, 模型看到用户连说几遍却没有任何
       回应, 容易误判上一轮没答而重复解释/道歉;
    2. user/assistant 角色交替被破坏(user,user,user / assistant,assistant)。
    替换为带语义的标注则两者都保留, 且仍不会触发网关"只有空白/标点"的 400。
    """
    service = _StubLLM()
    from mohobot.llm_service import LLMService

    service._prepare_messages = LLMService._prepare_messages.__get__(service)
    context = [
        {"role": "user", "content": "你生气了吗"},
        {"role": "assistant", "content": "……"},
        {"role": "user", "content": "你说话呀"},
        {"role": "assistant", "content": "~~~"},
        {"role": "user", "content": "？"},
        {"role": "assistant", "content": "怎么了呀"},
    ]
    messages = await _prepare(
        service, [{"type": "text", "data": {"text": "在吗"}}], context=context,
    )
    turns = [m for m in messages if m["role"] in ("user", "assistant")]
    roles = [m["role"] for m in turns]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant", "user"], roles
    assert all(has_meaningful_text(m["content"]) for m in turns), [m["content"] for m in turns]
    # 原文符号仍可见(模型知道对方发/回了什么), 而不是被抹掉
    assert "……" in turns[1]["content"] and "~~~" in turns[3]["content"], turns
    assert "？" in turns[4]["content"], turns[4]
    print("[5b] 纯符号历史替换后角色交替与 bot 回复保留 OK")


async def test_history_non_string_content_does_not_crash():
    """历史 content 不是字符串(旧数据/插件写入)时不能让整轮回复崩溃。"""
    service = _StubLLM()
    from mohobot.llm_service import LLMService

    service._prepare_messages = LLMService._prepare_messages.__get__(service)
    context = [
        {"role": "user", "content": None},
        {"role": "assistant", "content": 123},
        {"role": "user", "content": [{"type": "text", "text": "旧格式多模态"}]},
        {"role": "assistant", "content": "正常回复"},
    ]
    messages = await _prepare(
        service, [{"type": "text", "data": {"text": "在吗"}}], context=context,
    )
    contents = [m["content"] for m in messages if m["role"] in ("user", "assistant")]
    assert any("正常回复" in str(c) for c in contents), contents
    print("[5c] 非字符串历史 content 不崩溃 OK")


def test_history_symbol_placeholder_is_bounded():
    """刷屏式超长符号串不能在每轮请求里被完整复现(token 与成本)。"""
    from mohobot.llm_service import _HISTORY_SYMBOL_MAX_CHARS, _history_symbol_placeholder

    for role in ("user", "assistant"):
        out = _history_symbol_placeholder(role, "👍" * 5000, is_group=False)
        assert len(out) < 200, (role, len(out))
        assert has_meaningful_text(out), out
        assert "已截断" in out, out
        # 未超限的短符号原样保留
        short = _history_symbol_placeholder(role, "👍" * 3, is_group=False)
        assert "👍👍👍" in short and "已截断" not in short, short
    assert _HISTORY_SYMBOL_MAX_CHARS >= 10
    print("[5e] 超长符号历史占位被截断 OK")


def test_has_meaningful_text_tolerates_non_strings():
    """has_meaningful_text 对 None/数字/列表这类非字符串输入不得抛 TypeError。"""
    assert has_meaningful_text(None) is False
    assert has_meaningful_text(123) is True
    assert has_meaningful_text([]) is False
    assert has_meaningful_text([{"type": "text", "text": "看图"}]) is True
    assert has_meaningful_text({"a": 1}) is True
    print("[5d] has_meaningful_text 容忍非字符串 OK")


async def test_store_content_not_symbol_only():
    """写回上下文的内容: 纯符号消息也必须变成可重放的标注。"""
    h = MessageHandler.__new__(MessageHandler)
    h._image_cache = None
    h._llm = None

    async def _render(bot_id, message, *, describe_missing=False):
        return ""

    h._render_message_content = _render
    for message in ([{"type": "text", "data": {"text": "？"}}], []):
        out = await h._build_user_store_content("bot_001", _FakeEvent(message, 2001), "")
        assert out.strip(), "写回内容不能为空"
        assert has_meaningful_text(out), f"写回内容必须有语义: {out!r}"
    print("[6] 上下文写回不含纯符号条目 OK")


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                if asyncio.iscoroutinefunction(_fn):
                    asyncio.run(_fn())
                else:
                    _fn()
                print(f"PASS {_name}")
            except Exception as e:  # noqa: BLE001
                _failed += 1
                print(f"FAIL {_name}: {e}")
                import traceback
                traceback.print_exc()
    print("---")
    assert _failed == 0, f"{_failed} 个测试失败"
    print("ALL EMPTY MESSAGE TESTS PASSED")
