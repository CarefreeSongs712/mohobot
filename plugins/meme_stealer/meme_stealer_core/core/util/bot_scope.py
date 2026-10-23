"""表情包「适用 bot」标注与过滤(REQ-3 第三点)。

语义:
- `allowed_bots` 存为**逗号分隔**的 `bot_id` 白名单(数据库列与索引条目同格式);
- **空串 = 全部 bot 可用**(向后兼容: 老数据迁移后即为空串, 行为不变);
- 含 `*` 同样表示全部 bot 可用;
- 非空列表表示只有列出的 bot 可以检索/发送该表情包。

多 bot 部署下, 每个 bot 有独立人设, 表情包应按角色分流, 避免"傲娇角色
发温柔表情"这类违和。
"""

from __future__ import annotations

from typing import Any, Iterable

# 全部 bot 可用的标记
ALL_BOTS = "*"


def normalize_allowed_bots(value: Any) -> list[str]:
    """把多种输入形态归一化为去重后的 bot_id 列表(保持顺序)。

    接受: None / "" / "*" / "bot_001,bot_002" / ["bot_001", "bot_002"]。
    空结果表示"全部 bot 可用"。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text or text == ALL_BOTS:
            return []
        raw = text.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            if isinstance(item, str):
                raw.extend(item.replace("，", ",").split(","))
            elif item is not None:
                raw.append(str(item))
    else:
        raw = [str(value)]

    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        bot_id = str(item).strip()
        if not bot_id or bot_id in seen:
            continue
        seen.add(bot_id)
        out.append(bot_id)
    if ALL_BOTS in out:
        return []
    return out


def format_allowed_bots(value: Any) -> str:
    """归一化后序列化为数据库/索引用的逗号分隔字符串。"""
    return ",".join(normalize_allowed_bots(value))


def is_allowed_for_bot(value: Any, bot_id: str) -> bool:
    """该表情包是否允许指定 bot 使用。

    空白名单(或含 `*`)= 全部 bot 可用; 白名单非空时要求 bot_id 精确命中。
    bot_id 缺失时按"全部可用"处理(兼容无 bot 维度的旧调用)。
    """
    allowed = normalize_allowed_bots(value)
    if not allowed:
        return True
    current = str(bot_id or "").strip()
    if not current:
        return True
    return current in allowed


def describe_allowed_bots(value: Any) -> str:
    """人类可读描述(WebUI/日志用)。"""
    allowed = normalize_allowed_bots(value)
    return ", ".join(allowed) if allowed else "全部 bot"


def merge_allowed_bots(existing: Any, extra: Iterable[str]) -> list[str]:
    """合并两组白名单; 任一方为空(全部可用)则结果为空(全部可用)。"""
    current = normalize_allowed_bots(existing)
    incoming = normalize_allowed_bots(list(extra or []))
    if not current or not incoming:
        return []
    merged = list(current)
    for bot_id in incoming:
        if bot_id not in merged:
            merged.append(bot_id)
    return merged
