"""Mohobot 欢迎插件 — 新加好友 / 新入群时自动发送欢迎消息。

采用"监控列表对比"方式(不使用 group_increase/friend_add 通知):
- 框架周期任务每 check_interval_sec 秒(默认 60)触发一次检查:
  拉取 get_group_list / get_friend_list, 与持久化的已知集合对比
- 发现新群/新好友 → 随机延迟 3~5s 发送欢迎, 并更新已知集合
- 消失的群/好友 → 从已知集合移除(被踢/退群/删好友)
- 已知集合持久化到 data/plugins_data/welcome/known.json:
  首次运行时以当前列表为基线(已有群/好友不欢迎), 重启后继续对比

配置(WebUI 插件页可编辑): welcome_friend_enabled/welcome_friend_msg/
welcome_group_enabled/welcome_group_msg/delay_min/delay_max/check_every_heartbeats。
模板占位符「xxx（bot昵称）」(兼容旧「【此处替换为 bot 的昵称】」)
发送时自动替换为该 bot 昵称(取不到回退 bot_id)。
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

from loguru import logger

_NEW_MSG = (
    "这里是xxx（bot昵称）！\n"
    "这是一个QQ机器人，具有AI对话等多种功能~\n"
    "介绍+使用须知（浏览器打开）：http://103.236.70.18:712/ 或 https://7121099.xyz/ \n"
    "交流群：398870315"
)


class Plugin:
    """欢迎消息: 监控群/好友列表对比, 发现新增自动发送(可配置开关与文案)。"""

    info = {
        "description": "新加好友/新入群自动发送欢迎消息(监控列表对比方式, 可配置)",
    }

    _ws_server = None
    _data_dir = "./data"

    _DEFAULTS = {
        "welcome_friend_enabled": True,
        "welcome_friend_msg": _NEW_MSG,
        "welcome_group_enabled": True,
        "welcome_group_msg": _NEW_MSG,
        "delay_min": 3,
        "delay_max": 5,
        "check_interval_sec": 60,  # 列表对比间隔(秒)
    }

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    def _cfg(self, key: str, default):
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None else default

    # ── 周期任务(框架调度) ───────────────────────────────────

    @property
    def interval_sec(self) -> int:
        """框架周期任务间隔: 读取配置, 热更新即时生效。"""
        return max(5, int(self._cfg("check_interval_sec", 60)))

    async def on_tick(self) -> None:
        """周期任务: 遍历所有已注册 bot, 逐个对比群/好友列表。"""
        ws = self._ws_server
        if ws is None:
            return
        bm = getattr(ws, "_bot_manager", None)
        if bm is None:
            return
        for bot in list(bm.all_bots):
            try:
                await self._check_all(bot.bot_id)
            except Exception as e:
                logger.warning(f"欢迎插件列表检查失败({bot.bot_id}): {e}")

    # ── 列表对比 ─────────────────────────────────────────────

    def _known_path(self) -> Path:
        return Path(self._data_dir) / "plugins_data" / "welcome" / "known.json"

    async def _load_known(self) -> dict:
        from mohobot.file_store import json_read
        data = await json_read(self._known_path())
        if not isinstance(data, dict):
            return {"groups": {}, "friends": {}}
        data.setdefault("groups", {})
        data.setdefault("friends", {})
        return data

    async def _save_known(self, data: dict) -> None:
        from mohobot.file_store import json_write
        await json_write(self._known_path(), data)

    async def _fetch_list(self, bot_id: str, action: str) -> list[dict]:
        """拉取群/好友列表; 失败返回 []。"""
        ws = self._ws_server
        if ws is None:
            return []
        try:
            resp = await ws.send_to_bot(
                bot_id, action, {}, wait_response=True, timeout=10.0,
            )
            data = (resp or {}).get("data") or []
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"获取列表失败({bot_id} {action}): {e}")
            return []

    async def _check_all(self, bot_id: str) -> None:
        ws = self._ws_server
        if ws is None:
            return
        known = await self._load_known()
        groups_known = set(str(g) for g in known["groups"].get(bot_id, []))
        friends_known = set(str(f) for f in known["friends"].get(bot_id, []))

        groups_now = [str(g.get("group_id", "")) for g in await self._fetch_list(bot_id, "get_group_list") if g.get("group_id")]
        friends_now = [str(f.get("user_id", "")) for f in await self._fetch_list(bot_id, "get_friend_list") if f.get("user_id")]

        # 首次(无基线): 以当前列表为基线, 不欢迎已有
        if not groups_known and not friends_known and not known["groups"].get(bot_id) and not known["friends"].get(bot_id):
            known["groups"][bot_id] = groups_now
            known["friends"][bot_id] = friends_now
            await self._save_known(known)
            logger.debug(f"欢迎插件首启基线: {bot_id} 群 {len(groups_now)} 个, 好友 {len(friends_now)} 个")
            return

        # 发现新群/新好友 → 欢迎
        new_groups = [g for g in groups_now if g not in groups_known]
        new_friends = [f for f in friends_now if f not in friends_known]
        for gid in new_groups:
            await self._send_welcome(bot_id, target_type="group", target_id=gid)
        for fid in new_friends:
            await self._send_welcome(bot_id, target_type="friend", target_id=fid)

        # 更新基线(新增 + 移除消失的)
        known["groups"][bot_id] = groups_now
        known["friends"][bot_id] = friends_now
        await self._save_known(known)
        if new_groups or new_friends:
            logger.info(f"欢迎插件: {bot_id} 新群 {new_groups}, 新好友 {new_friends}")

    # ── 发送 ─────────────────────────────────────────────────

    async def _send_welcome(
        self, bot_id: str, *, target_type: str, target_id: str,
    ) -> None:
        ws = self._ws_server
        if ws is None or not target_id:
            return
        if target_type == "friend":
            enabled_key, msg_key = "welcome_friend_enabled", "welcome_friend_msg"
        else:
            enabled_key, msg_key = "welcome_group_enabled", "welcome_group_msg"
        if not self._cfg(enabled_key, True):
            return
        template = str(self._cfg(msg_key, "") or "").strip()
        if not template:
            return

        nickname = await self._get_bot_nickname(bot_id)
        text = template.replace("【此处替换为 bot 的昵称】", nickname)
        text = text.replace("xxx（bot昵称）", nickname)

        delay_min = max(0, int(self._cfg("delay_min", 3)))
        delay_max = max(delay_min, int(self._cfg("delay_max", 5)))
        await asyncio.sleep(random.uniform(delay_min, delay_max))

        try:
            if target_type == "group":
                await ws.send_group_msg(bot_id, int(target_id), text)
            else:
                await ws.send_private_msg(bot_id, int(target_id), text)
            logger.info(f"欢迎消息已发送: {target_type}={target_id} (bot {bot_id})")
        except Exception as e:
            logger.warning(f"欢迎消息发送失败({target_type}={target_id}): {e}")

    async def _get_bot_nickname(self, bot_id: str) -> str:
        """取 bot 配置的昵称; 无 bot_manager 引用时回退 bot_id。"""
        ws = self._ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        if bm is None:
            return bot_id
        try:
            instance = bm.get(bot_id)
            if instance is not None and getattr(instance, "nickname", ""):
                return instance.nickname
        except Exception:
            pass
        return bot_id
