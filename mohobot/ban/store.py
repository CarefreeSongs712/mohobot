"""封禁数据存储 — 全局统一名单(所有 bot 共享), 存于 data/ban/。

结构(移植自 reneban, 去掉 server/云同步):
  ban_list.json    {session_key: [记录]}   会话禁用
  banall_list.json [记录]                  全局禁用
  pass_list.json   {session_key: [记录]}   会话临时解禁
  passall_list.json [记录]                 全局临时解禁
记录: {"uid": QQ号(str), "time": 到期时间戳(int, 0=永久), "reason": str}
session_key: "group:群号" / "private:QQ号"
优先级: pass > ban > pass-all > ban-all
"""

from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.ban.models import UserDataList, UserDataModel
from mohobot.file_store import json_read, json_write


class BanStore:
    """封禁名单存储 + 判断。多 bot 共享单例, 进程内缓存 60s。"""

    def __init__(self, data_dir: str | Path = "./data", cache_ttl: int = 60):
        self._dir = Path(data_dir) / "ban"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cache_ttl = cache_ttl

        self.banlist_path = self._dir / "ban_list.json"
        self.banall_list_path = self._dir / "banall_list.json"
        self.passlist_path = self._dir / "pass_list.json"
        self.passall_list_path = self._dir / "passall_list.json"

        # 缓存: None 表示未加载
        self._ban_cache: dict[str, UserDataList] | None = None
        self._banall_cache: UserDataList | None = None
        self._pass_cache: dict[str, UserDataList] | None = None
        self._passall_cache: UserDataList | None = None
        self._cache_ts: float = 0.0

    # ── 读取/写入 ──────────────────────────────────────────────

    async def _read_dict(self, path: Path) -> dict[str, UserDataList]:
        data = await json_read(path)
        if not isinstance(data, dict):
            return {}
        result: dict[str, UserDataList] = {}
        for key, value in data.items():
            if isinstance(value, list):
                result[str(key)] = UserDataList(
                    [UserDataModel.from_dict(item) for item in value]
                )
        return result

    async def _read_list(self, path: Path) -> UserDataList:
        data = await json_read(path)
        if not isinstance(data, list):
            return UserDataList()
        return UserDataList([UserDataModel.from_dict(item) for item in data])

    @staticmethod
    async def _write_dict(path: Path, data: dict[str, UserDataList]) -> None:
        await json_write(path, {k: [dict(i) for i in v] for k, v in data.items()})

    @staticmethod
    async def _write_list(path: Path, data: UserDataList) -> None:
        await json_write(path, [dict(i) for i in data])

    async def _load_all(self) -> None:
        """加载全部 4 个名单到缓存。"""
        self._ban_cache = await self._read_dict(self.banlist_path)
        self._banall_cache = await self._read_list(self.banall_list_path)
        self._pass_cache = await self._read_dict(self.passlist_path)
        self._passall_cache = await self._read_list(self.passall_list_path)
        self._cache_ts = _time.time()

    async def _ensure_cache(self) -> None:
        """缓存过期时重载(并惰性清理过期记录)。"""
        if (
            self._ban_cache is None
            or self._banall_cache is None
            or self._pass_cache is None
            or self._passall_cache is None
            or _time.time() - self._cache_ts > self._cache_ttl
        ):
            await self.clear_banned()

    # ── 判断 ───────────────────────────────────────────────────

    async def is_banned(self, session_key: str, uid: str) -> tuple[bool, str | None]:
        """判断用户是否被禁(过期记录视为未禁)。

        Returns: (是否被禁, 理由)
        优先级: pass(会话) > ban(会话) > pass-all(全局) > ban-all(全局)
        """
        await self._ensure_cache()
        now = _time.time()
        uid = str(uid)

        def _active(item: UserDataModel) -> bool:
            return item.time == 0 or item.time > now

        # pass(会话) — 解禁优先
        pass_list = (self._pass_cache or {}).get(session_key) or UserDataList()
        for item in pass_list:
            if item.uid == uid and _active(item):
                return (False, item.reason)
        # ban(会话)
        ban_list = (self._ban_cache or {}).get(session_key) or UserDataList()
        for item in ban_list:
            if item.uid == uid and _active(item):
                return (True, item.reason)
        # pass-all(全局)
        for item in self._passall_cache or UserDataList():
            if item.uid == uid and _active(item):
                return (False, item.reason)
        # ban-all(全局)
        for item in self._banall_cache or UserDataList():
            if item.uid == uid and _active(item):
                return (True, item.reason)
        return (False, None)

    # ── 操作 ───────────────────────────────────────────────────

    async def upsert(
        self,
        list_name: str,
        uid: str,
        *,
        session_key: str | None = None,
        time_val: int = 0,
        reason: str | None = None,
    ) -> bool:
        """写入/更新记录(ban/ban-all/pass/pass-all)。

        time_val: 0=永久, 否则为到期时间戳(now + 秒数)。
        """
        await self._ensure_cache()
        now = int(_time.time())
        expiry = 0 if time_val <= 0 else now + time_val
        record = UserDataModel(uid=uid, time=expiry, reason=reason or "无理由")

        if list_name == "ban":
            assert session_key
            self._ban_cache.setdefault(session_key, UserDataList())
            lst = self._ban_cache[session_key]
            old = lst.find_by_uid(uid)
            if old:
                lst.update_user_full(uid, new_time=expiry, new_reason=record.reason)
            else:
                lst.append(record)
            await self._write_dict(self.banlist_path, self._ban_cache)
        elif list_name == "ban-all":
            lst = self._banall_cache or UserDataList()
            old = lst.find_by_uid(uid)
            if old:
                lst.update_user_full(uid, new_time=expiry, new_reason=record.reason)
            else:
                lst.append(record)
            self._banall_cache = lst
            await self._write_list(self.banall_list_path, lst)
        elif list_name == "pass":
            assert session_key
            self._pass_cache.setdefault(session_key, UserDataList())
            lst = self._pass_cache[session_key]
            old = lst.find_by_uid(uid)
            if old:
                lst.update_user_full(uid, new_time=expiry, new_reason=record.reason)
            else:
                lst.append(record)
            await self._write_dict(self.passlist_path, self._pass_cache)
        elif list_name == "pass-all":
            lst = self._passall_cache or UserDataList()
            old = lst.find_by_uid(uid)
            if old:
                lst.update_user_full(uid, new_time=expiry, new_reason=record.reason)
            else:
                lst.append(record)
            self._passall_cache = lst
            await self._write_list(self.passall_list_path, lst)
        else:
            return False
        return True

    async def delete(
        self,
        list_name: str,
        uid: str,
        *,
        session_key: str | None = None,
        seconds: int = 0,
        reason: str | None = None,
    ) -> tuple[bool, str | None]:
        """删除/削减记录(dec-ban 系列)。

        seconds=0: 直接移除记录;
        seconds>0: 削减剩余时长(永久记录不可削减)。
        Returns: (是否成功, 错误消息)
        """
        await self._ensure_cache()
        uid = str(uid)

        if list_name == "ban":
            assert session_key
            lst = (self._ban_cache or {}).get(session_key)
            if not lst or lst.find_by_uid(uid) is None:
                return (False, "未找到记录")
            if seconds > 0 and not lst.subtract_time_from_user(uid, seconds, reason):
                return (False, "永久限制不支持削减, 请用 /dec-ban 不带时间直接删除")
            if seconds == 0:
                lst.remove_by_uid(uid)
            await self._write_dict(self.banlist_path, self._ban_cache)
        elif list_name == "ban-all":
            lst = self._banall_cache or UserDataList()
            if lst.find_by_uid(uid) is None:
                return (False, "未找到记录")
            if seconds > 0 and not lst.subtract_time_from_user(uid, seconds, reason):
                return (False, "永久限制不支持削减, 请用 /dec-ban-all 不带时间直接删除")
            if seconds == 0:
                lst.remove_by_uid(uid)
            self._banall_cache = lst
            await self._write_list(self.banall_list_path, lst)
        elif list_name == "pass":
            assert session_key
            lst = (self._pass_cache or {}).get(session_key)
            if not lst or lst.find_by_uid(uid) is None:
                return (False, "未找到记录")
            if seconds > 0 and not lst.subtract_time_from_user(uid, seconds, reason):
                return (False, "永久解禁不支持削减, 请用 /dec-pass 不带时间直接删除")
            if seconds == 0:
                lst.remove_by_uid(uid)
            await self._write_dict(self.passlist_path, self._pass_cache)
        elif list_name == "pass-all":
            lst = self._passall_cache or UserDataList()
            if lst.find_by_uid(uid) is None:
                return (False, "未找到记录")
            if seconds > 0 and not lst.subtract_time_from_user(uid, seconds, reason):
                return (False, "永久解禁不支持削减, 请用 /dec-pass-all 不带时间直接删除")
            if seconds == 0:
                lst.remove_by_uid(uid)
            self._passall_cache = lst
            await self._write_list(self.passall_list_path, lst)
        else:
            return (False, "unknown list")
        return (True, None)

    async def reset_user(self, uid: str) -> None:
        """清除用户在所有名单中的记录。"""
        await self._ensure_cache()
        uid = str(uid)
        for cache in (self._banall_cache, self._passall_cache):
            if cache:
                cache.remove_by_uid(uid)
        for cache in (self._ban_cache, self._pass_cache):
            if cache:
                for lst in cache.values():
                    lst.remove_by_uid(uid)
        await self._write_dict(self.banlist_path, self._ban_cache)
        await self._write_list(self.banall_list_path, self._banall_cache)
        await self._write_dict(self.passlist_path, self._pass_cache)
        await self._write_list(self.passall_list_path, self._passall_cache)

    # ── 清理 ───────────────────────────────────────────────────

    async def clear_banned(self) -> None:
        """清除过期记录 + pass/ban 冗余, 重建缓存。"""
        await self._clear_expired()
        await self._clear_redundant()
        await self._load_all()

    async def _clear_expired(self) -> None:
        """清除已过期(非永久)的记录。"""
        now = _time.time()
        changed = False

        ban_cache = await self._read_dict(self.banlist_path)
        for key in list(ban_cache.keys()):
            kept = UserDataList(
                [i for i in ban_cache[key] if i.time == 0 or i.time > now]
            )
            if len(kept) != len(ban_cache[key]):
                changed = True
            if kept:
                ban_cache[key] = kept
            else:
                del ban_cache[key]
        if changed:
            await self._write_dict(self.banlist_path, ban_cache)

        pass_cache = await self._read_dict(self.passlist_path)
        changed = False
        for key in list(pass_cache.keys()):
            kept = UserDataList(
                [i for i in pass_cache[key] if i.time == 0 or i.time > now]
            )
            if len(kept) != len(pass_cache[key]):
                changed = True
            if kept:
                pass_cache[key] = kept
            else:
                del pass_cache[key]
        if changed:
            await self._write_dict(self.passlist_path, pass_cache)

        for path in (self.banall_list_path, self.passall_list_path):
            lst = await self._read_list(path)
            kept = UserDataList([i for i in lst if i.time == 0 or i.time > now])
            if len(kept) != len(lst):
                await self._write_list(path, kept)

    async def _clear_redundant(self) -> None:
        """清理冗余: pass 覆盖 ban(按时间), 无 ban 对应的 pass 移除。"""
        banall = await self._read_list(self.banall_list_path)
        passall = await self._read_list(self.passall_list_path)
        ban_cache = await self._read_dict(self.banlist_path)
        pass_cache = await self._read_dict(self.passlist_path)

        # 1. pass_all 覆盖 ban_all
        passall_time = {i.uid: i.time for i in passall}
        banall = UserDataList([
            item for item in banall
            if item.uid not in passall_time
            or (passall_time[item.uid] < item.time and passall_time[item.uid] != 0)
            or (item.time == 0 and passall_time[item.uid] != 0)
        ])

        # 2. pass 覆盖 ban(会话级)
        for session in list(ban_cache.keys()):
            pass_list = pass_cache.get(session, UserDataList())
            pass_time = {i.uid: i.time for i in pass_list}
            ban_cache[session] = UserDataList([
                item for item in ban_cache[session]
                if item.uid not in pass_time
                or (pass_time[item.uid] < item.time and pass_time[item.uid] != 0)
                or (item.time == 0 and pass_time[item.uid] != 0)
            ])
            if not ban_cache[session]:
                del ban_cache[session]

        # 3. 清理冗余 pass: 只保留有对应 ban/ban-all 的 uid
        banall_uids = {i.uid for i in banall}
        combined = set(banall_uids)
        for lst in ban_cache.values():
            combined.update(i.uid for i in lst)
        passall = UserDataList([i for i in passall if i.uid in combined])
        for session in list(pass_cache.keys()):
            kept = UserDataList([i for i in pass_cache[session] if i.uid in combined])
            if kept:
                pass_cache[session] = kept
            else:
                del pass_cache[session]

        await self._write_dict(self.banlist_path, ban_cache)
        await self._write_dict(self.passlist_path, pass_cache)
        await self._write_list(self.banall_list_path, banall)
        await self._write_list(self.passall_list_path, passall)

    # ── 面板数据 ───────────────────────────────────────────────

    async def get_all(self) -> dict[str, Any]:
        """导出全部名单(web 面板展示用)。"""
        await self._ensure_cache()
        return {
            "ban": {k: [dict(i) for i in v] for k, v in (self._ban_cache or {}).items()},
            "ban_all": [dict(i) for i in (self._banall_cache or UserDataList())],
            "pass": {k: [dict(i) for i in v] for k, v in (self._pass_cache or {}).items()},
            "pass_all": [dict(i) for i in (self._passall_cache or UserDataList())],
        }

    def session_key_for(self, chat_type: str, chat_id: str | int) -> str:
        """构造会话 key: group:群号 / private:QQ号。"""
        return f"{chat_type}:{chat_id}"
