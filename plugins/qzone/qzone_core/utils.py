"""消息解析工具（移植自 astrbot_plugin_qzone_lite core/utils.py，改为纯函数）。

mohobot 侧不再依赖 AstrMessageEvent，直接接收消息文本与 @ 目标列表。
"""

from __future__ import annotations

from typing import Any


def parse_range_from_tokens(tokens: list[str]) -> tuple[int, int, int | None]:
    """在 tokens 中寻找第一个范围 token，返回 (pos, num, idx)。

    - n        → pos=n(允许 0)，num=1
    - s~e      → pos=s-1,num=e-s+1（兼容 s=0 的情况）
    - 未找到    → pos=0,num=1,idx=None
    """
    for i, tok in enumerate(tokens):
        if "~" in tok:
            s, _, e = tok.partition("~")
            if s.isdigit() and e.isdigit():
                s_i = int(s)
                e_i = int(e)
                if e_i < s_i:
                    continue
                if s_i == 0:
                    return 0, e_i - s_i + 1, i
                return s_i - 1, e_i - s_i + 1, i
        elif tok.isdigit():
            n = int(tok)
            return (n - 1 if n > 0 else 0), 1, i
    return 0, 1, None


def parse_range(text: str) -> tuple[int, int]:
    """从完整命令文本中解析序号/范围（扫描全部 token，与原版一致）。"""
    parts = text.strip().split()
    pos, num, _ = parse_range_from_tokens(parts)
    return pos, num


def extract_at_ids(message: Any, bot_qq: str | int = "") -> list[str]:
    """从 OneBot 消息段中提取 @ 目标 QQ 列表。

    - at 段: 排除 @全体 与 @bot 自己（触发用 @）
    - 文本 token: "@123" 数字形式
    """
    ids: list[str] = []
    bot_str = str(bot_qq) if bot_qq else ""
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = str((seg.get("data") or {}).get("qq", "") or "")
                if not qq or qq == "all" or (bot_str and qq == bot_str):
                    continue
                ids.append(qq)
    # 文本中的 "@123"
    from mohobot.utils.cq_code import extract_plain_text

    for tok in extract_plain_text(message).split():
        if tok.startswith("@") and tok[1:].isdigit():
            ids.append(tok[1:])
    return ids


def parse_comment_args(text: str, at_ids: list[str]) -> tuple[str | None, int, int, str]:
    """解析 `/评说说 [@用户] [序号/范围] <内容>` → (target_id, pos, num, content)"""
    tokens = text.strip().split()
    if not tokens:
        return None, 0, 1, ""

    tokens = tokens[1:]  # 去掉命令本身
    target_id = at_ids[0] if at_ids else None
    filtered = [t for t in tokens if not (t.startswith("@") and t[1:].isdigit())]

    pos, num, idx = parse_range_from_tokens(filtered)
    content_tokens = filtered if idx is None else filtered[idx + 1:]
    content = " ".join(content_tokens).strip()
    return target_id, pos, num, content


def parse_reply_args(text: str, at_ids: list[str]) -> tuple[str | None, int, int, str]:
    """解析 `/回评 [@用户] [说说序号] [评论序号] <内容>` → (target_id, pos, comment_index, content)"""
    tokens = text.strip().split()
    if not tokens:
        return None, 0, -1, ""

    tokens = tokens[1:]
    target_id = at_ids[0] if at_ids else None
    filtered = [t for t in tokens if not (t.startswith("@") and t[1:].isdigit())]

    # 说说序号
    pos, _, idx = parse_range_from_tokens(filtered)
    if idx is None:
        return target_id, 0, -1, ""

    # 评论序号
    if idx + 1 >= len(filtered):
        return target_id, pos, -1, ""
    comment_tok = filtered[idx + 1]
    comment_index = int(comment_tok) if comment_tok.lstrip("-").isdigit() else -1

    content = " ".join(filtered[idx + 2:]).strip()
    return target_id, pos, comment_index, content
