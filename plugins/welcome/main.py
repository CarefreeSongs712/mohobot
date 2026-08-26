"""Mohobot 欢迎插件 — 新加好友 / 新入群时自动发送欢迎消息。

采用"监控列表对比"方式(不使用 group_increase/friend_add 通知):
- 框架周期任务每 check_interval_sec 秒(默认 60)触发一次检查:
  拉取 get_group_list / get_friend_list, 与持久化的已知集合对比
- 发现新群/新好友 → 随机延迟 3~5s 发送欢迎, 并更新已知集合
- 同时向全局管理员(admins)私聊通知(含目标名称)
- 消失的群/好友 → 从已知集合移除(被踢/退群/删好友)
- 已知集合持久化到 data/plugins_data/welcome/known.json:
  首次运行时以当前列表为基线(已有群/好友不欢迎), 重启后继续对比

配置(WebUI 插件页可编辑): welcome_friend_enabled/welcome_friend_msg/
welcome_group_enabled/welcome_group_msg/delay_min/delay_max/
check_interval_sec/admin_notify_enabled。
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

    # 框架注入引用
    _ws_server = None
    _data_dir = "./data"
    _admin_ids: list[str] = []  # 全局管理员(框架注入, 通知接收者)

    _DEFAULTS = {
        "welcome_friend_enabled": True,
        "welcome_friend_msg": _NEW_MSG,
        "welcome_group_enabled": True,
        "welcome_group_msg": _NEW_MSG,
        "delay_min": 3,
        "delay_max": 5,
        "check_interval_sec": 60,      # 列表对比间隔(秒)
        "admin_notify_enabled": True,  # 发现新群/新好友时通知管理员
    }

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    @classmethod
    def inject_admin_ids(cls, admin_ids) -> None:
        """框架注入全局管理员(欢迎通知接收者)。"""
        cls._admin_ids = [str(a) for a in (admin_ids or [])]

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)

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

    async def _fetch_list(self, bot_id: str, action: str) -> tuple[bool, list[dict]]:
        """拉取列表并校验响应; 失败时返回无效标记, 绝不当作空列表。"""
        ws = self._ws_server
        if ws is None:
            return False, []
        try:
            resp = await ws.send_to_bot(
                bot_id, action, {}, wait_response=True, timeout=10.0,
            )
            if (not isinstance(resp, dict) or resp.get("status") != "ok"
                    or resp.get("retcode", 0) not in (0, None)):
                return False, []
            data = resp.get("data")
            if not isinstance(data, list):
                return False, []
            return True, data
        except Exception as e:
            logger.warning(f"获取列表失败({bot_id} {action}): {e}")
            return False, []

    async def _commit_baseline(self, bot_id: str, kind: str, current: list[str]) -> None:
        """原子提交单个 bot 的群或好友基线。"""
        from mohobot.file_store import json_update

        def update(data: Any) -> dict:
            if not isinstance(data, dict):
                data = {}
            data.setdefault("groups", {})
            data.setdefault("friends", {})
            data[kind][bot_id] = current
            return data

        await json_update(self._known_path(), update, default={})

    async def _check_relationship(self, bot_id: str, kind: str, valid: bool,
                                  current: list[str], known: dict) -> None:
        if not valid:
            return
        known_map = known[kind]
        initialized = bot_id in known_map
        previous = set(str(value) for value in (known_map.get(bot_id) or []))
        if not initialized:
            await self._commit_baseline(bot_id, kind, current)
            logger.debug(f"欢迎插件首启基线: {bot_id} {kind} {len(current)} 个")
            return

        target_type = "group" if kind == "groups" else "friend"
        new_items = [value for value in current if value not in previous]
        failed = set()
        for target_id in new_items:
            sent = await self._send_welcome(
                bot_id, target_type=target_type, target_id=target_id,
            )
            if not sent:
                failed.add(target_id)
                continue
            await self._notify_admin(
                bot_id, target_type=target_type, target_id=target_id,
                name=await self._get_target_name(
                    bot_id, target_type=target_type, target_id=target_id,
                ),
            )

        # Disabled/empty templates report success; actual send failures remain retryable.
        committed = [value for value in current if value not in failed]
        await self._commit_baseline(bot_id, kind, committed)
        if new_items:
            logger.info(f"欢迎插件: {bot_id} {kind} 新增 {new_items}, 失败 {sorted(failed)}")

    async def _check_all(self, bot_id: str) -> None:
        ws = self._ws_server
        if ws is None:
            return
        known = await self._load_known()
        groups_ok, groups_data = await self._fetch_list(bot_id, "get_group_list")
        friends_ok, friends_data = await self._fetch_list(bot_id, "get_friend_list")
        groups_now = [str(g.get("group_id")) for g in groups_data
                      if isinstance(g, dict) and g.get("group_id")] if groups_ok else []
        friends_now = [str(f.get("user_id")) for f in friends_data
                       if isinstance(f, dict) and f.get("user_id")] if friends_ok else []
        await self._check_relationship(bot_id, "groups", groups_ok, groups_now, known)
        await self._check_relationship(bot_id, "friends", friends_ok, friends_now, known)

    # ── 管理员通知 ───────────────────────────────────────────

    async def _get_target_name(
        self, bot_id: str, *, target_type: str, target_id: str,
    ) -> str:
        """查目标名称(群名/好友昵称); 失败或缺失回退目标 ID, 绝不让通知失败。"""
        ws = self._ws_server
        if ws is None or not target_id:
            return target_id
        try:
            if target_type == "group":
                resp = await ws.send_to_bot(
                    bot_id, "get_group_info",
                    {"group_id": int(target_id)},
                    wait_response=True, timeout=8.0,
                )
                data = (resp or {}).get("data") or {}
                return str(data.get("group_name") or "") or target_id
            resp = await ws.send_to_bot(
                bot_id, "get_stranger_info",
                {"user_id": int(target_id)},
                wait_response=True, timeout=8.0,
            )
            data = (resp or {}).get("data") or {}
            return str(data.get("nickname") or "") or target_id
        except Exception as e:
            logger.debug(f"查询目标名称失败({target_type}={target_id}): {e}")
            return target_id

    async def _notify_admin(
        self, bot_id: str, *, target_type: str, target_id: str, name: str,
    ) -> None:
        """向全局管理员私聊通知: 发现新群/新好友(含目标名称)。"""
        if not self._cfg("admin_notify_enabled", True):
            return
        if not self._admin_ids:
            return
        ws = self._ws_server
        if ws is None:
            return
        label = "新群" if target_type == "group" else "新好友"
        text = (
            f"【welcome】{label}: {name}({target_id})\n"
            f"bot {bot_id} 已发送欢迎消息"
        )
        for admin in self._admin_ids:
            try:
                await ws.send_private_msg(bot_id, int(admin), text)
                logger.info(f"管理员通知已发送: {admin} ({label}={target_id})")
            except Exception as e:
                logger.warning(f"管理员通知发送失败({admin}): {e}")

    # ── 发送 ─────────────────────────────────────────────────

    async def _send_welcome(
        self, bot_id: str, *, target_type: str, target_id: str,
    ) -> bool:
        ws = self._ws_server
        if ws is None or not target_id:
            return False
        if target_type == "friend":
            enabled_key, msg_key = "welcome_friend_enabled", "welcome_friend_msg"
        else:
            enabled_key, msg_key = "welcome_group_enabled", "welcome_group_msg"
        if not self._cfg(enabled_key, True):
            return True
        template = str(self._cfg(msg_key, "") or "").strip()
        if not template:
            return True

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
            return True
        except Exception as e:
            logger.warning(f"欢迎消息发送失败({target_type}={target_id}): {e}")
            return False

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