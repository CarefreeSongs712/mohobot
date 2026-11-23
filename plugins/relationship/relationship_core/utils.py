"""工具 — 移植自 astrbot_plugin_relationship (core/utils.py), 适配 mohobot。

OneBot API 通过 ws_server.send_to_bot(bot_id, action, params, wait_response=True) 调用。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

# 回执判定结果(issue #151): 成功 / 协议端明确失败 / 未取得确认。
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_UNCONFIRMED = "unconfirmed"

# 回执错误文本限长与单行化, 避免把协议端长文本原样回给聊天窗口。
ERROR_TEXT_LIMIT = 200

_MISSING = object()
_FAILED_STATUSES = frozenset({"failed", "error", "fail"})
# OneBot v11 正常回执 status: ok / async; 未知 status 一律按畸形回执处理。
_OK_STATUSES = frozenset({"ok", "async"})
_RETCODE_RE = re.compile(r"^[+-]?\d+$")


@dataclass(frozen=True)
class ApiCallResult:
    """OneBot API 调用结果。

    ``outcome`` 区分三种状态: 协议端确认成功、协议端明确失败、无法确认
    (超时/断连/未就绪)。上层据此决定是否发完成文案、是否提示"未确认"。
    """

    ok: bool
    data: Any = None
    error: str = ""
    outcome: str = OUTCOME_UNCONFIRMED


def summarize_error(value: Any) -> str:
    """把错误信息压成单行纯文本摘要并限长(详细诊断留在日志里)。"""
    text = str(value if value is not None else "")
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if len(text) > ERROR_TEXT_LIMIT:
        text = text[: ERROR_TEXT_LIMIT - 1] + "…"
    return text


def _parse_retcode(value: Any) -> int | None:
    """严格解析 retcode: 接受 int 与十进制数字串, 其余(含 float/bool)判非法。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _RETCODE_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def convert_duration_advanced(duration: int) -> str:
    """将秒数转换为友好的时长字符串, 如"1天2小时3分钟4秒"。"""
    if duration < 0:
        return "未知时长"
    if duration == 0:
        return "0秒"

    days, rem = divmod(duration, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    units = [
        (days, "天"), (hours, "小时"), (minutes, "分钟"), (seconds, "秒"),
    ]
    non_zero = [(value, label) for value, label in units if value > 0]
    if len(non_zero) == 1:
        return f"{non_zero[0][0]}{non_zero[0][1]}"
    return "".join(f"{value}{label}" for value, label in non_zero)


async def api_call_checked(ws_server, bot_id: str, action: str, params: dict | None = None,
                           timeout: float = 10.0) -> ApiCallResult:
    """调用 OneBot API 并严格判定回执, 返回三态结果(issue #151)。

    判定规则:
    - 协议端明确失败: status 为 failed/error/fail, 或 retcode 非 0;
    - 未取得确认: WebSocket 未就绪、调用异常/超时、响应为 None(断连或等待超时),
      以及畸形回执(非 dict、未知 status、缺失/无法解析的 retcode)。
      畸形回执说明本地无法判断协议端是否已执行(可能已生效但回执不合规),
      因此不能判成"明确失败"去诱导重复人工处理, 只能要求人工确认。
    """
    if ws_server is None:
        return ApiCallResult(False, None, "WebSocket 未就绪", OUTCOME_UNCONFIRMED)
    try:
        resp = await ws_server.send_to_bot(
            bot_id, action, params or {}, wait_response=True, timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — 超时/断连等统一按未确认处理
        err = summarize_error(f"{type(e).__name__}: {e}")
        logger.warning(f"API {action} 调用失败: {err}")
        return ApiCallResult(False, None, err, OUTCOME_UNCONFIRMED)

    def _fail(text: str) -> ApiCallResult:
        err = summarize_error(text)
        logger.warning(f"API {action} 返回错误: {err}")
        return ApiCallResult(False, None, err, OUTCOME_FAILED)

    def _unconfirmed(text: str) -> ApiCallResult:
        err = summarize_error(text)
        logger.warning(f"API {action} 回执无法确认结果: {err}")
        return ApiCallResult(False, None, err, OUTCOME_UNCONFIRMED)

    if resp is None:
        return ApiCallResult(
            False, None, "未取得协议端确认（等待响应超时或连接已断开）",
            OUTCOME_UNCONFIRMED,
        )
    if not isinstance(resp, dict):
        return _unconfirmed(
            f"回执异常：响应不是 JSON 对象({type(resp).__name__})，无法确认协议端是否已执行"
        )

    def _err_text(fallback: str) -> str:
        return str(resp.get("wording") or resp.get("message") or fallback)

    # status: 允许缺失(兼容不返回 status 的实现), 但非空且未知时按畸形回执处理。
    status_raw = resp.get("status", _MISSING)
    if status_raw is not _MISSING and status_raw is not None:
        status = str(status_raw).strip().lower()
        if status in _FAILED_STATUSES:
            return _fail(_err_text("请求被协议端拒绝"))
        if status and status not in _OK_STATUSES:
            return _unconfirmed(
                f"回执异常：未知 status={summarize_error(status_raw)}，无法确认协议端是否已执行"
            )

    raw_retcode = resp.get("retcode", _MISSING)
    if raw_retcode is _MISSING:
        return _unconfirmed("回执异常：缺少 retcode，无法确认协议端是否已执行")
    retcode = _parse_retcode(raw_retcode)
    if retcode is None:
        return _unconfirmed(
            f"回执异常：retcode 无法解析({summarize_error(raw_retcode)!r})，无法确认协议端是否已执行"
        )
    if retcode != 0:
        return _fail(_err_text(f"retcode={retcode}"))

    return ApiCallResult(True, resp.get("data"), "", OUTCOME_OK)


async def api_call_ex(ws_server, bot_id: str, action: str, params: dict | None = None,
                      timeout: float = 10.0) -> tuple[bool, Any, str]:
    """调用 OneBot API, 返回 (是否成功, data, 错误文本)。

    失败时错误文本可用于回给审批人; api_call 只在内部丢弃它。
    需要区分"明确失败"与"未取得确认"时用 api_call_checked。
    """
    result = await api_call_checked(ws_server, bot_id, action, params, timeout)
    return result.ok, result.data, result.error


async def api_call(ws_server, bot_id: str, action: str, params: dict | None = None,
                   timeout: float = 10.0) -> Any:
    """调用 OneBot API 并返回响应 data(任意类型; 失败/超时返回 None)。

    兼容不同实现的 retcode 类型(str/int)与缺失 status 的情况。
    """
    result = await api_call_checked(ws_server, bot_id, action, params, timeout)
    return result.data if result.ok else None


async def get_nickname(ws_server, bot_id: str, group_id: str | int, user_id: str | int) -> str:
    """获取指定群友的群昵称或 Q 名, 群接口失败自动降级到陌生人资料。"""
    user_id = int(user_id)
    info: dict = {}

    if str(group_id).isdigit():
        data = await api_call(
            ws_server, bot_id, "get_group_member_info",
            {"group_id": int(group_id), "user_id": user_id},
        )
        if data:
            info = data

    if not info:
        data = await api_call(ws_server, bot_id, "get_stranger_info", {"user_id": user_id})
        if data:
            info = data

    return info.get("card") or info.get("nickname") or info.get("nick") or str(user_id)


def get_ats(event: Any, noself: bool = False) -> list[str]:
    """获取消息中 @ 的用户 id 列表(mohobot message 段格式)。"""
    ats: set[str] = set()
    message = getattr(event, "message", None)
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = str(seg.get("data", {}).get("qq", ""))
                if qq and qq != "all":
                    ats.add(qq)
    if noself:
        ats.discard(str(getattr(event, "self_id", "")))
    return list(ats)


def get_reply_id(event: Any) -> str | None:
    """获取引用(reply)段的消息 id。"""
    message = getattr(event, "message", None)
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                return str(seg.get("data", {}).get("id", ""))
    return None


async def get_reply_text(ws_server, bot_id: str, event: Any) -> str:
    """获取被引用消息的纯文本(通过 get_msg 接口)。"""
    reply_id = get_reply_id(event)
    if not reply_id:
        return ""
    try:
        mid = int(reply_id)
    except (TypeError, ValueError):
        logger.warning(f"非法引用消息 id: {reply_id!r}")
        return ""
    data = await api_call(ws_server, bot_id, "get_msg", {"message_id": mid})
    if not data:
        return ""
    message = data.get("message") or []
    text = ""
    if isinstance(message, str):
        return message
    for seg in message:
        if isinstance(seg, dict) and seg.get("type") == "text":
            text += seg.get("data", {}).get("text", "")
    return text


def get_message_text(event: Any) -> str:
    """提取事件消息的纯文本。"""
    message = getattr(event, "message", None)
    if isinstance(message, str):
        return message.strip()
    text = ""
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "text":
                text += seg.get("data", {}).get("text", "")
    return text.strip()


def parse_multi_input(raw: str, total: int) -> tuple[set[int], set[str]]:
    """
    解析文本参数, 支持:
    - 空格分隔 / 序号 / 区间(1~5 / 1-5) / 直接 ID(QQ/群号)
    返回: (indexes: 0-based 索引集合, ids: 明确 ID 集合)
    """
    indexes: set[int] = set()
    ids: set[str] = set()

    for token in (raw or "").strip().split():
        token = token.strip()
        if not token:
            continue

        m = re.fullmatch(r"(\d+)\s*[~-]\s*(\d+)", token)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                if 1 <= i <= total:
                    indexes.add(i - 1)
            continue

        if token.isdigit():
            num = int(token)
            if 1 <= num <= total:
                indexes.add(num - 1)
            else:
                ids.add(token)

    return indexes, ids
