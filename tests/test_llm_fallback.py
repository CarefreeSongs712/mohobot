"""LLMModule 备用模型回退测试:
1. 连接类错误(APIConnectionError/APITimeoutError) → 回退备用模型重试 1 次
2. 非连接类错误(AuthenticationError) → 不回退, 直接抛
3. 备用模型为空 → 不回退
4. 回退也失败 → 抛异常
5. 备用与主模型相同 → 不重复调用
6. 主模型成功 → 不触发回退
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import APIConnectionError, APITimeoutError, AuthenticationError
from mohobot.agent.llm_module import LLMModule


class FakeChat:
    """mock openai chat.completions.create。"""

    def __init__(self, fail_first=0, fail_err=None, always_fail=None):
        # fail_first: 前 N 次调用抛 fail_err; always_fail: 每次抛(覆盖 fail_first)
        self.calls = []          # 每次调用的 model
        self.fail_first = fail_first or 0
        self.fail_err = fail_err if fail_err is not None else APIConnectionError(request=None)
        self.always_fail = always_fail

    async def create(self, **params):
        self.calls.append(params.get("model"))
        err = self.always_fail
        if err is None and len(self.calls) <= self.fail_first:
            err = self.fail_err
        if err is not None:
            raise err
        return FakeResp(f"回复-{params.get('model')}")


class FakeClient:
    def __init__(self, chat):
        self.chat = type("Chat", (), {"completions": chat})()


class FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})()


def make_module(chat, fallback="DeepSeek-V4-Flash", model="Qwen3-8B"):
    mod = LLMModule(
        module_name="memory_writer",
        config={},
        template="test {{x}}",
        model=model,
        base_url="http://x/v1",
        api_key="k",
        fallback_model=fallback,
        data_dir="./data_test_llm",
    )
    mod._client = FakeClient(chat)
    return mod


async def test_connection_error_falls_back():
    chat = FakeChat(fail_first=1)
    mod = make_module(chat)
    out = await mod.generate_response(x="hi")
    assert out == "回复-DeepSeek-V4-Flash"
    assert chat.calls == ["Qwen3-8B", "DeepSeek-V4-Flash"], "应先主后备用"


async def test_timeout_error_falls_back():
    chat = FakeChat(fail_first=1, fail_err=APITimeoutError(request=None))
    mod = make_module(chat, fallback="mimo-v2.5")
    out = await mod.generate_response(x="hi")
    assert out == "回复-mimo-v2.5"
    assert chat.calls == ["Qwen3-8B", "mimo-v2.5"]


async def test_auth_error_no_fallback():
    """非连接类错误(如 key 无效)不回退, 直接抛。"""
    fake_resp = type("R", (), {"request": None, "status_code": 401, "headers": {}, "text": ""})()
    chat = FakeChat(always_fail=AuthenticationError(
        message="bad key", response=fake_resp, body=None))
    mod = make_module(chat)
    try:
        await mod.generate_response(x="hi")
        raise AssertionError("应抛出 AuthenticationError")
    except AuthenticationError:
        pass
    assert chat.calls == ["Qwen3-8B"], "不应触发回退调用"


async def test_no_fallback_configured():
    chat = FakeChat(fail_first=1)
    mod = make_module(chat, fallback="")
    try:
        await mod.generate_response(x="hi")
        raise AssertionError("应抛出 APIConnectionError")
    except APIConnectionError:
        pass
    assert chat.calls == ["Qwen3-8B"], "无备用模型不应重试"


async def test_fallback_also_fails():
    chat = FakeChat(fail_first=2)  # 两次都失败
    mod = make_module(chat)
    try:
        await mod.generate_response(x="hi")
        raise AssertionError("应抛出异常")
    except APIConnectionError:
        pass
    assert chat.calls == ["Qwen3-8B", "DeepSeek-V4-Flash"]


async def test_same_model_no_retry():
    chat = FakeChat(fail_first=1)
    mod = make_module(chat, fallback="Qwen3-8B", model="Qwen3-8B")
    try:
        await mod.generate_response(x="hi")
        raise AssertionError("应抛出异常")
    except APIConnectionError:
        pass
    assert chat.calls == ["Qwen3-8B"], "备用与主模型相同不应重试"


async def test_success_no_fallback():
    chat = FakeChat(fail_first=0)
    mod = make_module(chat)
    assert await mod.generate_response(x="hi") == "回复-Qwen3-8B"
    assert chat.calls == ["Qwen3-8B"]


def _main() -> int:
    import asyncio as _a
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if _a.iscoroutinefunction(fn):
                    _a.run(fn())
                else:
                    fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{len([n for n in globals() if n.startswith('test_')]) - failed}/"
          f"{len([n for n in globals() if n.startswith('test_')])} passed")
    return failed


if __name__ == "__main__":
    sys.exit(_main())
