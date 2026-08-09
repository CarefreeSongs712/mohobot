"""封禁数据模型 — 移植自 reneban (user_manager.py), 去掉 AstrBot 依赖。

UserDataModel: 单条记录 {uid, time(到期时间戳, 0=永久), reason}
UserDataList: 记录列表, 提供按 uid 查找/更新/移除
"""

from __future__ import annotations

from typing import Any


class InvalidKeyError(KeyError):
    pass


class UserDataModel(dict):
    """一条封禁/解禁记录。"""

    ALLOWED_KEYS = frozenset({"uid", "time", "reason"})
    IMMUTABLE_KEYS = frozenset({"uid"})

    def __init__(self, uid: str, time: int, reason: str = "无理由"):
        super().__init__(uid=str(uid), time=int(time), reason=reason)
        object.__setattr__(self, "uid", str(uid))
        object.__setattr__(self, "time", int(time))
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "_initialized", True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserDataModel":
        filtered = {k: v for k, v in data.items() if k in cls.ALLOWED_KEYS}
        filtered.setdefault("time", 0)
        filtered.setdefault("reason", "无理由")
        return cls(**filtered)

    def __setitem__(self, key, value):
        if key not in self.ALLOWED_KEYS:
            raise InvalidKeyError(key)
        if key in self.IMMUTABLE_KEYS and key in self:
            raise InvalidKeyError(f"{key} is immutable")
        super().__setitem__(key, value)
        object.__setattr__(self, key, value)

    def __setattr__(self, name, value):
        if name in self.ALLOWED_KEYS:
            self[name] = value
        elif name.startswith("_") or getattr(self.__class__, name, None):
            super().__setattr__(name, value)
        else:
            raise InvalidKeyError(name)

    def __getattr__(self, name):
        if name in self.ALLOWED_KEYS:
            return self[name]
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )

    def update_data(self, *, time: int | None = None, reason: str | None = None):
        if time is not None:
            self["time"] = int(time)
        if reason is not None:
            self["reason"] = reason

    def add_time(self, seconds: int, reason: str | None = None):
        """叠加时间: 0=设为永久; 永久记录不可叠加。"""
        if self.time == 0:
            raise ValueError("Cannot add time to a permanent record")
        if seconds == 0:
            self.update_data(time=0, reason=reason)
        else:
            self.update_data(time=self.time + seconds, reason=reason)

    def subtract_time(self, seconds: int, reason: str | None = None):
        """减少时间: 0=置为过期(1); 永久记录不可减。"""
        if self.time == 0 and seconds != 0:
            raise ValueError("Cannot subtract time from a permanent record")
        if seconds == 0:
            new_time = 1
        else:
            new_time = self.time - seconds
        self.update_data(time=new_time, reason=reason)


class UserDataList(list):
    """存放 UserDataModel 的列表, 提供按 uid 操作。"""

    def __init__(self, iterable=None):
        super().__init__()
        if iterable:
            for item in iterable:
                self.append(item)

    def append(self, obj: UserDataModel):
        if not isinstance(obj, UserDataModel):
            raise TypeError(f"只能添加 UserDataModel, 实际 {type(obj)}")
        super().append(obj)

    def extend(self, iterable):
        for item in iterable:
            self.append(item)

    def find_by_uid(self, uid: str) -> UserDataModel | None:
        for user_data in self:
            if user_data.uid == str(uid):
                return user_data
        return None

    def remove_by_uid(self, uid: str) -> bool:
        for i, user_data in enumerate(self):
            if user_data.uid == str(uid):
                self.pop(i)
                return True
        return False

    def update_user_full(self, uid: str, new_time: int | None = None, new_reason: str | None = None) -> bool:
        user_data = self.find_by_uid(uid)
        if user_data:
            user_data.update_data(time=new_time, reason=new_reason)
            return True
        return False

    def add_time_to_user(self, uid: str, seconds: int, reason: str | None = None) -> bool:
        user_data = self.find_by_uid(uid)
        if user_data:
            try:
                user_data.add_time(seconds, reason)
                return True
            except ValueError:
                return False
        return False

    def subtract_time_from_user(self, uid: str, seconds: int, reason: str | None = None) -> bool:
        user_data = self.find_by_uid(uid)
        if user_data:
            try:
                user_data.subtract_time(seconds, reason)
                return True
            except ValueError:
                return False
        return False
