"""Mohobot 环境感知插件 — 迁移自 AstrBot LLMPerception (add_time) 插件。

在生成回复前, 为 LLM 提供当前环境感知信息(只进 LLM 请求, 不写入 context):
- 发送时间(UTC+8)与时间段(上午/中午/下午/晚上/深夜)
- 节假日/工作日感知(含调休, 依赖 chinese-calendar; 缺库降级为周末判断)
- 农历日期(天干地支年/生肖/月日/闰月, 依赖 lunarcalendar; 缺库自动跳过)
- 二十四节气(简化日期表, 前后 3 天临近判断)
- 群聊环境(群聊/私聊 + 群名 + 消息是否含图片/语音/视频)

框架侧: 插件实现 on_perception(bot_id, event, raw) -> str 钩子,
由 message_handler 在回复生成时(agent main_chat / legacy 路径)注入。
"""

from __future__ import annotations

import importlib
import time
from typing import Any

from loguru import logger

# ── 可选依赖(缺库自动降级) ─────────────────────────────────
_calendar_cn = None
_lunar_Converter = None
_lunar_Solar = None


def _load_calendar_deps() -> None:
    global _calendar_cn, _lunar_Converter, _lunar_Solar
    if _calendar_cn is None:
        try:
            _calendar_cn = importlib.import_module("chinese_calendar")
        except ImportError:
            _calendar_cn = False
    if _lunar_Converter is None:
        try:
            lunar = importlib.import_module("lunarcalendar")
            _lunar_Converter = lunar.Converter
            _lunar_Solar = lunar.Solar
        except ImportError:
            _lunar_Converter = False
            _lunar_Solar = False


WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

LUNAR_MONTHS = ["正月", "二月", "三月", "四月", "五月", "六月", "七月",
                "八月", "九月", "十月", "冬月", "腊月"]
LUNAR_DAYS = ["初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八",
              "初九", "初十", "十一", "十二", "十三", "十四", "十五", "十六",
              "十七", "十八", "十九", "二十", "廿一", "廿二", "廿三", "廿四",
              "廿五", "廿六", "廿七", "廿八", "廿九", "三十"]
SOLAR_TERMS = ["小寒", "大寒", "立春", "雨水", "惊蛰", "春分", "清明", "谷雨",
               "立夏", "小满", "芒种", "夏至", "小暑", "大暑", "立秋", "处暑",
               "白露", "秋分", "寒露", "霜降", "立冬", "小雪", "大雪", "冬至"]
# (月, 日) 近似节气日期
SOLAR_TERM_DATES = [
    (1, 6), (1, 20), (2, 4), (2, 19), (3, 6), (3, 21),
    (4, 5), (4, 20), (5, 6), (5, 21), (6, 6), (6, 21),
    (7, 7), (7, 23), (8, 7), (8, 23), (9, 8), (9, 23),
    (10, 8), (10, 23), (11, 7), (11, 22), (12, 7), (12, 22),
]
TIAN_GAN = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
DI_ZHI = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]
SHENG_XIAO = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]

# 群名缓存: {group_id: (name, fetch_ts)} — 10 分钟 TTL
_GROUP_NAME_CACHE: dict[str, tuple[str, float]] = {}
_GROUP_NAME_TTL = 600


