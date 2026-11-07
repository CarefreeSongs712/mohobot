"""Mohobot /status plugin — 系统与框架状态卡片。

用法: /status 或 /状态

深色卡片包含: CPU 每核方块、内存/缓存/Swap 堆叠条、主磁盘占用、Bot 连接
(昵称 + 在线时长)、管理员/交流群/使用须知页脚。图片生成或发送失败时降级为文本。

配置(WebUI 插件页, 各 bot 通用): admin_qqs / community_group / help_url。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from loguru import logger

from mohobot.utils.contact_info import (
    DEFAULT_COMMUNITY_GROUP,
    DEFAULT_HELP_URL,
    resolve_contacts,
)

# Optional psutil for detailed system stats
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class Plugin:
    """Plugin that responds to /status with a status card."""

    # 全局指令: 群内多 bot 时只由随机选中的一个 bot 回复(框架去重)
    global_triggers = {"/status", "/状态"}

    info = {
        "commands": [
            {
                "name": "status",
                "desc": "显示框架与系统状态",
                "usage": "/status",
            },
        ],
    }

    _DEFAULTS = {
        "admin_qqs": [],
        "community_group": DEFAULT_COMMUNITY_GROUP,
        "help_url": DEFAULT_HELP_URL,
    }

    _bot_manager = None
    _ws_server = None
    _admin_ids: list[str] = []

    def __init__(self):
        self._start_time = time.time()
        self.plugin_config: dict = dict(self._DEFAULTS)

    # ── 注入(必须 classmethod) ────────────────────────────────

    @classmethod
    def inject_bot_manager(cls, bot_manager) -> None:
        cls._bot_manager = bot_manager

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_admin_ids(cls, admin_ids) -> None:
        cls._admin_ids = [str(a) for a in (admin_ids or [])]

    # ── 配置 ─────────────────────────────────────────────────

    def _cfg(self, key: str, default):
        value = (getattr(self, "plugin_config", None) or {}).get(key, default)
        return value if value not in (None, "") else default

    # ── 消息入口 ──────────────────────────────────────────────

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Check for /status and return the status card if found."""
        text = _extract_text(event)
        if not (text == "/status" or text == "/状态"
                or text.startswith("/status ") or text.startswith("/状态 ")):
            return (False, None)

        data = await self._collect_status(bot_id)
        ws = getattr(self, "_ws_server", None)
        if ws is None:
            # 未装配发送端时不渲染, 避免生成后无法发送的临时文件残留。
            return (True, _text_report(data))

        from mohobot.utils.status_card import render_status_card

        try:
            img_path = await asyncio.to_thread(render_status_card, data)
        except Exception as e:
            logger.warning(f"渲染状态图片失败, 降级为文本: {e}")
            return (True, _text_report(data))
        if img_path is None:
            return (True, _text_report(data))

        try:
            chat_type, chat_id = _event_chat(event)
            await ws.send_image(bot_id, chat_type, chat_id, img_path)
            return (True, None)  # 已发送图片, 无需文本
        except Exception as e:
            logger.warning(f"发送状态图片失败, 降级为文本: {e}")
            return (True, _text_report(data))
        finally:
            try:
                os.remove(img_path)
            except OSError:
                pass

    async def on_notice(self, bot_id: str, event: Any, raw: dict) -> None:
        pass

    async def on_meta(self, bot_id: str, event: Any, raw: dict) -> None:
        pass

    # ── 数据采集 ──────────────────────────────────────────────

    async def _collect_status(self, bot_id: str) -> dict[str, Any]:
        now = _now_text()
        bots = self._bot_list()
        self_nickname = next(
            (b["nickname"] for b in bots if b["bot_id"] == bot_id), bot_id,
        )
        data: dict[str, Any] = {
            "bot_id": bot_id,
            "nickname": self_nickname,
            "now": now,
            "runtime_secs": time.time() - self._start_time,
            "plugin_runtime_secs": time.time() - self._start_time,
            "system_uptime_secs": 0.0,
            "cpu": {},
            "memory": {},
            "disks": [],
            "bots": bots,
            "contacts": resolve_contacts(
                {
                    "admin_qqs": self._cfg("admin_qqs", []),
                    "community_group": self._cfg("community_group", DEFAULT_COMMUNITY_GROUP),
                    "help_url": self._cfg("help_url", DEFAULT_HELP_URL),
                },
                fallback_admins=self._admin_ids,
            ),
        }

        if not HAS_PSUTIL:
            return data

        try:
            data["system_uptime_secs"] = max(0.0, time.time() - psutil.boot_time())
        except Exception as e:
            logger.debug(f"读取系统运行时间失败: {e}")

        cpu = await asyncio.to_thread(_read_cpu)
        data["cpu"] = cpu
        data["memory"] = _read_memory()
        data["disks"] = _read_disks()
        return data

    def _bot_list(self) -> list[dict[str, Any]]:
        bm = self._bot_manager
        if not bm:
            return []
        bots = []
        for b in sorted(bm.all_bots, key=lambda item: item.bot_id):
            # 昵称为空时按需求回退 QQ
            nickname = (getattr(b.config, "nickname", "") or "").strip() or str(b.qq)
            bots.append({
                "bot_id": b.bot_id,
                "nickname": nickname,
                "online_secs": max(0.0, time.time() - b.connected_at),
            })
        return bots


