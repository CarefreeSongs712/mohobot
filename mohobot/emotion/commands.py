"""情感系统聊天命令 — 用户命令 + 管理员命令。

返回纯文本(由 CommandHandler 拦截器发送); 管理员 = GlobalConfig.admins。
"""

from __future__ import annotations

from typing import Any, Callable

from loguru import logger

from .relationship import StageManager

ADMIN_HINT = "该命令仅管理员可用。"


def _user_id(event) -> str:
    return str(getattr(event, "user_id", "") or "")


def build_command_registry(manager) -> dict[str, tuple[Callable, str]]:
    """返回 CommandHandler 可直接合并的 {命令名: (handler, help)} 注册表。"""

    async def cmd_favor(bot_id: str, event, args) -> str:
        state = await manager.get_state(bot_id, _user_id(event))
        lines = [
            "💗 好感度",
            f"好感度: {state.favor} | 亲密度: {state.intimacy}",
            f"关系阶段: {state.relationship_stage}",
            f"主导情感: {state.emotions.get_dominant()}",
            f"态度倾向: {state.descriptions.attitude}",
            f"关系描述: {state.descriptions.relationship}",
            f"互动次数: {state.stats.total_count}(正面 {state.stats.positive_count} / 负面 {state.stats.negative_count})",
        ]
        return "\n".join(lines)

    async def cmd_stage(bot_id: str, event, args) -> str:
        state = await manager.get_state(bot_id, _user_id(event))
        info = StageManager.get_stage_info(state)
        lines = [
            "👫 关系阶段",
            f"当前阶段: {info['stage_name']} — {info['description']}",
            f"复合评分: {info['composite_score']:.1f}"
            f"(阶段阈值 {info['current_stage_threshold']})",
        ]
        if info["is_max_stage"]:
            lines.append("已达最高阶段。")
        elif info["next_stage_name"]:
            lines.append(
                f"下一阶段: {info['next_stage_name']}"
                f"(阈值 {info['next_stage_threshold']}, 进度 {info['progress_to_next']:.0f}%)"
            )
        lines.append(StageManager.get_stage_advice(state))
        return "\n".join(lines)

    def _fmt_rank_entry(rank: int, entry) -> str:
        user_key, avg, favor, intimacy, state = entry
        return (
            f"{rank}. 用户{user_key} — 好感 {favor} / 亲密 {intimacy} "
            f"(综合 {avg:.1f}, {state.relationship_stage})"
        )

    async def cmd_rank(bot_id: str, event, args) -> str:
        limit = _parse_limit(args)
        entries = await manager.ranking(bot_id, limit=limit, reverse=True)
        if not entries:
            return "暂无好感度数据。"
        lines = ["🏆 好感度排行"] + [
            _fmt_rank_entry(i, e) for i, e in enumerate(entries, 1)
        ]
        return "\n".join(lines)

    async def cmd_negative_rank(bot_id: str, event, args) -> str:
        limit = _parse_limit(args)
        entries = await manager.ranking(bot_id, limit=limit, reverse=False)
        entries = [e for e in entries if e[2] <= 0] or entries
        if not entries:
            return "暂无好感度数据。"
        lines = ["💔 负好感排行"] + [
            _fmt_rank_entry(i, e) for i, e in enumerate(entries, 1)
        ]
        return "\n".join(lines)

    # ── 管理员命令 ───────────────────────────────────────────

    def _admin_guard(event) -> str | None:
        if not manager.is_admin(_user_id(event)):
            return ADMIN_HINT
        return None

    async def cmd_set_favor(bot_id: str, event, args) -> str:
        if err := _admin_guard(event):
            return err
        parsed = _parse_two_ints(args)
        if parsed is None:
            return "用法: /设置好感 <QQ> <数值(-100~100)>"
        qq, value = parsed
        state = await manager.set_favor(bot_id, qq, value)
        return f"已设置 用户{qq} 好感度为 {state.favor}(阶段: {state.relationship_stage})"

    async def cmd_set_intimacy(bot_id: str, event, args) -> str:
        if err := _admin_guard(event):
            return err
        parsed = _parse_two_ints(args)
        if parsed is None:
            return "用法: /设置亲密 <QQ> <数值(0~100)>"
        qq, value = parsed
        state = await manager.set_intimacy(bot_id, qq, value)
        return f"已设置 用户{qq} 亲密度为 {state.intimacy}"

    async def cmd_set_attitude(bot_id: str, event, args) -> str:
        if err := _admin_guard(event):
            return err
        if len(args) < 2:
            return "用法: /设置态度 <QQ> <描述文本(≤20字)>"
        qq = args[0].strip()
        if not qq.isdigit():
            return "用法: /设置态度 <QQ> <描述文本(≤20字)>"
        text = args[1].strip()
        state, ok = await manager.set_attitude(bot_id, qq, text)
        if not ok:
            return "设置失败: 文本含不支持的字符或为空(支持汉字/字母数字/常用中文标点)。"
        return f"已设置 用户{qq} 态度描述为「{state.descriptions.attitude}」"

    async def cmd_reset_favor(bot_id: str, event, args) -> str:
        if err := _admin_guard(event):
            return err
        if not args or not args[0].strip().isdigit():
            return "用法: /重置好感 <QQ>"
        qq = args[0].strip()
        state = await manager.reset_user(bot_id, qq)
        return f"已重置 用户{qq} 的情感状态(好感 {state.favor} / 亲密 {state.intimacy}, 阶段: {state.relationship_stage})"

    async def cmd_view_favor(bot_id: str, event, args) -> str:
        if err := _admin_guard(event):
            return err
        if not args or not args[0].strip().isdigit():
            return "用法: /查看好感 <QQ>"
        qq = args[0].strip()
        state = await manager.get_state(bot_id, qq)
        mem = manager._memory.user_memory_stats(bot_id, str(qq))
        return (
            f"🔍 用户{qq} 情感状态\n"
            f"好感度: {state.favor} | 亲密度: {state.intimacy}\n"
            f"关系阶段: {state.relationship_stage}\n"
            f"态度: {state.descriptions.attitude} | 关系: {state.descriptions.relationship}\n"
            f"互动: 共 {state.stats.total_count} 次(正面比例 {state.stats.positive_ratio:.0f}%)\n"
            f"长期记忆: {mem['long_term_count']} 条"
        )

    async def cmd_emotion_reset(bot_id: str, event, args) -> str:
        if err := _admin_guard(event):
            return err
        await manager.clear_bot(bot_id)
        logger.info(f"情感数据已全部重置(bot={bot_id}, 操作者={_user_id(event)})")
        return "已清空本 bot 的全部情感数据(好感/亲密/记忆), 所有用户从头开始。"

    return {
        "好感度":   (cmd_favor,   "查看你与当前 bot 的好感度/亲密度"),
        "关系阶段": (cmd_stage,   "查看当前关系阶段与进阶建议"),
        "好感排行": (cmd_rank,    "好感度排行榜 | 用法: /好感排行 [数量]"),
        "负好感排行": (cmd_negative_rank, "负好感排行榜 | 用法: /负好感排行 [数量]"),
        "设置好感": (cmd_set_favor, "设置好感度(管理员) | 用法: /设置好感 <QQ> <数值>"),
        "设置亲密": (cmd_set_intimacy, "设置亲密度(管理员) | 用法: /设置亲密 <QQ> <数值>"),
        "设置态度": (cmd_set_attitude, "设置态度描述(管理员) | 用法: /设置态度 <QQ> <文本>"),
        "重置好感": (cmd_reset_favor, "重置某用户的情感状态(管理员) | 用法: /重置好感 <QQ>"),
        "查看好感": (cmd_view_favor, "查看某用户情感状态(管理员) | 用法: /查看好感 <QQ>"),
        "情感重置": (cmd_emotion_reset, "清空本 bot 全部情感数据(管理员, 慎用)"),
    }


def _parse_limit(args: list[str], default: int = 10) -> int:
    if args and args[0].strip().isdigit():
        return max(1, min(20, int(args[0].strip())))
    return default


def _parse_two_ints(args: list[str]) -> tuple[int, int] | None:
    if len(args) < 2:
        return None
    qq, value = args[0].strip(), args[1].strip()
    if not qq.isdigit() or not _is_int(value):
        return None
    return int(qq), int(value)


def _is_int(text: str) -> bool:
    try:
        int(text)
        return True
    except ValueError:
        return False
