"""歌曲知识库同步插件 — 多来源定时同步 + 手动触发。

命令(仅全局管理员):
    /sync-songs                 — 按配置同步全部来源(默认 fill_missing)
    /sync-songs new             — 只抓库里没有的新歌
    /sync-songs fill            — 补全已有歌曲的残缺字段(默认)
    /sync-songs refresh         — 全部重新抓取并合并
    /sync-songs <来源> [模式]   — 只同步指定来源(vcpedia/vocawiki/moegirl)
    /sync-songs status          — 查看各来源上次同步时间与结果

定时: 插件声明 interval_sec + on_tick, 框架每轮重读该属性(配置热生效);
到达 `sync.interval_hours` 且不在静默时段才真正执行。同步任务注册进
TaskSupervisor(owner="plugins"), 插件重载/关闭时会被取消。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from loguru import logger

TRIGGERS = {"/sync-songs", "/同步歌曲"}
_SOURCE_NAMES = ("vcpedia", "vocawiki", "moegirl")
_MODE_ALIASES = {
    "new": "new_only", "new_only": "new_only", "新歌": "new_only",
    "fill": "fill_missing", "fill_missing": "fill_missing", "补全": "fill_missing",
    "refresh": "full_refresh", "full_refresh": "full_refresh", "全量": "full_refresh",
}

# 同步任务串行化: 并发触发 /sync-songs 时避免两个线程同时写知识库 SQLite
_sync_lock = asyncio.Lock()


class Plugin:
    """多来源歌曲库同步(定时 + 手动)。"""

    info = {
        "commands": [
            {"name": "sync-songs", "desc": "同步歌曲知识库(管理员): new/fill/refresh/status/来源名"},
        ],
    }

    _data_dir = "./data"
    _ws_server = None
    _matcher = None
    _task_supervisor = None
    _config = None

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_song_matcher(cls, matcher) -> None:
        cls._matcher = matcher

    @classmethod
    def inject_task_supervisor(cls, supervisor) -> None:
        cls._task_supervisor = supervisor

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    def __init__(self) -> None:
        self.plugin_config: dict = {}

    # ── 配置 ────────────────────────────────────────────────

    def _sync_cfg(self) -> dict[str, Any]:
        """读取同步配置: 插件配置优先, 否则用全局 music_knowledge.sync。

        单文件插件拿不到自己的 _conf_schema.json(见开发指南), 因此定时同步
        的开关主要来自 config/global.yaml 的 music_knowledge.sync。
        """
        cfg = self.plugin_config or {}
        sync = cfg.get("sync")
        if isinstance(sync, dict) and sync:
            return sync
        global_cfg = self._global_config()
        music = (getattr(global_cfg, "music_knowledge", None) or {}) if global_cfg else {}
        value = music.get("sync") if isinstance(music, dict) else {}
        return value if isinstance(value, dict) else {}

    def _global_config(self):
        """读取 GlobalConfig(与权限检查共用); 失败返回 None。"""
        if self._config is not None:
            return self._config
        try:
            import os

            from mohobot.models.config import GlobalConfig
            self._config = GlobalConfig.load(
                os.environ.get("MOHOBOT_CONFIG", "./config/global.yaml")
            )
        except Exception as e:
            logger.warning(f"歌曲同步读取全局配置失败: {e}")
            self._config = None
        return self._config

    def on_config_update(self, config: dict) -> None:
        """同步更新插件配置(必须同步函数, 框架不会 await)。"""
        self.plugin_config = config or {}
        self._config = None  # 下次重新加载全局配置

    # ── 定时任务 ─────────────────────────────────────────────

    @property
    def interval_sec(self) -> int:
        """框架轮询间隔(实际是否执行由 sync.interval_hours 决定)。"""
        return max(60, int(self._sync_cfg().get("check_interval_sec", 600) or 600))

    async def on_tick(self) -> None:
        """周期检查是否到达同步时间。"""
        sync = self._sync_cfg()
        if not sync.get("enabled", False):
            return
        hours = float(sync.get("interval_hours", 24) or 24)
        if not self._due(hours):
            return
        if not self._within_window(sync):
            logger.debug("歌曲定时同步: 当前不在允许时段, 跳过")
            return
        if _sync_lock.locked():
            logger.debug("歌曲定时同步: 已有任务运行, 跳过")
            return
        self._spawn_sync(mode=sync.get("mode") or "fill_missing", limit=None)
        logger.info("歌曲定时同步已启动")

    def _due(self, hours: float) -> bool:
        """距上次成功同步是否已超过 interval_hours。"""
        from mohobot.music_knowledge.pool import ensure_init, get_session
        from mohobot.music_knowledge.song_database import get_sync_state

        cfg = self._song_db_cfg()
        ensure_init(cfg["db_folder"], cfg["db_file"])
        db = get_session()
        try:
            state = get_sync_state(db, "song_sync")
        finally:
            db.close()
        sources = state.get("sources") or {}
        stamps = [item.get("last_run") for item in sources.values() if item.get("last_run")]
        if not stamps:
            return True
        try:
            latest = max(datetime.fromisoformat(str(s)) for s in stamps)
        except (TypeError, ValueError):
            return True
        return (datetime.now() - latest).total_seconds() >= hours * 3600

    @staticmethod
    def _within_window(sync: dict[str, Any]) -> bool:
        """quiet_hours_start/end 之外才执行(避免白天抢占带宽)。"""
        start = sync.get("quiet_hours_start")
        end = sync.get("quiet_hours_end")
        if start is None or end is None:
            return True
        try:
            start_h, end_h = int(start), int(end)
        except (TypeError, ValueError):
            return True
        hour = datetime.now().hour
        if start_h <= end_h:
            return not (start_h <= hour < end_h)
        return not (hour >= start_h or hour < end_h)

    # ── 命令 ────────────────────────────────────────────────

    async def on_message(self, bot_id: str, event: Any, raw_event: dict[str, Any]):
        text = self._text(event)
        first_word = text.split()[0] if text else ""
        if first_word not in TRIGGERS:
            return (False, None)

        if not self._is_admin(event):
            return (True, "❌ 你没有权限执行此操作。")

        args = text.split()[1:]
        if args and args[0].lower() in ("status", "状态"):
            return (True, self._status_text())

        mode = "fill_missing"
        only_source = ""
        for arg in args:
            low = arg.lower()
            if low in _MODE_ALIASES:
                mode = _MODE_ALIASES[low]
            elif low in _SOURCE_NAMES:
                only_source = low
            else:
                return (
                    True,
                    f"❌ 未知参数: {arg}\n用法: /sync-songs "
                    f"[new|fill|refresh|status|{'|'.join(_SOURCE_NAMES)}]",
                )

        if _sync_lock.locked():
            return (True, "⏳ 已有同步任务在运行, 请稍后再试")

        self._spawn_sync(
            mode=mode, limit=None, only_source=only_source, notify=(bot_id, event),
        )
        scope = only_source or "全部来源"
        return (True, f"🔄 已开始同步 {scope}({mode}), 完成后会通知你~")

    def _spawn_sync(self, *, mode: str, limit: int | None, only_source: str = "",
                    notify: tuple[str, Any] | None = None) -> None:
        coro = self._run_sync(mode, limit, only_source, notify)
        if self._task_supervisor is not None:
            self._task_supervisor.create_task(coro, name="song-sync", owner="plugins")
        else:
            asyncio.create_task(coro)

    async def _run_sync(self, mode: str, limit: int | None, only_source: str,
                        notify: tuple[str, Any] | None) -> None:
        """后台同步(阻塞网络操作放线程池), 完成后可选通知。"""
        reply = ""
        try:
            async with _sync_lock:
                from mohobot.music_knowledge.sync import (
                    build_adapters, set_db_location, sync_all,
                )

                cfg = self._global_config()
                music_cfg = (cfg.music_knowledge if cfg else None) or {}
                db_cfg = music_cfg.get("song_database") or {}
                if db_cfg.get("db_folder") and db_cfg.get("db_file"):
                    # 同步与匹配器必须指向同一个库文件
                    set_db_location(str(db_cfg["db_folder"]), str(db_cfg["db_file"]))
                adapters = build_adapters(music_cfg)
                if only_source:
                    adapters = [a for a in adapters if a.name == only_source]
                if not adapters:
                    suffix = f"({only_source})" if only_source else ""
                    reply = f"❌ 没有可用的歌曲来源{suffix}"
                else:
                    report = await asyncio.to_thread(
                        sync_all, adapters, mode=mode, limit=limit,
                    )
                    reply = report.summary_text()
                    self._reload_matcher()
        except Exception as e:
            logger.exception(f"song-sync failed: {e}")
            reply = f"❌ 同步失败: {e}"

        if notify is not None and reply:
            bot_id, event = notify
            await self._send_event(bot_id, event, reply)

    def _reload_matcher(self) -> None:
        matcher = self._matcher
        if matcher is None:
            return
        try:
            matcher.reload_index()
        except Exception as e:
            logger.warning(f"歌曲索引重载失败: {e}")

    def _status_text(self) -> str:
        from sqlalchemy import func

        from mohobot.music_knowledge.pool import ensure_init, get_session
        from mohobot.music_knowledge.song_database import (
            Song, SongSource, get_sync_state,
        )

        cfg = self._song_db_cfg()
        ensure_init(cfg["db_folder"], cfg["db_file"])
        db = get_session()
        try:
            total = db.query(Song).count()
            state = get_sync_state(db, "song_sync")
            sources = state.get("sources") or {}
            rows = (
                db.query(SongSource.source, func.count(SongSource.id))
                .group_by(SongSource.source)
                .all()
            )
            counts = {src: n for src, n in rows}
        finally:
            db.close()

        lines = [f"歌曲库共 {total} 首"]
        for name in _SOURCE_NAMES:
            item = sources.get(name) or {}
            line = f"- {name}: 记录 {counts.get(name, 0)}, 上次 {item.get('last_run') or '从未'}"
            if item.get("last_mode"):
                line += f", 模式 {item['last_mode']}"
            lines.append(line)
        sync = self._sync_cfg()
        state_text = "开启" if sync.get("enabled") else "关闭"
        lines.append(
            f"定时同步: {state_text} (每 {sync.get('interval_hours', 24)} 小时, "
            f"模式 {sync.get('mode', 'fill_missing')})"
        )
        return "\n".join(lines)

    # ── 辅助 ────────────────────────────────────────────────

    def _song_db_cfg(self) -> dict[str, str]:
        cfg = self._global_config()
        music = (cfg.music_knowledge if cfg else None) or {}
        db_cfg = music.get("song_database") or {}
        return {
            "db_folder": db_cfg.get("db_folder", f"{self._data_dir}/song_knowledge"),
            "db_file": db_cfg.get("db_file", "knowledge_db.db"),
        }

    def _is_admin(self, event: Any) -> bool:
        cfg = self._global_config()
        admins = {str(a) for a in (getattr(cfg, "admins", None) or [])}
        return str(getattr(event, "user_id", "")) in admins

    @staticmethod
    def _text(event: Any) -> str:
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

    async def _send_event(self, bot_id: str, event: Any, text: str) -> None:
        from mohobot.models.onebot import GroupMessageEvent

        if isinstance(event, GroupMessageEvent):
            await self._send(bot_id, "group", event.group_id, text)
        else:
            await self._send(bot_id, "private", getattr(event, "user_id", 0), text)

    async def _send(self, bot_id: str, chat_type: str, chat_id: Any, text: str) -> None:
        ws = getattr(self.__class__, "_ws_server", None)
        if ws is None:
            logger.warning("sync-songs: ws_server 未注入, 无法发送结果")
            return
        # 长结果分段, 避免超出单条消息限制
        chunks = [text[i:i + 900] for i in range(0, len(text), 900)] or [text]
        for chunk in chunks:
            if chat_type == "group":
                await ws.send_group_msg(bot_id, chat_id, chunk)
            else:
                await ws.send_private_msg(bot_id, chat_id, chunk)
