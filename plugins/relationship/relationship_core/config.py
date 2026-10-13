"""插件配置 — 移植自 astrbot_plugin_relationship (core/config.py), 适配 mohobot。

配置来自插件配置系统(data/plugins_config/relationship.json, 面板可编辑)。
管理员从全局配置 admins 获取(注入), 审批员 = 全局管理员 + 额外 manage_users。

并发安全: 修改方法(加/减审批员、拉黑/解黑)持共享锁 + 从磁盘重读最新存档后
再修改, _persist 用 json_update 原子读改写 — 6 bot 并发指令不丢更新,
面板保存与插件运行时写回互不覆盖。
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def normalize_id(value: Any) -> str:
    """把 QQ 号/群号规范化为统一的比较与存档形式。

    去首尾空白、去多余前导零, 全角数字转半角(``int()`` 本身接受全角)。
    只有纯十进制且大于 0 的数字才合法: ``"0222"``/``" 222 "``/``"２２２"``
    → ``"222"``; ``0``、负数、空串、``None``、非数字一律 → ``""``
    (调用方据此拒绝写入)。

    名单的写入与查询都过这一层, 否则 ``operator_id="0222"`` 会被原样存档,
    后续 ``is_block_user("222")`` 匹配不到 —— 管理员收到"已拉黑"提示,
    实际却不生效。
    """
    text = str(value).strip()
    if not text.isdecimal():
        return ""
    number = int(text)
    if number <= 0:
        return ""
    return str(number)


class PluginConfig:
    """关系插件配置(扁平的 dict 包装)。"""

    def __init__(self, data: dict, *, admins: list[str] | None = None,
                 ws_server=None, data_dir: str = "./data", lock=None):
        # Keep even an empty injected dictionary shared with Plugin.plugin_config.
        self._data = data if isinstance(data, dict) else {}
        self.ws_server = ws_server
        self._data_dir = data_dir
        # 跨实例共享锁(多个 _ensure_handlers 重建的 cfg 引用同一把锁)
        self._lock = lock
        self._admin_ids = [n for n in (normalize_id(a) for a in (admins or [])) if n]
        self._rebuild()

    def _rebuild(self) -> None:
        """从 self._data 重建全部派生字段(init 与磁盘重读共用)。"""
        # 1. 管理员(全局配置 admins 注入)
        self.admins_id = [str(a) for a in (self._admin_ids or [])]
        self.admin_id = self.admins_id[0] if self.admins_id else None

        # 2. 审批员 = 全局管理员 + 额外 manage_users
        self.manage_users = self._clean_ids(self._data.get("manage_users", []))
        self._append_admin_to_manage_users()

        # 3. 审批群号校验
        self.manage_group = (
            str(self._data.get("manage_group", "") or "")
            if str(self._data.get("manage_group", "") or "").isdigit()
            else ""
        )

        # 4. 合法性提醒
        if not self.manage_group and not self.manage_users:
            logger.warning("关系插件: 未配置审批群或审批员, 将无法发送审批消息")

        # 5. 子配置
        request = self._data.get("request") or {}
        notice = self._data.get("notice") or {}
        self.request = RequestConfig(request)
        self.notice = NoticeConfig(notice)

        # 6. 黑名单引用
        self.group_blacklist = self.request.group_blacklist
        self.user_blacklist = self.request.user_blacklist

    # ── 工具 ─────────────────────────────────────────────────

    @staticmethod
    def _clean_ids(ids: Any) -> list[str]:
        if isinstance(ids, str):
            ids = [ids]
        clean: list[str] = []
        for value in ids or []:
            normalized = normalize_id(value)
            if normalized and normalized not in clean:
                clean.append(normalized)
        return clean

    def _append_admin_to_manage_users(self) -> None:
        """确保所有全局管理员都在审批员列表中(不止第一个)。"""
        for admin_id in self.admins_id:
            if admin_id and admin_id not in self.manage_users:
                self.manage_users.append(admin_id)

    def _config_path(self):
        from pathlib import Path

        return Path(self._data_dir) / "plugins_config" / "relationship.json"

    async def _reload_from_disk(self) -> None:
        """锁内: 从磁盘重读最新存档并重建派生字段。

        并发修改/面板保存后, 后续协程必须基于最新状态操作,
        否则旧快照写回会覆盖他人的修改(丢更新)。
        """
        from mohobot.file_store import json_read

        data = await json_read(self._config_path())
        if isinstance(data, dict):
            self._data.clear()
            self._data.update(data)
        self._rebuild()

    def is_black_group(self, group_id: str) -> bool:
        gid = normalize_id(group_id)
        return bool(gid) and gid in self.group_blacklist

    async def add_black_group(self, group_id: str | int) -> None:
        gid = normalize_id(group_id)
        if not gid:
            logger.warning(f"关系插件: 群号非法, 忽略拉黑: {group_id!r}")
            return
        async with self._lock:
            await self._reload_from_disk()
            if gid not in self.group_blacklist:
                self.group_blacklist.append(gid)
                await self._persist()
                logger.info(f"群聊 {gid} 已加入黑名单")

    async def remove_black_group(self, group_id: str | int) -> None:
        gid = normalize_id(group_id)
        if not gid:
            return
        async with self._lock:
            await self._reload_from_disk()
            if gid in self.group_blacklist:
                self.group_blacklist.remove(gid)
                await self._persist()
                logger.info(f"群聊 {gid} 已从黑名单移除")

    def is_block_user(self, user_id: str) -> bool:
        uid = normalize_id(user_id)
        return bool(uid) and uid in self.user_blacklist

    async def add_block_user(self, user_id: str | int) -> None:
        uid = normalize_id(user_id)
        if not uid:
            logger.warning(f"关系插件: 用户号非法, 忽略拉黑: {user_id!r}")
            return
        async with self._lock:
            await self._reload_from_disk()
            if uid not in self.user_blacklist:
                self.user_blacklist.append(uid)
                await self._persist()
                logger.info(f"用户 {uid} 已加入拉黑名单")

    async def remove_block_user(self, user_id: str | int) -> None:
        uid = normalize_id(user_id)
        if not uid:
            return
        async with self._lock:
            await self._reload_from_disk()
            if uid in self.user_blacklist:
                self.user_blacklist.remove(uid)
                await self._persist()
                logger.info(f"用户 {uid} 已从拉黑名单移除")

    def is_manage_user(self, user_id: str) -> bool:
        uid = normalize_id(user_id)
        return bool(uid) and uid in self.manage_users

    async def add_manage_user(self, user_id: str | int) -> None:
        uid = normalize_id(user_id)
        if not uid:
            logger.warning(f"关系插件: 用户号非法, 忽略加审批员: {user_id!r}")
            return
        async with self._lock:
            await self._reload_from_disk()
            if uid not in self.manage_users:
                self.manage_users.append(uid)
                await self._persist()
                logger.info(f"用户 {uid} 已加入审批员")

    async def remove_manage_user(self, user_id: str | int) -> None:
        uid = normalize_id(user_id)
        if not uid:
            return
        async with self._lock:
            await self._reload_from_disk()
            if uid in self.manage_users:
                self.manage_users.remove(uid)
                await self._persist()
                logger.info(f"用户 {uid} 已从审批员移除")

    async def _persist(self) -> None:
        """把运行时修改(黑名单/审批员)原子写回插件配置存档。

        用 json_update 以磁盘当前值为基础更新, 保留面板保存的其它字段,
        与并发修改/面板保存互不覆盖。
        """
        from mohobot.file_store import json_update

        def _merge(current):
            from copy import deepcopy

            if isinstance(current, dict) and current:
                base = current
            else:
                # 磁盘尚无存档时以内存快照为基底: 宿主注入但未落盘的字段(如
                # notice 开关、max_ban_days)不能因为一次拉黑写回就被抹成默认值。
                base = deepcopy(self._data) if isinstance(self._data, dict) else {}
            base.setdefault("request", {})
            base["request"]["group_blacklist"] = list(self.group_blacklist)
            base["request"]["user_blacklist"] = list(self.user_blacklist)
            # 存档只保留"额外审批员"(全局管理员自动加入, 不落盘)
            admin_set = set(self.admins_id)
            base["manage_users"] = [
                u for u in self.manage_users if u not in admin_set
            ]
            return base

        try:
            persisted = await json_update(self._config_path(), _merge, default={})
            # 每个入口钩子都用 plugin_config 重建 handler, 所以必须把结果发布回
            # 同一份字典; 但只发布 _merge 负责的字段, 不做整字典替换 —— 否则
            # 磁盘快照里没有的宿主注入配置会被一并抹掉。
            if isinstance(persisted, dict):
                disk_request = persisted.get("request")
                disk_request = disk_request if isinstance(disk_request, dict) else {}
                request = self._data.get("request")
                if not isinstance(request, dict):
                    request = {}
                    self._data["request"] = request
                request["group_blacklist"] = list(disk_request.get("group_blacklist") or [])
                request["user_blacklist"] = list(disk_request.get("user_blacklist") or [])
                if "manage_users" in persisted:
                    self._data["manage_users"] = list(persisted.get("manage_users") or [])
            self._rebuild()
        except Exception as e:
            logger.warning(f"关系插件配置持久化失败: {e}")


class RequestConfig:
    def __init__(self, data: dict):
        # 名单在进出口都规范化: 面板里手填的 "0222"/全角数字也能与事件字段匹配。
        self.group_blacklist = self._clean(data.get("group_blacklist"))
        self.user_blacklist = self._clean(data.get("user_blacklist"))
        self.auto_reject_group = bool(data.get("auto_reject_group", False))
        self.auto_agree_group = bool(data.get("auto_agree_group", False))
        self.auto_reject_friend = bool(data.get("auto_reject_friend", False))
        self.auto_agree_friend = bool(data.get("auto_agree_friend", False))

    @staticmethod
    def _clean(values: Any) -> list[str]:
        ids: list[str] = []
        for value in values or []:
            normalized = normalize_id(value)
            if normalized and normalized not in ids:
                ids.append(normalized)
        return ids
        self.auto_reject_group = bool(data.get("auto_reject_group", False))
        self.auto_agree_group = bool(data.get("auto_agree_group", False))
        self.auto_reject_friend = bool(data.get("auto_reject_friend", False))
        self.auto_agree_friend = bool(data.get("auto_agree_friend", False))


class NoticeConfig:
    def __init__(self, data: dict):
        self.block_small_group = bool(data.get("block_small_group", False))
        self.min_group_size = int(data.get("min_group_size", 50))
        self.max_group_size = int(data.get("max_group_size", 2000))
        self.max_group_capacity = int(data.get("max_group_capacity", 100))
        self.max_ban_days = int(data.get("max_ban_days", 3))
        self.kick_block_user = bool(data.get("kick_block_user", True))
        self.kick_block_group = bool(data.get("kick_block_group", True))
        self.mutual_blacklist = RequestConfig._clean(data.get("mutual_blacklist"))
        self.max_duration = self.max_ban_days * 24 * 60 * 60

    def is_mutual(self, group_id: str) -> bool:
        gid = normalize_id(group_id)
        return bool(gid) and gid in self.mutual_blacklist
