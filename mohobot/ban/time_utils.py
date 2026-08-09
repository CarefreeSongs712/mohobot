"""时间解析/格式化 — 移植自 reneban (time_utils.py)。

时间字符串: 1d(天) 2h(小时) 30m(分钟) 10s(秒), 可组合如 1d2h30m;
"0" = 永久。到期时间戳: 0 表示永久。
"""

from __future__ import annotations

import re

_TIME_RE = re.compile(
    r"^(?=.*\d)(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s?)?$"
)


def timestr_to_int(timestr: str) -> int:
    """把时间字符串(如 1d2h30m)转为秒数; '0' → 0(永久)。"""
    if timestr in ("0", "", "永久"):
        return 0
    m = _TIME_RE.fullmatch(timestr.strip())
    if not m:
        raise ValueError(f"非法的时间字符串格式: {timestr!r}")
    parts = {k: int(v or 0) for k, v in m.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _fmt_seconds(total: int) -> str:
    """把秒数格式化为易读时间, 0 → '永久'。"""
    if total <= 0:
        return "永久"
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    result = []
    if days > 0:
        result.append(f"{days}天")
    if hours > 0:
        result.append(f"{hours}小时")
    if minutes > 0:
        result.append(f"{minutes}分钟")
    if seconds > 0 or not result:
        result.append(f"{seconds}秒")
    return "".join(result)


def time_format(time_str: str) -> str:
    """把时间字符串格式化为易读描述(用于操作回执)。"""
    return _fmt_seconds(timestr_to_int(time_str))


def timelast_format(time_last: int) -> str:
    """把剩余秒数格式化为易读描述(time_last < 0 = 已过期)。"""
    if time_last < 0:
        return "已过期"
    return _fmt_seconds(time_last)
