"""核心逻辑 — 抽取、记录、冷却、清理(操作 WifeStore)。

移植自 astrbot_plugin_wifepicker src/core.py, 去掉 AstrBot 依赖,
数据读写统一走 WifeStore(内存 + 原子落盘)。
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any, Set

from loguru import logger

from .store import WifeStore


# ── 活跃池 ──────────────────────────────────────────────────

def count_active_users(store: WifeStore) -> int:
    total = 0
    for users in store.active_users.values():
        if isinstance(users, dict):
            total += len(users)
    return total


def get_active_user_days(config: dict) -> int:
    raw = config.get("active_user_days", 30)
    try:
        days = int(float(raw))
    except Exception:
        days = 30
    return min(30, max(1, days))


def get_max_records(config: dict) -> int:
    try:
        return max(0, int(config.get("max_records", 500)))
    except Exception:
        return 500


def record_active(store: WifeStore, group_id: str, user_id: str, bot_id: str) -> None:
    """记录群内发言 → 活跃池。"""
    uid, bot = str(user_id), str(bot_id)
    if uid == bot or uid == "0":
        return
    group = store.active_users.setdefault(group_id, {})
    if not isinstance(group, dict):
        group = {}
        store.active_users[group_id] = group
    group[uid] = time.time()
    store.mark_dirty()


def cleanup_inactive(store: WifeStore, config: dict, group_id: str | None = None) -> int:
    """清理活跃池中超过 N 天未发言的用户, 返回移除数。"""
    days = get_active_user_days(config)
    now, limit = time.time(), days * 24 * 3600
    if group_id is not None:
        group_ids = [str(group_id)]
    else:
        group_ids = list(store.active_users.keys())

    removed = 0
    for gid in group_ids:
        group = store.active_users.get(gid)
        if not isinstance(group, dict):
            store.active_users.pop(gid, None)
            removed += 1
            continue
        new_group = {
            uid: ts
            for uid, ts in group.items()
            if uid != "0" and _is_recent_ts(ts, now=now, limit=limit)
        }
        if len(new_group) != len(group):
            removed += len(group) - len(new_group)
            if new_group:
                store.active_users[gid] = new_group
            else:
                store.active_users.pop(gid, None)
    if removed:
        store.mark_dirty()
    return removed


def trim_active_users(store: WifeStore, config: dict) -> None:
    """活跃池总量超限时按最沉默优先淘汰(max_records)。"""
    max_total = get_max_records(config)
    current = count_active_users(store)
    if current <= max_total:
        return
    # 收集所有 (ts, gid, uid), 保留最活跃的 max_total 个
    entries: list[tuple[float, str, str]] = []
    for gid, group in store.active_users.items():
        if not isinstance(group, dict):
            continue
        for uid, ts in group.items():
            try:
                entries.append((float(ts), str(gid), str(uid)))
            except (TypeError, ValueError):
                continue
    entries.sort(reverse=True)
    keep = entries[:max_total]
    new_active: dict[str, dict] = {}
    for _, gid, uid in keep:
        new_active.setdefault(gid, {})[uid] = store.active_users[gid][uid]
    store.active_users.clear()
    store.active_users.update(new_active)
    store.mark_dirty()
    logger.info(f"wifepicker: 活跃池超限, 裁剪至 {max_total} 条")


# ── 时间戳工具 ──────────────────────────────────────────────

def _is_recent_ts(ts: object, *, now: float, limit: float) -> bool:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return False
    # 兼容毫秒时间戳
    while value > now + 86400 and value > 10_000_000_000:
        value = value / 1000
    return now - value < limit


# ── 今日记录 ────────────────────────────────────────────────

def ensure_today_records(store: WifeStore) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    if store.records.get("date") != today:
        store.records["date"] = today
        store.records["groups"] = {}
        store.mark_dirty()


def get_group_records(store: WifeStore, group_id: str) -> list[dict]:
    ensure_today_records(store)
    groups = store.records.setdefault("groups", {})
    if group_id not in groups or not isinstance(groups[group_id], dict):
        groups[group_id] = {"records": []}
    return groups[group_id]["records"]


def upsert_user_wife_record(
    records: list,
    *,
    user_id: str,
    wife_id: str,
    wife_name: str,
    timestamp: str,
    daily_limit: int,
    forced: bool = True,
) -> None:
    """写入/更新用户今日老婆记录(每日上限 >1 时随机覆盖一条)。"""
    new_record = {
        "user_id": str(user_id),
        "wife_id": str(wife_id),
        "wife_name": wife_name,
        "timestamp": timestamp,
        "forced": forced,
    }
    user_indexes = [
        i for i, r in enumerate(records)
        if str(r.get("user_id")) == str(user_id)
    ]
    if daily_limit > 1 and user_indexes:
        records[random.choice(user_indexes)] = new_record
        return
    records[:] = [r for r in records if str(r.get("user_id")) != str(user_id)]
    records.append(new_record)


def maybe_add_other_half_record(
    records: list,
    *,
    user_id: str,
    user_name: str,
    wife_id: str,
    wife_name: str,
    enabled: bool,
    timestamp: str,
) -> bool:
    """自动设置对方老婆(对方当天无记录时生效)。"""
    if not enabled:
        return False
    if any(str(r.get("user_id")) == str(wife_id) for r in records):
        return False
    records.append({
        "user_id": str(wife_id),
        "wife_id": str(user_id),
        "wife_name": str(user_name),
        "timestamp": timestamp,
        "auto_set": True,
        "auto_set_target_name": str(wife_name),
    })
    return True


# ── 冷却 ────────────────────────────────────────────────────

def get_force_marry_cd_days(config: dict) -> int:
    try:
        return max(0, int(config.get("force_marry_cd", 3)))
    except Exception:
        return 3


def get_force_marry_cooldown_status(
    store: WifeStore, group_id: str, user_id: str, config: dict
) -> dict | None:
    """强娶冷却: 从最后一次强娶时间所在日期的午夜起算 N 天。"""
    last_time = store.forced_marriage.setdefault(group_id, {}).get(str(user_id))
    if not isinstance(last_time, (int, float)):
        return None
    last_dt = datetime.fromtimestamp(last_time)
    cd_days = get_force_marry_cd_days(config)
    reset_dt = datetime.combine(last_dt.date(), datetime.min.time()) + timedelta(days=cd_days)
    try:
        reset_ts = reset_dt.timestamp()
    except (OSError, OverflowError):
        reset_ts = 0
    remaining = reset_ts - time.time()
    if remaining <= 0:
        return None
    return {
        "action": "force_marry",
        "last_time": last_time,
        "reset_at": reset_ts,
        "reset_dt": reset_dt,
        "remaining": remaining,
        "cd_days": cd_days,
    }


def get_propose_cooldown_seconds(config: dict) -> int:
    try:
        minutes = int(float(config.get("propose_cooldown_minutes", 60)))
    except Exception:
        minutes = 60
    return max(0, minutes) * 60


def get_propose_cooldown_status(
    store: WifeStore, group_id: str, user_id: str
) -> dict | None:
    """求婚冷却(当天记录, 过期自动清除)。"""
    record = store.marriage_actions.get(group_id, {}).get(str(user_id))
    if not isinstance(record, dict):
        return None
    action = record.get("action")
    expire_at = record.get("expire_at")
    if action != "propose" or not isinstance(expire_at, (int, float)):
        _remove_marriage_action(store, group_id, user_id)
        return None
    remaining = expire_at - time.time()
    if remaining <= 0:
        _remove_marriage_action(store, group_id, user_id)
        return None
    return {
        "action": "propose",
        "start_at": record.get("start_at"),
        "expire_at": expire_at,
        "remaining": remaining,
        "role": record.get("role"),
        "related_user_id": record.get("related_user_id"),
    }


def set_propose_cooldown(
    store: WifeStore,
    group_id: str,
    user_id: str,
    *,
    related_user_id: str,
    role: str,
    config: dict,
    now: float | None = None,
) -> None:
    start_at = time.time() if now is None else now
    cooldown_seconds = get_propose_cooldown_seconds(config)
    if cooldown_seconds <= 0:
        _remove_marriage_action(store, group_id, user_id)
        return
    group = store.marriage_actions.setdefault(group_id, {})
    group[str(user_id)] = {
        "action": "propose",
        "start_at": start_at,
        "expire_at": start_at + cooldown_seconds,
        "related_user_id": related_user_id,
        "role": role,
    }


def _remove_marriage_action(store: WifeStore, group_id: str, user_id: str) -> None:
    group = store.marriage_actions.get(group_id)
    if not isinstance(group, dict):
        return
    group.pop(str(user_id), None)
    if not group:
        store.marriage_actions.pop(group_id, None)


# ── rbq 统计 ────────────────────────────────────────────────

def clean_rbq_stats(store: WifeStore) -> None:
    """清理 rbq 统计: 只保留 30 天内 + 活跃相关的记录。"""
    now = time.time()
    thirty_days = 30 * 24 * 3600
    seven_days = 7 * 24 * 3600
    five_days = 5 * 24 * 3600

    changed = False
    new_stats: dict[str, dict] = {}
    for gid, users in store.rbq_stats.items():
        if not isinstance(users, dict):
            continue
        active_group = store.active_users.get(gid, {})
        if not isinstance(active_group, dict):
            active_group = {}
        new_users: dict[str, list] = {}
        for uid, timestamps in users.items():
            valid_ts = [ts for ts in timestamps if now - ts < thirty_days]
            if not valid_ts:
                continue
            last_forced_ts = max(valid_ts)
            is_in_active = uid in active_group
            last_active_ts = active_group.get(uid, 0)
            should_keep = True
            if not is_in_active:
                if last_active_ts == 0:
                    if now - last_forced_ts > five_days:
                        should_keep = False
                elif len(valid_ts) <= 4 and (now - last_active_ts > seven_days):
                    should_keep = False
            if should_keep:
                new_users[uid] = valid_ts
        if new_users:
            new_stats[gid] = new_users
    if new_stats != store.rbq_stats:
        changed = True
    store.rbq_stats.clear()
    store.rbq_stats.update(new_stats)
    if changed:
        store.mark_dirty()


# ── 排除用户 ────────────────────────────────────────────────

def normalize_user_id_set(values: Any) -> Set[str]:
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {str(v) for v in values if str(v).strip()}


def draw_excluded_users(config: dict) -> Set[str]:
    return normalize_user_id_set(config.get("excluded_users", []))


def force_marry_excluded_users(config: dict) -> Set[str]:
    return normalize_user_id_set(config.get("force_marry_excluded_users", []))


# ── 配置读取 ────────────────────────────────────────────────

def get_daily_limit(config: dict) -> int:
    try:
        return max(1, int(config.get("daily_limit", 1)))
    except Exception:
        return 1


def format_remaining_seconds(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    mins = (total_seconds % 3600) // 60
    if days > 0:
        return f"{days}天{hours}小时{mins}分"
    if hours > 0:
        return f"{hours}小时{mins}分"
    if mins > 0:
        return f"{mins}分{total_seconds % 60}秒"
    return f"{total_seconds}秒"
