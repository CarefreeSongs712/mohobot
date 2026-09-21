"""OneBot API 调用工具 — 兼容不同协议端的返回值差异。

retcode 有 str/int 两种形态、status 可能缺失, 这里统一收口,
与 relationship_core/utils.py 的同名实现保持一致的容错口径。
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def _ok(resp: Any) -> bool | None:
    """响应 → 成功/失败; 结构无法判断时返回 None。"""
    if not isinstance(resp, dict):
        return None
    if str(resp.get("status", "")).lower() in ("failed", "error", "fail"):
        return False
    try:
        return int(resp.get("retcode", 0)) == 0
    except (TypeError, ValueError):
        return False


def _detail(resp: Any) -> Any:
    """提取响应里的错误描述(非 dict 直接返回原值)。"""
    if not isinstance(resp, dict):
        return resp
    return resp.get("wording") or resp.get("message") or resp


async def api_call(
    ws_server, bot_id: str, action: str, params: dict | None = None,
    timeout: float = 10.0,
) -> Any:
    """调用 OneBot API 并返回响应 data(失败/超时返回 None)。"""
    if ws_server is None:
        return None
    try:
        resp = await ws_server.send_to_bot(
            bot_id, action, params or {}, wait_response=True, timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"API {action} 调用失败: {e}")
        return None
    if _ok(resp) is not True:
        logger.warning(f"API {action} 返回错误: {_detail(resp)}")
        return None
    return resp.get("data")


async def api_ok(
    ws_server, bot_id: str, action: str, params: dict | None = None,
    timeout: float = 10.0,
) -> bool:
    """调用 OneBot API, 只返回成败。

    与 api_call 的区别: 不依赖响应 data 是否为 null ——
    发送类接口成功时 data 可能为 null, 用 api_call 会误判为失败。
    """
    if ws_server is None:
        return False
    try:
        resp = await ws_server.send_to_bot(
            bot_id, action, params or {}, wait_response=True, timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"API {action} 调用失败: {e}")
        return False
    if _ok(resp) is not True:
        logger.warning(f"API {action} 返回错误: {_detail(resp)}")
        return False
    return True
