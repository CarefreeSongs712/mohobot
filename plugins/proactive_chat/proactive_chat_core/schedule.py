"""主动聊天调度辅助: 免打扰判断、随机窗口与提示词渲染(纯函数, 便于测试)。

移植自 astrbot_plugin_proactive_chat (AGPL-3.0) 的 utils/time_utils.py 与
提示词占位符逻辑, 适配 mohobot 的 UTC+8 时间工具。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from mohobot.utils.time_utils import format_utc8, now_utc8


def is_quiet_time(quiet_hours: str, now: datetime | None = None) -> bool:
    """当前时间是否处于免打扰时段("开始小时-结束小时", 24 小时制, 支持跨天)。

    配置非法时按"非免打扰"处理, 避免误阻断主动消息。
    """
    try:
        start_str, end_str = str(quiet_hours or "").split("-")
        start_hour, end_hour = int(start_str), int(end_str)
        current = now or now_utc8()
        if start_hour <= end_hour:
            return start_hour <= current.hour < end_hour
        # 跨天区间, 如 23-7
        return current.hour >= start_hour or current.hour < end_hour
    except (ValueError, TypeError):
        return False


def next_non_quiet_time(quiet_hours: str, when: datetime) -> datetime:
    """返回 when 及其之后第一个非免打扰时刻(按整点对齐)。

    用于把 LLM 指定的未来时间挪出免打扰时段; 配置非法时原样返回,
    避免把任务排到不可预期的时段。
    """
    try:
        start_str, end_str = str(quiet_hours or "").split("-")
        int(start_str)
        int(end_str)
    except (ValueError, TypeError):
        return when
    candidate = when.replace(minute=0, second=0, microsecond=0)
    if candidate < when:
        candidate += timedelta(hours=1)
    for _ in range(26):
        if not is_quiet_time(quiet_hours, candidate):
            return candidate
        candidate += timedelta(hours=1)
    return when


def random_interval_seconds(min_interval_minutes: int, max_interval_minutes: int) -> int:
    """在 [min, max] 分钟内随机取下一次触发间隔(秒), 降低触发规律性。"""
    import random

    lo = max(1, int(min_interval_minutes)) * 60
    hi = max(lo, int(max_interval_minutes) * 60)
    return random.randint(lo, hi)


def render_prompt(template: str, unanswered_count: int,
                  max_schedule_days: int | None = None) -> str:
    """渲染主动消息提示词模板, 支持 {{unanswered_count}} / {{current_time}} /
    {{max_schedule_days}}。"""
    text = str(template or "")
    text = (
        text.replace("{{unanswered_count}}", str(int(unanswered_count)))
        .replace("{{current_time}}", format_utc8("%Y-%m-%d %H:%M:%S %A"))
    )
    if max_schedule_days is not None:
        text = text.replace("{{max_schedule_days}}", str(int(max_schedule_days)))
    return text
