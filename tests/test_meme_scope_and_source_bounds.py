"""Issue #6 回归: meme 作用域过滤、steal_meme 图片来源边界、source_type 规范化。

1. `/meme list` 必须按当前事件作用域过滤 local 表情(A 群专属不能出现在 B 群)。
2. `steal_meme` 显式 ref 必须命中当前/引用消息里的图片, 不得接受任意 URL/绝对路径。
3. `source_type` 入口统一 strip().lower(), 空格包裹的 ` meme_pack ` 不能绕过白名单。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "plugins" / "meme_stealer"))


# ── 1. list 命令作用域过滤 ──────────────────────────────────

def _list_cmd(scope: str = "group:100"):
    from meme_stealer_core.core.commands.image_mgmt_command import ImageManagementCommand

    class _Cfg:
        @staticmethod
        def get_event_target(event):
            kind, _, tid = scope.partition(":")
            return kind, tid

    class _Db:
        def __init__(self):
            self.calls: list[dict] = []

        def get_emojis_paginated(self, **kwargs):
            self.calls.append(kwargs)
            return [], 0, {}

        @staticmethod
        def count_total():
            return 5

        @staticmethod
        def get_paths_sorted_newest():
            return []

    db = _Db()
    plugin = SimpleNamespace(plugin_config=_Cfg(), db_service=db)
    return ImageManagementCommand(plugin), db


async def test_list_command_passes_scope_target():
    """列表分页查询必须带当前事件作用域, 否则 local 表情跨群泄漏。"""

    cmd, db = _list_cmd("group:100")
    class _Event:
        @staticmethod
        def plain_result(text):
            return ("text", text)

    event = _Event()  # get_event_target 由假 cfg 处理
    gen = cmd.list_images(event, "", "10", "1")
    out = []
    async for item in gen:
        out.append(item)
    assert db.calls, "应至少调用一次分页查询"
    assert db.calls[0].get("scope_target") == "group:100", db.calls[0]


async def test_list_command_user_scope_target():
    cmd, db = _list_cmd("user:456")

    class _Event:
        @staticmethod
        def plain_result(text):
            return ("text", text)

    async for _ in cmd.list_images(_Event(), "", "10", "1"):
        pass
    assert db.calls[0].get("scope_target") == "user:456", db.calls[0]


def test_event_scope_target_helper():
    cmd, _ = _list_cmd("group:20002")
    assert cmd._event_scope_target(SimpleNamespace()) == "group:20002"
    # 取不到作用域时返回空串(不因过滤把全部表情隐藏)
    class _BadCfg:
        @staticmethod
        def get_event_target(event):
            raise RuntimeError("no target")

    cmd.plugin.plugin_config = _BadCfg()
    assert cmd._event_scope_target(SimpleNamespace()) == ""


# ── 2. steal_meme 图片来源边界 ──────────────────────────────

def _match(explicit: str, candidates: list[tuple[str, str]]):
    from meme_stealer_core.core.tools.meme_steal_tool import MemeStealToolWorkflow

    return MemeStealToolWorkflow._match_candidate_ref(explicit, candidates)


def test_explicit_ref_matching_rules():
    """完整 ref、basename、带 query 的 URL 都可命中; 外部 URL/任意路径不可。"""
    candidates = [
        ("https://cdn.example/a.png?x=1", "llm_tool"),
        ("/tmp/mohobot/b.png", "llm_tool"),
    ]
    assert _match("https://cdn.example/a.png?x=1", candidates) is not None
    assert _match("https://cdn.example/a.png", candidates) is not None  # 去 query 后一致
    assert _match("a.png", candidates) is not None                      # basename
    assert _match("/tmp/mohobot/b.png", candidates) is not None
    assert _match("b.png", candidates) is not None

    # 越界内容必须拒绝
    assert _match("https://evil.example/steal.png", candidates) is None
    assert _match("/etc/passwd", candidates) is None
    assert _match("/root/secret.png", candidates) is None
    assert _match("", candidates) is None
    assert _match("x.png", []) is None


async def test_steal_tool_rejects_outside_image_ref():
    """显式 ref 不在当前/引用消息中 → 返回空 ref(rejected), 不进入下载/VLM。"""
    from meme_stealer_core.core.tools.meme_steal_tool import MemeStealToolWorkflow

    handler = object.__new__(MemeStealToolWorkflow)

    class _Event:
        def get_messages(self):
            return []  # 当前消息没有图片

    class _EH:
        @staticmethod
        async def _resolve_media_ref(comp):
            return ""

        @staticmethod
        def _extract_store_emoji_urls(event):
            return []

    ref, source = await handler._resolve_steal_image_ref(
        _Event(), "https://evil.example/x.png", _EH(),
    )
    assert ref == "", f"越界 URL 必须被拒绝, got {ref!r}"
    assert source == "rejected", source


async def test_steal_tool_accepts_in_message_url():
    """当前消息里的图片 URL: 显式 ref 命中 → 放行。"""
    from meme_stealer_core.core.tools.meme_steal_tool import MemeStealToolWorkflow
    from meme_stealer_core.host import Image as MessageImage

    handler = object.__new__(MemeStealToolWorkflow)

    class _Event:
        def get_messages(self):
            return [MessageImage(url="https://cdn.example/in-msg.png")]

    class _EH:
        @staticmethod
        async def _resolve_media_ref(comp):
            return getattr(comp, "url", "")

        @staticmethod
        def _extract_store_emoji_urls(event):
            return []

    ref, source = await handler._resolve_steal_image_ref(
        _Event(), "https://cdn.example/in-msg.png", _EH(),
    )
    assert ref == "https://cdn.example/in-msg.png", ref
    assert source == "llm_tool"


async def test_steal_tool_without_ref_uses_event_image():
    from meme_stealer_core.core.tools.meme_steal_tool import MemeStealToolWorkflow
    from meme_stealer_core.host import Image as MessageImage

    handler = object.__new__(MemeStealToolWorkflow)

    class _Event:
        def get_messages(self):
            return [MessageImage(url="https://cdn.example/first.png")]

    class _EH:
        @staticmethod
        async def _resolve_media_ref(comp):
            return getattr(comp, "url", "")

        @staticmethod
        def _extract_store_emoji_urls(event):
            return []

    ref, _source = await handler._resolve_steal_image_ref(_Event(), "", _EH())
    assert ref == "https://cdn.example/first.png", ref


# ── 3. source_type 规范化 ──────────────────────────────────

def _api_source_validator(tmp: Path):
    from meme_stealer_core.api.external_sources import ExternalSourceRoutes

    api = object.__new__(ExternalSourceRoutes)
    api._data_dir = tmp
    return api


def test_source_type_whitespace_does_not_bypass_whitelist():
    """` meme_pack ` 带空格时, 仍要进入 pack 路径白名单校验(越界路径被拒)。"""
    import asyncio
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        api = _api_source_validator(tmp)
        # 越界路径: 若绕过白名单校验会直接通过; 正确行为是抛错
        payload = {"source_type": " meme_pack ", "path": "/etc/passwd"}
        try:
            api._validate_source_pack_path(payload)
        except Exception as exc:  # ExternalSourceError
            assert "path" in str(exc).lower() or "allow" in str(exc).lower(), exc
        else:
            raise AssertionError("带空格的 meme_pack 绕过了路径白名单校验")

        # 插件数据目录内的合法路径 → 不应抛错
        inside = tmp / "pack.zip"
        inside.write_bytes(b"PK\x03\x04")
        api._validate_source_pack_path({"source_type": " meme_pack ", "path": str(inside)})


def test_source_type_normalization_is_strip_lower():
    import inspect

    from meme_stealer_core.api.external_sources import ExternalSourceRoutes

    src = inspect.getsource(ExternalSourceRoutes._validate_source_pack_path)
    assert ".strip().lower()" in src, "source_type 必须先 strip 再 lower"