# ── 数据读取(模块级, 便于测试) ──────────────────────────────

def _read_cpu() -> dict[str, Any]:
    if not HAS_PSUTIL:
        return {}
    try:
        cores = psutil.cpu_percent(interval=0.35, percpu=True) or []
    except Exception as e:
        logger.debug(f"读取 CPU 失败: {e}")
        cores = []
    if not cores:
        try:
            overall = float(psutil.cpu_percent(interval=0.2))
        except Exception:
            overall = 0.0
        count = psutil.cpu_count() or 0
        cores = [overall] * count if count else []
    average = sum(cores) / len(cores) if cores else 0.0
    return {
        "count": psutil.cpu_count(logical=True) or len(cores),
        "average": average,
        "cores": [float(c) for c in cores],
    }


def _read_memory() -> dict[str, Any]:
    """读取内存并保证「应用 + 缓存 + 可用 == 总量」。

    psutil 7.x 的 Linux `used` 已经是 `total - available`（包含 cache），
    而 5.x 的 `used` 不含 cache；只看 `used + cache` 会得到 0 可用。
    这里按版本 + 关系判定 used 是否包含 cache，并在两种语义下都保证三分量守恒。
    `used` 字段表示「应用」占用（不含缓存），`percent` 是含缓存的整体占用率。
    """
    if not HAS_PSUTIL:
        return {}
    try:
        vm = psutil.virtual_memory()
        total = max(int(getattr(vm, "total", 0) or 0), 0)
        available = min(max(int(getattr(vm, "available", 0) or 0), 0), total)
        raw_used = min(max(int(getattr(vm, "used", 0) or 0), 0), total)
        cache = max(
            int(getattr(vm, "buffers", 0) or 0) + int(getattr(vm, "cached", 0) or 0),
            0,
        )
        cache = min(cache, total)

        version = tuple(getattr(psutil, "version_info", (0, 0, 0)) or ())
        # 7.x 统一改为 total - available；关系判定兜住未带版本信息的替身。
        used_includes_cache = version >= (7, 0, 0) or (raw_used + cache > total)
        if used_includes_cache:
            app = max(raw_used - cache, 0)
            free = available
        else:
            # 5.x: used 不含 cache；available 含可回收缓存，若同时展示 cache
            # 会重复计数，因此旧语义下「可用」用 free（总量 - 应用 - 缓存）。
            app = raw_used
            free = max(total - app - cache, 0)

        # 守恒兜底: 极端/异常数据下优先保留 available, 再压缩 cache/app。
        overflow = app + cache + free - total
        if overflow > 0:
            cache = max(cache - overflow, 0)
            overflow = app + cache + free - total
            if overflow > 0:
                app = max(app - overflow, 0)
                overflow = app + cache + free - total
                if overflow > 0:
                    free = max(free - overflow, 0)

        used_total = app + cache
        percent = (used_total / total * 100.0) if total else 0.0
        app_percent = (app / total * 100.0) if total else 0.0
        data = {
            "total": total,
            "used": app,
            "cache": cache,
            "free": free,
            "percent": percent,
            "app_percent": app_percent,
            "swap_total": 0,
            "swap_used": 0,
            "swap_percent": 0.0,
        }
        try:
            sm = psutil.swap_memory()
            data.update({
                "swap_total": int(getattr(sm, "total", 0) or 0),
                "swap_used": int(getattr(sm, "used", 0) or 0),
                "swap_percent": float(getattr(sm, "percent", 0) or 0),
            })
        except Exception:
            pass
        return data
    except Exception as e:
        logger.debug(f"读取内存失败: {e}")
        return {}


_DISK_SKIP_FS = {
    "", "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs",
    "cgroup", "cgroup2", "devpts", "mqueue", "debugfs", "tracefs",
    "securityfs", "pstore", "bpf", "autofs", "fusectl", "configfs",
    "ramfs", "hugetlbfs", "nsfs",
}


