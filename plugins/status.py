"""Mohobot /status plugin — reports framework and system status as an image.

Usage: /status  or  /状态
文本渲染成深色卡片图片发送(PIL); 图片生成或发送失败时降级为文本。
"""

from __future__ import annotations

import datetime
import os
import platform
import tempfile
import time
from typing import Any

# Optional psutil for detailed system stats
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from loguru import logger


class Plugin:
    """Plugin that responds to /status with system and framework status."""

    info = {
        "commands": [
            {"name": "status", "desc": "显示框架与系统状态(图片)"},
        ],
    }

    def __init__(self):
        self._start_time = time.time()

    @classmethod
    def inject_bot_manager(cls, bot_manager) -> None:
        """Set the bot manager reference for status reporting (called from main.py)."""
        cls._bot_manager = bot_manager

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        """Set the WS server reference for sending status image (called from main.py)."""
        cls._ws_server = ws_server

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Check for /status and return system status if found."""
        # Extract plain text
        text = ""
        if isinstance(event.message, str):
            text = event.message.strip()
        elif isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
            text = text.strip()

        # Check for /status or /状态
        if not (text.startswith("/status") or text.startswith("/状态")):
            return (False, None)

        # Build status report
        status = await self._build_status(bot_id)

        # 渲染深色卡片图片发送; 失败降级为文本
        img_path = _render_status_image(status)
        if img_path is not None and getattr(self, "_ws_server", None) is not None:
            try:
                chat_type, chat_id = _event_chat(event)
                await self._ws_server.send_image(bot_id, chat_type, chat_id, img_path)
                os.remove(img_path)
                return (True, None)  # 已发送图片, 无需文本
            except Exception as e:
                logger.warning(f"发送状态图片失败, 降级为文本: {e}")
                try:
                    os.remove(img_path)
                except OSError:
                    pass
        return (True, status)

    async def on_notice(self, bot_id: str, event: Any, raw: dict) -> None:
        pass

    async def on_meta(self, bot_id: str, event: Any, raw: dict) -> None:
        pass

    async def _build_status(self, bot_id: str) -> str:
        """Build a formatted status string."""
        lines = [
            "╔══════════════════════════════╗",
            "║      Mohobot 系统状态        ║",
            "╚══════════════════════════════╝",
            "",
        ]

        # ── Framework Status ──
        lines.append("📦 框架状态:")
        lines.append(f"  Bot ID: {bot_id}")

        # Bot manager injected via inject_bot_manager() classmethod (main.py)
        bm = self._bot_manager
        if bm:
            lines.append(f"  已连接 Bot 数: {bm.bot_count}")
            for b in bm.all_bots:
                uptime_secs = time.time() - b.connected_at
                uptime_str = str(datetime.timedelta(seconds=int(uptime_secs)))
                lines.append(f"    └ {b.bot_id} (QQ:{b.qq}) — 在线 {uptime_str}")
        else:
            lines.append(f"  已连接 Bot 数: 未知 (管理器未注入)")

        uptime_secs = time.time() - self._start_time
        uptime_str = str(datetime.timedelta(seconds=int(uptime_secs)))
        lines.append(f"  插件运行时间: {uptime_str}")

        # ── System Status ──
        lines.append("")
        lines.append("💻 系统状态:")
        lines.append(f"  操作系统: {platform.system()} {platform.release()}")
        lines.append(f"  平台: {platform.platform()}")
        lines.append(f"  Python: {platform.python_version()}")
        lines.append(f"  主机名: {platform.node()}")

        if HAS_PSUTIL:
            # CPU
            cpu_percent = psutil.cpu_percent(interval=0.5)
            cpu_count = psutil.cpu_count()
            lines.append(f"  CPU: {cpu_percent}% 使用率 ({cpu_count} 核)")

            # Memory
            mem = psutil.virtual_memory()
            mem_total_gb = mem.total / (1024**3)
            mem_used_gb = mem.used / (1024**3)
            mem_percent = mem.percent
            lines.append(f"  内存: {mem_used_gb:.1f}GB / {mem_total_gb:.1f}GB ({mem_percent}%)")

            # Disk
            disk = psutil.disk_usage(os.path.abspath(os.sep))
            disk_total_gb = disk.total / (1024**3)
            disk_used_gb = disk.used / (1024**3)
            disk_percent = disk.percent
            lines.append(f"  磁盘: {disk_used_gb:.1f}GB / {disk_total_gb:.1f}GB ({disk_percent}%)")

            # Boot time
            boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
            now = datetime.datetime.now()
            boot_duration = now - boot_time
            lines.append(f"  系统已运行: {str(boot_duration).split('.')[0]}")
        else:
            lines.append("  CPU: psutil 未安装，无法获取详细系统信息")
            lines.append("  提示: pip install psutil 获取详细系统状态")

            # Fallback basic info
            try:
                if platform.system() == "Windows":
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    memory_status = ctypes.c_int()
                    kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status))
                    lines.append("  (Windows 内存信息获取受限，建议安装 psutil)")
                else:
                    # Linux: try reading /proc/meminfo
                    if os.path.exists("/proc/meminfo"):
                        with open("/proc/meminfo") as f:
                            for i, line in enumerate(f):
                                if i >= 3:
                                    break
                                lines.append(f"  {line.strip()}")
            except Exception:
                pass

        # ── Bot-specific info ──
        lines.append("")
        lines.append("⚙️ 配置信息:")
        try:
            from mohobot.models.config import GlobalConfig
            cfg = GlobalConfig.load(os.environ.get("MOHOBOT_CONFIG", "./config/global.yaml"))
            lines.append(f"  WS 服务: ws://{cfg.server.host}:{cfg.server.port}")
            lines.append(f"  LLM 模型: {cfg.llm.chat_model}")
            lines.append(f"  Vision 模型: {cfg.llm.vision_model}")
            lines.append(f"  最大上下文轮数: {cfg.context_max_rounds}")
        except Exception:
            lines.append("  配置读取失败")

        lines.append("")
        lines.append("═══════════════════════════════")

        return "\n".join(lines)

# ── 图片渲染(模块级工具) ─────────────────────────────────────

_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",        # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",      # 黑体
    r"C:\Windows\Fonts\simsun.ttc",      # 宋体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_EMOJI_CHARS = set("📦💻⚙️🔒⚠️✅❌🔁🗄️🤖💬🧠🖥️📊📋🛠️✨")


def _find_cjk_font() -> str | None:
    for candidate in _CJK_FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _clean_status_text(text: str) -> list[str]:
    """去掉 emoji 与装饰字符, 返回渲染行。"""
    lines = []
    for line in text.splitlines():
        line = "".join(ch for ch in line if ch not in _EMOJI_CHARS)
        line = line.replace("╔", "").replace("║", "").replace("╚", "")
        line = line.replace("╗", "").replace("╝", "")
        line = line.replace("═", "")
        if line.strip(" -—"):
            lines.append(line.rstrip())
    return lines


def _event_chat(event: Any) -> tuple[str, str]:
    """从消息事件取 (chat_type, chat_id)。"""
    from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent
    if isinstance(event, GroupMessageEvent):
        return ("group", str(event.group_id))
    return ("private", str(event.user_id))


def _render_status_image(text: str) -> str | None:
    """把状态文本渲染成深色卡片 PNG, 返回文件路径; 失败返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font_path = _find_cjk_font()
    if font_path is None:
        return None

    lines = _clean_status_text(text)
    if not lines:
        return None

    title = "Mohobot 系统状态"
    pad_x, pad_y = 28, 24
    title_h = 42
    line_h = 27
    width = 600
    height = pad_y * 2 + title_h + line_h * len(lines) + 12
    accent = (102, 204, 255)      # #66CCFF
    bg = (30, 34, 43)             # #1e222b
    fg = (214, 219, 228)
    dim = (148, 155, 168)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    try:
        title_font = ImageFont.truetype(font_path, 22)
        body_font = ImageFont.truetype(font_path, 16)
    except Exception:
        return None

    # 标题 + 强调线
    draw.text((pad_x, pad_y), title, font=title_font, fill=fg)
    draw.rectangle(
        [pad_x, pad_y + title_h - 8, pad_x + 170, pad_y + title_h - 4],
        fill=accent,
    )

    y = pad_y + title_h + 10
    for line in lines:
        # 分区标题(以 [ 结尾的"xxx状态:"行)用强调色
        is_section = line.endswith(":") and not line.startswith("  ")
        color = accent if is_section else fg
        draw.text((pad_x, y), line, font=body_font, fill=color)
        y += line_h

    # 保存到临时文件
    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="mohobot_status_")
    os.close(fd)
    img.save(tmp_path, "PNG")
    img.close()
    return tmp_path
