"""歌曲知识库同步插件 — /sync-songs 手动触发 VCPedia 新歌同步。

用法: /sync-songs          — 同步当年洛天依模板页新歌(耗时较长, 完成后回复)
仅全局配置 ban.admins 中的管理员可执行。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

TRIGGERS = {"/sync-songs", "/同步歌曲"}


class Plugin:
    """Responds to /sync-songs with a manual VCPedia sync."""

    info = {
        "commands": [
            {"name": "sync-songs", "desc": "手动同步 VCPedia 新歌到歌曲知识库(管理员)"},
        ],
    }

    _data_dir = "./data"

    # WS server injected by main.py via inject_ws_server() classmethod
    _ws_server = None

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        """Set the WS server reference for sending results (called from main.py)."""
        cls._ws_server = ws_server

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        """Set the data dir (called from PluginSystem.apply_injections)."""
        cls._data_dir = data_dir

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        # Extract plain text
        text = ""
        if isinstance(event.message, str):
            text = event.message.strip()
        elif isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
            text = text.strip()

        first_word = text.split()[0] if text else ""
        if first_word not in TRIGGERS:
            return (False, None)

        # 管理员检查(与封禁系统共用全局 admins 配置)
        import os
        from mohobot.models.config import GlobalConfig

        cfg = GlobalConfig.load(os.environ.get("MOHOBOT_CONFIG", "./config/global.yaml"))
        admins = {str(a) for a in (cfg.admins or [])}
        if str(event.user_id) not in admins:
            return (True, "❌ 你没有权限执行此操作。")

        asyncio.create_task(self._run_sync(bot_id, event, cfg))
        return (True, "🔄 正在同步 VCPedia 新歌(约需几分钟), 完成后会通知你~")

    async def _run_sync(self, bot_id: str, event: Any, cfg) -> None:
        """后台同步(阻塞网络操作放线程池), 完成后发结果。"""
        try:
            from mohobot.agent.music_knowledge.vcpedia import sync_vcpedia_new_songs

            music_cfg = cfg.agent.music_knowledge or {}
            result = await asyncio.to_thread(sync_vcpedia_new_songs, music_cfg)
            added = result.get("added", [])
            failed = result.get("failed", [])
            lines = [f"✅ 同步完成: 新增 {len(added)} 首, 失败 {len(failed)} 首"]
            if added:
                lines.append("新增: " + "、".join(added[:10]) + ("…" if len(added) > 10 else ""))
            if failed:
                lines.append("失败: " + "、".join(failed[:5]) + ("…" if len(failed) > 5 else ""))
            reply = "\n".join(lines)
        except Exception as e:
            logger.error(f"sync-songs failed: {e}")
            reply = f"❌ 同步失败: {e}"

        try:
            from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent

            if isinstance(event, GroupMessageEvent):
                await self._send(bot_id, "group", event.group_id, reply)
            elif isinstance(event, PrivateMessageEvent):
                await self._send(bot_id, "private", event.user_id, reply)
        except Exception as e:
            logger.error(f"sync-songs 结果发送失败: {e}")

    async def _send(self, bot_id: str, chat_type: str, chat_id: Any, text: str) -> None:
        """通过 ws_server 发送(注入引用, 若不可用则放弃)。"""
        ws = getattr(self.__class__, "_ws_server", None)
        if ws is None:
            logger.warning("sync-songs: ws_server 未注入, 无法发送结果")
            return
        if chat_type == "group":
            await ws.send_group_msg(bot_id, chat_id, text)
        else:
            await ws.send_private_msg(bot_id, chat_id, text)
