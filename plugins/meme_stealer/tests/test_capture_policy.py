"""抓取策略回归: 概览式被动抓取默认关闭; LLM 工具抓取默认需管理员审核。

对应需求: 移除入站聊天全量概览式抓取, 仅保留 LLM 工具调用抓取与 WebUI 手动上传;
LLM 标注后需管理员审核后才入库。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _config_defaults():
    import tempfile
    from pathlib import Path as _P

    from meme_stealer_core.core.config.config import PluginConfig

    with tempfile.TemporaryDirectory() as td:
        return PluginConfig(None, _P(td))


def test_passive_steal_disabled_by_default():
    """概览式被动抓取必须默认关闭(这是本次需求的核心)。"""
    cfg = _config_defaults()
    assert cfg.steal_meme is False, "偷图总开关默认关闭"
    assert cfg.passive_steal_enabled is False, "被动概览抓取必须默认关闭"


def test_tool_capture_requires_review_by_default():
    """LLM 工具抓取默认必须经管理员审核。"""
    cfg = _config_defaults()
    assert cfg.tool_capture_requires_review is True


def test_schema_declares_new_switch_defaults():
    """_conf_schema.json 的默认值必须与代码一致, 否则 WebUI 会显示错误状态。"""
    import json

    schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["passive_steal_enabled"]["default"] is False
    assert schema["tool_capture_requires_review"]["default"] is True
    # steal_meme 的 hint 不能再宣传"所有消息都会被观察"
    hint = schema["steal_meme"].get("hint", "")
    assert "所有消息都会被观察" not in hint, hint


def test_event_handler_gates_passive_capture():
    """on_message 在被动抓取关闭时必须跳过聊天图片(VLM 不被调用)。"""
    import asyncio
    import inspect

    from meme_stealer_core.core.events import event_handler as eh_mod

    src = inspect.getsource(eh_mod.EventHandler.on_message)
    assert "passive_steal_enabled" in src, "on_message 必须读取被动抓取开关"
    # 开关判断必须在图片提取之前, 否则仍会下载/分析
    gate_pos = src.index("passive_steal_enabled")
    imgs_pos = src.index("imgs: list[Image]")
    assert gate_pos < imgs_pos, "被动抓取开关必须在提取图片之前生效"
    assert asyncio.iscoroutinefunction(eh_mod.EventHandler.on_message)


async def test_passive_disabled_returns_before_processing():
    """真实调用: passive_steal_enabled=False 时 on_message 不进入图片处理。"""
    from types import SimpleNamespace

    from meme_stealer_core.core.events.event_handler import EventHandler

    handler = object.__new__(EventHandler)
    handler._cleaned = False
    calls = {"process": 0}

    class _ChatContext:
        def record_event(self, event):
            return 1

    handler.chat_context = _ChatContext()

    class _Plugin:
        plugin_config = SimpleNamespace(
            steal_meme=True,                    # 总开关开着
            passive_steal_enabled=False,        # 被动抓取关闭
            audit_required=False,
        )
        steal_meme = True                       # 插件实例上的总开关属性

        def is_steal_enabled_for_event(self, event):
            return True

        def get_force_capture_entry(self, event):
            return None

        async def _should_count(self):  # pragma: no cover - 不应被调用
            calls["process"] += 1

    handler.plugin = _Plugin()

    class _Image:
        pass

    class _Event:
        def get_messages(self):
            return [_Image()]

        def get_message_str(self):
            return ""

    # 让 Image 判定成立, 若未被门控拦下就会往下走
    import meme_stealer_core.core.events.event_handler as mod
    original = getattr(mod, "Image", None)
    mod.Image = _Image
    try:
        await handler.on_message(_Event())
    finally:
        if original is not None:
            mod.Image = original
    assert calls["process"] == 0, "被动抓取关闭时不应处理图片"


async def test_tool_capture_passes_to_pending():
    """LLM 工具抓取: tool_capture_requires_review=True 时 _process_image 收到 to_pending=True。"""
    from types import SimpleNamespace

    from meme_stealer_core.core.tools import meme_steal_tool as mod

    handler = object.__new__(mod.MemeStealToolWorkflow)
    handler.plugin_config = SimpleNamespace(tool_capture_requires_review=True)
    captured = {}

    async def fake_process(event, path, **kwargs):
        captured.update(kwargs)
        return True, {}

    handler._process_image = fake_process
    handler._build_steal_tool_extra_meta = lambda *a, **k: {}
    handler.index_manager = SimpleNamespace(
        load_index=lambda: _async_value({}), save_index=lambda idx: _async_value(None)
    )

    class _EH:
        @staticmethod
        def build_chat_context(event):
            return ""

    handler._get_event_handler = lambda **kw: _EH()
    handler.is_steal_enabled_for_event = lambda event: True

    async def fake_resolve(event, ref, eh):
        return "https://example.com/a.png", "current"

    handler._resolve_steal_image_ref = fake_resolve

    class _Cfg:
        steal_meme = True
        tool_capture_requires_review = True

    handler.plugin_config = _Cfg()

    class _Plugin:
        pass

    handler.plugin = _Plugin()

    # 用本地临时图片替代下载
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as td:
        p = _P(td) / "x.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

        async def fake_download(url, log_download=False):
            return str(p), False

        class _EH2(_EH):
            @staticmethod
            async def _download_to_temp(url, log_download=False):
                return str(p), False

        handler._get_event_handler = lambda **kw: _EH2()
        handler._precheck_image_file = lambda path: (True, "")

        async def fake_todelete(*a, **k):
            return None

        collected = []
        async for chunk in handler._steal_sticker_impl(None, "https://example.com/a.png"):
            collected.append(chunk)
        assert captured.get("to_pending") is True, captured
        assert any("待管理员审核" in str(c) or "管理员审核" in str(c) for c in collected), collected


def _async_value(value):
    async def _inner(*a, **k):
        return value
    return _inner()


if __name__ == "__main__":
    import asyncio
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            if asyncio.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {e}")
            import traceback
            traceback.print_exc()
    print("---")
    assert failed == 0, f"{failed} failed"
    print("ALL CAPTURE POLICY TESTS PASSED")
