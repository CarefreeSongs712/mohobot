"""Bot 离线监控告警。

已配置且启用的 bot 连续离线超过阈值后，用任意一只**在线** bot 往管理群
（复用全局 ``snitch_admin_group``，默认 1070473353）发一次聚合告警。
告警按「离线 episode」去重：恢复在线后重置，下次再离线会重新告警；
若当前没有在线 bot 可发，则保留待下一轮检查重试。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from loguru import logger


class BotOfflineWatcher:
    """周期性检查已配置 bot 的在线状态并发送离线告警。"""

    def __init__(
        self,
        bot_manager: Any,
        ws_server: Any,
        *,
        group_id: int | str,
        threshold_sec: float = 300.0,
        interval_sec: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot_manager = bot_manager
        self._ws_server = ws_server
        self._group_id = int(group_id)
        self._threshold = max(1.0, float(threshold_sec))
        self._interval = max(5.0, float(interval_sec))
        self._clock = clock
        # bot_id -> 本轮连续离线的起点(monotonic)
        self._offline_since: dict[str, float] = {}
        # 已就本轮离线发过告警的 bot_id；恢复在线时清除
        self._alerted: set[str] = set()

    # ── 在线判定 ─────────────────────────────────────────

    def _is_online(self, bot_id: str) -> bool:
        """BotManager 只保留已连接实例；再兼容 ws 已关闭但尚未 unregister 的短暂窗口。"""
        inst = self._bot_manager.get(bot_id)
        if inst is None:
            return False
        ws = getattr(inst, "ws", None)
        return getattr(ws, "close_code", None) is None

    def _pick_sender(self, exclude: set[str]) -> str | None:
        """从在线 bot 里挑一个非离线 bot 作为发送方。"""
        for inst in self._bot_manager.all_bots:
            bot_id = str(getattr(inst, "bot_id", "") or "")
            if not bot_id or bot_id in exclude:
                continue
            if self._is_online(bot_id):
                return bot_id
        return None

    # ── 检查与告警 ───────────────────────────────────────

    async def check_once(self, now: float | None = None) -> list[str]:
        """执行一轮检查，返回本轮成功发出告警的 bot_id 列表。"""
        now = self._clock() if now is None else float(now)
        try:
            configured = [
                cfg
                for cfg in self._bot_manager.list_bot_configs()
                if getattr(cfg, "enabled", False)
                and str(getattr(cfg, "bot_id", "") or "")
                and int(getattr(cfg, "qq", 0) or 0) != 0
            ]
        except Exception as exc:  # 配置读取异常不能拖垮后台任务
            logger.warning(f"离线监控读取 bot 配置失败: {exc}")
            return []

        offline: list[tuple[Any, float]] = []
        for cfg in configured:
            bot_id = str(cfg.bot_id)
            if self._is_online(bot_id):
                if bot_id in self._offline_since:
                    logger.info(f"Bot {bot_id} 已恢复在线")
                self._offline_since.pop(bot_id, None)
                self._alerted.discard(bot_id)
                continue
            since = self._offline_since.setdefault(bot_id, now)
            duration = now - since
            if duration >= self._threshold and bot_id not in self._alerted:
                offline.append((cfg, duration))

        if not offline:
            return []

        offline_ids = {str(cfg.bot_id) for cfg, _ in offline}
        sender = self._pick_sender(offline_ids)
        if sender is None:
            logger.warning(
                f"检测到离线 bot {sorted(offline_ids)}，但没有在线 bot 可发送告警"
            )
            return []

        lines = ["【离线告警】以下 bot 未连接:"]
        for cfg, duration in offline:
            nickname = str(getattr(cfg, "nickname", "") or "")
            label = str(cfg.bot_id)
            if nickname:
                label += f"（{nickname} / {cfg.qq}）"
            else:
                label += f"（QQ {cfg.qq}）"
            minutes = max(1, int(duration // 60))
            lines.append(f"- {label} 已离线 {minutes} 分钟")
        message = "\n".join(lines)

        try:
            await self._ws_server.send_group_msg(sender, str(self._group_id), message)
        except Exception as exc:
            logger.warning(f"发送离线告警失败（sender={sender}）: {exc}")
            return []

        self._alerted.update(offline_ids)
        logger.warning(
            f"已通过 {sender} 向群 {self._group_id} 发送离线告警: {sorted(offline_ids)}"
        )
        return sorted(offline_ids)

    async def run(self) -> None:
        """后台循环：每 interval_sec 检查一次，直到被 TaskSupervisor 取消。"""
        logger.info(
            f"Bot 离线监控已启动（阈值 {int(self._threshold)}s，"
            f"检查间隔 {int(self._interval)}s，告警群 {self._group_id}）"
        )
        while True:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            try:
                await self.check_once()
            except Exception as exc:
                logger.warning(f"离线监控检查异常: {exc}")