class Plugin:
    """环境感知: 时间/节假日/农历/节气/群聊环境(供 LLM 回复注入)。"""

    info = {
        "commands": [],
        "description": "环境感知: 为 LLM 回复注入当前时间/节假日/农历/节气/群聊环境信息(不写入对话上下文)",
    }

    _ws_server = None

    _DEFAULTS = {
        "enable_holiday": True,
        "enable_lunar": True,
        "enable_solar_term": True,
        "enable_platform": True,
    }

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)
        _load_calendar_deps()

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    def _cfg(self, key: str, default):
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None else default

    # ── 感知钩子(框架在 LLM 回复生成时调用) ─────────────────

    async def on_perception(
        self, bot_id: str, event: Any, raw_event: dict[str, Any],
    ) -> str:
        """收集环境感知文本, 返回空串表示不注入。"""
        from mohobot.utils.time_utils import TZ_UTC8
        from datetime import datetime

        now = datetime.now(TZ_UTC8)
        parts: list[str] = [f"发送时间: {now.strftime('%Y-%m-%d %H:%M:%S')}"]

        holiday = self._holiday_info(now)
        if holiday:
            parts.append(holiday)
        lunar = self._lunar_info(now)
        if lunar:
            parts.append(lunar)
        solar_term = self._solar_term_info(now)
        if solar_term:
            parts.append(solar_term)
        platform = await self._platform_info(bot_id, event)
        if platform:
            parts.append(platform)
        return " | ".join(parts)

    # ── 节假日/工作日 ────────────────────────────────────────

    def _holiday_info(self, current_time) -> str:
        if not self._cfg("enable_holiday", True):
            return ""
        parts = [WEEKDAY_NAMES[current_time.weekday()]]

        if _calendar_cn is not None and _calendar_cn is not False:
            try:
                d = current_time.date()
                is_holiday = _calendar_cn.is_holiday(d)
                is_workday = _calendar_cn.is_workday(d)
                if is_holiday:
                    detail = _calendar_cn.get_holiday_detail(d)
                    name = detail[1] if detail and detail[1] else "法定节假日"
                    parts.append(f"周末({name})" if current_time.weekday() >= 5
                                 else f"法定节假日({name})")
                elif is_workday:
                    parts.append("调休工作日" if current_time.weekday() >= 5 else "工作日")
                else:
                    parts.append("周末")
            except Exception as e:
                logger.debug(f"节假日判断失败: {e}")
                parts.append("周末" if current_time.weekday() >= 5 else "工作日")
        else:
            parts.append("周末" if current_time.weekday() >= 5 else "工作日")

        hour = current_time.hour
        if 5 <= hour < 12:
            parts.append("上午")
        elif 12 <= hour < 14:
            parts.append("中午")
        elif 14 <= hour < 18:
            parts.append("下午")
        elif 18 <= hour < 22:
            parts.append("晚上")
        else:
            parts.append("深夜")
        return ", ".join(parts)

    # ── 农历 ─────────────────────────────────────────────────

    def _lunar_info(self, current_time) -> str:
        if (not self._cfg("enable_lunar", True)
                or not _lunar_Converter or not _lunar_Solar):
            return ""
        try:
            solar = _lunar_Solar(current_time.year, current_time.month, current_time.day)
            lunar = _lunar_Converter.Solar2Lunar(solar)
            month_str = LUNAR_MONTHS[lunar.month - 1]
            day_str = LUNAR_DAYS[lunar.day - 1]
            if lunar.isleap:
                month_str = "闰" + month_str
            year_gan = TIAN_GAN[(lunar.year - 4) % 10]
            year_zhi = DI_ZHI[(lunar.year - 4) % 12]
            sheng_xiao = SHENG_XIAO[(lunar.year - 4) % 12]
            return f"农历{year_gan}{year_zhi}年({sheng_xiao}年){month_str}{day_str}"
        except Exception as e:
            logger.debug(f"农历信息失败: {e}")
            return ""

    # ── 二十四节气(近似表) ──────────────────────────────────

    def _solar_term_info(self, current_time) -> str:
        if not self._cfg("enable_solar_term", True):
            return ""
        try:
            m, d = current_time.month, current_time.day
            # 前后 3 天临近判断
            for i, (month, day) in enumerate(SOLAR_TERM_DATES):
                if month == m and abs(d - day) <= 2:
                    if d == day:
                        return f"今日{SOLAR_TERMS[i]}"
                    return f"临近{SOLAR_TERMS[i]}" if d < day else f"{SOLAR_TERMS[i]}已过"
            # 处于哪两个节气之间
            cur = m * 100 + d
            for i, (month, day) in enumerate(SOLAR_TERM_DATES):
                nxt = SOLAR_TERM_DATES[(i + 1) % 24]
                this_ord, next_ord = month * 100 + day, nxt[0] * 100 + nxt[1]
                if next_ord < this_ord:  # 跨年
                    if cur >= this_ord or cur < next_ord:
                        return f"当前节气: {SOLAR_TERMS[i]}"
                elif this_ord <= cur < next_ord:
                    return f"当前节气: {SOLAR_TERMS[i]}"
            return ""
        except Exception as e:
            logger.debug(f"节气信息失败: {e}")
            return ""

    # ── 群聊环境 ─────────────────────────────────────────────

    async def _platform_info(self, bot_id: str, event: Any) -> str:
        if not self._cfg("enable_platform", True):
            return ""
        from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent

        parts = ["平台: QQ"]
        if isinstance(event, GroupMessageEvent):
            parts.append("群聊")
            group_name = await self._group_name(bot_id, event.group_id)
            if group_name:
                parts.append(f"群名: {group_name}")
        elif isinstance(event, PrivateMessageEvent):
            parts.append("私聊")

        # 消息类型感知
        if isinstance(event.message, list):
            types = {seg.get("type") for seg in event.message}
            if "image" in types:
                parts.append("含图片")
            if "record" in types or "voice" in types:
                parts.append("含语音")
            if "video" in types:
                parts.append("含视频")
        return ", ".join(parts)

    async def _group_name(self, bot_id: str, group_id) -> str | None:
        """获取群名(带 10 分钟缓存); 获取失败返回 None。"""
        gid = str(group_id)
        cached = _GROUP_NAME_CACHE.get(gid)
        now = time.time()
        if cached and now - cached[1] < _GROUP_NAME_TTL:
            return cached[0]
        ws = self._ws_server
        if ws is None:
            return None
        try:
            resp = await ws.send_to_bot(
                bot_id, "get_group_info", {"group_id": gid},
                wait_response=True, timeout=5.0,
            )
            name = ""
            if resp and resp.get("status") == "ok" and resp.get("retcode") == 0:
                info = resp.get("data") or {}
                name = str(info.get("group_name") or "").strip()
            # 失败也缓存空值 60s, 避免每次消息都打 API
            ttl = _GROUP_NAME_TTL if name else 60
            _GROUP_NAME_CACHE[gid] = (name, now)
            return name or None
        except Exception as e:
            logger.debug(f"获取群名失败: {e}")
            _GROUP_NAME_CACHE[gid] = ("", now)
            return None
