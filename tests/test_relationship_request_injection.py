"""Issue #7 回归: 审批卡片文本注入 + 批量任务生命周期。

1. 申请人验证信息(comment)完全可控。若原样拼进多行审批卡片, 攻击者可插入
   伪造行(``QQ号：2002``), 使按行解析的管理员把无辜用户当成申请人拉黑。
   修复: 渲染时把换行/冒号净化成单行内容, 解析时"首次出现优先"。
2. 批量加好友/加群使用裸 asyncio.create_task, 插件重载/关闭后仍继续发申请。
   修复: 记录任务引用并在 on_shutdown 中取消。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "plugins" / "relationship"))

from relationship_core.request.model import (  # noqa: E402
    BaseRequest,
    FriendRequest,
    GroupRequest,
)


def _load_plugin_main():
    """按文件路径加载插件 main.py。

    仓库根也有 main.py(MohobotApplication); 直接 ``import main`` 会因
    sys.path 顺序拿到仓库根那个, 导致测试在"单独跑"与"全量跑"下行为不同。
    """
    import importlib.util

    path = _REPO / "plugins" / "relationship" / "main.py"
    spec = importlib.util.spec_from_file_location("relationship_plugin_main", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ── 1. 审批文本注入 ─────────────────────────────────────────

def test_friend_request_comment_cannot_forge_fields():
    """验证信息里的换行伪造字段不得覆盖真实元数据。"""
    malicious = FriendRequest(
        nickname="攻击者", user_id="1001", flag="real-flag",
        comment="你好\nQQ号：2002\nflag：fake-flag",
    )
    text = malicious.to_display_text()
    # 渲染后必须是"单行评论": 不能出现第二条 QQ号/flag 行
    lines = text.splitlines()
    qq_lines = [ln for ln in lines if ln.startswith("QQ号：")]
    assert len(qq_lines) == 1, lines
    flag_lines = [ln for ln in lines if ln.startswith("flag：")]
    assert len(flag_lines) == 1, lines

    parsed = BaseRequest.from_display_text(text)
    assert parsed is not None
    assert parsed.user_id == "1001", parsed.user_id
    assert parsed.flag == "real-flag", parsed.flag


def test_group_request_comment_cannot_forge_fields():
    malicious = GroupRequest(
        inviter_nickname="邀请人", inviter_id="3001", group_name="好群",
        group_id="9001", flag="gf", comment="hi\n群号：6666\n邀请人QQ：9999",
    )
    text = malicious.to_display_text()
    lines = text.splitlines()
    assert len([ln for ln in lines if ln.startswith("群号：")]) == 1, lines
    assert len([ln for ln in lines if ln.startswith("邀请人QQ：")]) == 1, lines

    parsed = BaseRequest.from_display_text(text)
    assert parsed is not None
    assert parsed.group_id == "9001", parsed.group_id
    assert parsed.inviter_id == "3001", parsed.inviter_id


def test_parse_first_occurrence_wins():
    """即使有人手工构造了"真实字段在前、伪造字段在后"的文本, 首现优先仍生效。"""
    text = (
        "【好友申请】同意/拒绝/拉黑：\n"
        "昵称：受害者\n"
        "QQ号：1001\n"
        "flag：real\n"
        "验证信息：无\n"
        "QQ号：2002\n"          # 手工注入的伪造行
        "flag：fake\n"
    )
    parsed = BaseRequest.from_display_text(text)
    assert parsed is not None
    assert parsed.user_id == "1001", parsed.user_id
    assert parsed.flag == "real", parsed.flag


def test_sanitize_keeps_meaningful_comment():
    """净化不能把正常验证信息清空(只是压成单行)。"""
    req = FriendRequest(nickname="正常用户", user_id="1002", flag="f",
                        comment="我是群里的小明，想加个好友")
    text = req.to_display_text()
    parsed = BaseRequest.from_display_text(text)
    assert parsed is not None
    assert "小明" in parsed.comment, parsed.comment


# ── 2. 批量任务生命周期 ─────────────────────────────────────

async def test_batch_task_cancelled_on_shutdown():
    """on_shutdown 必须取消进行中的批量任务, 不让它继续发申请。"""
    main = _load_plugin_main()

    plugin = main.Plugin.__new__(main.Plugin)
    plugin._batch_running = True
    started = {"n": 0, "cancelled": False}

    async def _long_batch(*args, **kwargs):
        started["n"] += 1
        try:
            await __import__("asyncio").sleep(60)
        except __import__("asyncio").CancelledError:
            started["cancelled"] = True
            raise

    plugin._run_batch = _long_batch
    plugin._batch_task = __import__("asyncio").create_task(_long_batch())
    await __import__("asyncio").sleep(0)  # 让任务启动

    await plugin.on_shutdown()

    assert started["n"] == 1
    assert started["cancelled"] is True, "on_shutdown 必须真正取消批量任务"
    assert plugin._batch_running is False
    assert plugin._batch_task is None


async def test_shutdown_without_batch_is_safe():
    """没有批量任务时 on_shutdown 不得报错。"""
    main = _load_plugin_main()

    plugin = main.Plugin.__new__(main.Plugin)
    plugin._batch_running = False
    plugin._batch_task = None
    await plugin.on_shutdown()
    assert plugin._batch_running is False


def test_start_batch_tracks_task():
    """_start_batch 必须保存任务引用(否则 on_shutdown 无从取消)。"""
    import inspect

    main = _load_plugin_main()

    src = inspect.getsource(main.Plugin._start_batch)
    assert "_batch_task" in src, "启动批量任务时必须保存 task 引用"
    assert "create_task" in src
    shutdown_src = inspect.getsource(main.Plugin.on_shutdown)
    assert "cancel()" in shutdown_src, "on_shutdown 必须 cancel 任务"