def _read_disks() -> list[dict[str, Any]]:
    """主盘固定 /(Windows 为系统盘), 其它物理挂载点按发现顺序追加。"""
    if not HAS_PSUTIL:
        return []
    main_mount = "/" if os.path.exists("/") else os.path.abspath(os.sep)
    disks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(mount: str) -> None:
        key = os.path.normcase(os.path.normpath(mount))
        if key in seen:
            return
        try:
            usage = psutil.disk_usage(mount)
        except Exception:
            return
        seen.add(key)
        disks.append({
            "mount": mount,
            "total": int(usage.total),
            "used": int(usage.used),
            "percent": float(usage.percent),
        })

    _add(main_mount)
    if not disks:
        # 容器/异常环境里 / 可能不可用, 退回系统盘
        _add(os.path.abspath(os.sep))
    try:
        for part in psutil.disk_partitions(all=False):
            mount = part.mountpoint
            if not mount or part.fstype.lower() in _DISK_SKIP_FS:
                continue
            if mount.startswith(("/snap", "/var/lib/docker", "/run")):
                continue
            _add(mount)
    except Exception as e:
        logger.debug(f"枚举磁盘挂载点失败: {e}")
    return disks


# ── 文本降级 ─────────────────────────────────────────────────

def _text_report(data: dict[str, Any]) -> str:
    cpu = data.get("cpu") or {}
    mem = data.get("memory") or {}
    disks = data.get("disks") or []
    contacts = data.get("contacts") or {}
    lines = [
        f"Mohobot 状态 — {data.get('bot_id')} · {data.get('nickname')}",
        f"框架 {_fmt_duration(data.get('runtime_secs'))} · "
        f"插件 {_fmt_duration(data.get('plugin_runtime_secs'))} · "
        f"系统 {_fmt_duration(data.get('system_uptime_secs'))} · {data.get('now')}",
    ]
    if cpu:
        cores = "/".join(f"{int(c)}%" for c in (cpu.get("cores") or []))
        lines.append(f"CPU: {int(cpu.get('average') or 0)}% ({cpu.get('count')} 核: {cores})")
    if mem:
        lines.append(
            f"内存: 应用 {_fmt_gb(mem.get('used'))} / 总量 {_fmt_gb(mem.get('total'))} "
            f"({int(mem.get('percent') or 0)}%) · 缓存 {_fmt_gb(mem.get('cache'))} "
            f"· 可用 {_fmt_gb(mem.get('free'))}"
            + (
                f" · Swap {_fmt_gb(mem.get('swap_used'))}/{_fmt_gb(mem.get('swap_total'))}"
                if mem.get("swap_total") else " · Swap 未启用"
            )
        )
    if disks:
        main = disks[0]
        lines.append(
            f"磁盘 {main.get('mount')}: {_fmt_gb(main.get('used'))}/"
            f"{_fmt_gb(main.get('total'))} ({int(main.get('percent') or 0)}%)"
        )
        if len(disks) > 1:
            others = " · ".join(
                f"{d.get('mount')} {int(d.get('percent') or 0)}%" for d in disks[1:4]
            )
            lines.append(f"其他盘: {others}")
    bots = data.get("bots") or []
    lines.append(f"Bot 连接: {len(bots)}")
    for bot in bots:
        lines.append(
            f"  · {bot.get('nickname')}（{bot.get('bot_id')}） "
            f"在线 {_fmt_duration(bot.get('online_secs'))}"
        )
    admins = ", ".join(contacts.get("admins") or []) or "未配置"
    lines.append(f"管理员: {admins}")
    lines.append(f"交流群: {contacts.get('group')} · 使用须知: {contacts.get('url')}")
    return "\n".join(lines)


def _fmt_duration(seconds: float | int | None) -> str:
    from mohobot.utils.status_card import format_duration
    return format_duration(seconds)


def _fmt_gb(value: float | int | None) -> str:
    try:
        gb = float(value or 0) / (1024 ** 3)
    except (TypeError, ValueError):
        gb = 0.0
    return f"{gb:.1f}GB" if gb >= 1 else f"{float(value or 0) / (1024 ** 2):.0f}MB"


def _now_text() -> str:
    try:
        from mohobot.utils.time_utils import format_utc8
        return format_utc8("%Y-%m-%d %H:%M")
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M")


def _extract_text(event: Any) -> str:
    message = getattr(event, "message", "")
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        return "".join(
            seg.get("data", {}).get("text", "")
            for seg in message
            if isinstance(seg, dict) and seg.get("type") == "text"
        ).strip()
    return ""


def _event_chat(event: Any) -> tuple[str, str]:
    from mohobot.models.onebot import GroupMessageEvent
    if isinstance(event, GroupMessageEvent):
        return ("group", str(event.group_id))
    return ("private", str(event.user_id))
