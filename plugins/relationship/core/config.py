"""插件配置 — 移植自 astrbot_plugin_relationship (core/config.py), 适配 mohobot。

配置来自插件配置系统(data/plugins_config/relationship.json, 面板可编辑)。
管理员从全局配置 admins 获取(注入), 审批员 = 全局管理员 + 额外 manage_users。
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class PluginConfig:
    """关系插件配置(扁平的 dict 包装)。"""

    def __init__(self, data: dict, *, admins: list[str] | None = None,
                 ws_server=None, data_dir: str = "./data"):
        self._data = data or {}
        self.ws_server = ws_server
        self._data_dir = data_dir

        # 1. 管理员(全局配置 admins 注入)
        self.admins_id = [str(a) for a in (admins or [])]
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
        check = self._data.get("check") or {}
        self.request = RequestConfig(request)
        self.notice = NoticeConfig(notice)
        self.check = CheckConfig(check)

        # 6. 黑名单引用
        self.group_blacklist = self.request.group_blacklist
        self.user_blacklist = self.request.user_blacklist

    # ── 工具 ─────────────────────────────────────────────────

    @staticmethod
    def _clean_ids(ids: Any) -> list[str]:
        if isinstance(ids, str):
            ids = [ids]
        return [str(i) for i in (ids or []) if str(i).isdigit()]

    def _append_admin_to_manage_users(self) -> None:
        if self.admin_id and self.admin_id not in self.manage_users:
            self.manage_users.append(self.admin_id)

    def is_black_group(self, group_id: str) -> bool:
        return str(group_id) in self.group_blacklist

    def add_black_group(self, group_id: str | int) -> None:
        gid = str(group_id)
        if gid not in self.group_blacklist:
            self.group_blacklist.append(gid)
            self._persist()
            logger.info(f"群聊 {gid} 已加入黑名单")

    def remove_black_group(self, group_id: str | int) -> None:
        gid = str(group_id)
        if gid in self.group_blacklist:
            self.group_blacklist.remove(gid)
            self._persist()
            logger.info(f"群聊 {gid} 已从黑名单移除")

    def is_block_user(self, user_id: str) -> bool:
        return str(user_id) in self.user_blacklist

    def add_block_user(self, user_id: str | int) -> None:
        uid = str(user_id)
        if uid not in self.user_blacklist:
            self.user_blacklist.append(uid)
            self._persist()
            logger.info(f"用户 {uid} 已加入拉黑名单")

    def remove_block_user(self, user_id: str | int) -> None:
        uid = str(user_id)
        if uid in self.user_blacklist:
            self.user_blacklist.remove(uid)
            self._persist()
            logger.info(f"用户 {uid} 已从拉黑名单移除")

    def is_manage_user(self, user_id: str) -> bool:
        return str(user_id) in self.manage_users

    def add_manage_user(self, user_id: str | int) -> None:
        uid = str(user_id)
        if uid not in self.manage_users:
            self.manage_users.append(uid)
            self._persist()
            logger.info(f"用户 {uid} 已加入审批员")

    def remove_manage_user(self, user_id: str | int) -> None:
        uid = str(user_id)
        if uid in self.manage_users:
            self.manage_users.remove(uid)
            self._persist()
            logger.info(f"用户 {uid} 已从审批员移除")

    def _persist(self) -> None:
        """把运行时修改(黑名单/审批员)写回插件配置存档。"""
        self._data.setdefault("request", {})["group_blacklist"] = list(self.group_blacklist)
        self._data["request"]["user_blacklist"] = list(self.user_blacklist)
        self._data["manage_users"] = [
            u for u in self.manage_users if u != self.admin_id
        ]
        try:
            import json
            from pathlib import Path

            path = Path(self._data_dir) / "plugins_config" / "relationship.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"关系插件配置持久化失败: {e}")


class CheckConfig:
    def __init__(self, data: dict):
        self.count = int(data.get("count", 20))
        self.batch_size = int(data.get("batch_size", 40))
        self.check_new_group = bool(data.get("check_new_group", True))
        self.delay = int(data.get("delay", 30))


class RequestConfig:
    def __init__(self, data: dict):
        self.group_blacklist = [str(i) for i in data.get("group_blacklist", []) or []]
        self.user_blacklist = [str(i) for i in data.get("user_blacklist", []) or []]
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
        self.mutual_blacklist = [str(i) for i in data.get("mutual_blacklist", []) or []]
        self.max_duration = self.max_ban_days * 24 * 60 * 60

    def is_mutual(self, group_id: str) -> bool:
        return str(group_id) in self.mutual_blacklist
