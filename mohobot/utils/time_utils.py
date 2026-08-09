"""时间工具 — 强制 UTC+8(东八区), 不依赖系统时区设置。

所有展示给用户/LLM 的时间统一走这里, 保证无论服务器时区如何
(UTC/CST/其他), 框架内时间都是北京时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ_UTC8 = timezone(timedelta(hours=8))


def now_utc8() -> datetime:
    """当前 UTC+8 时间(aware datetime)。"""
    return datetime.now(TZ_UTC8)


def format_utc8(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """当前 UTC+8 时间的格式化字符串。"""
    return now_utc8().strftime(fmt)


def to_utc8(dt: datetime) -> datetime:
    """把任意 datetime 转成 UTC+8(naive 视为 UTC)。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_UTC8)
