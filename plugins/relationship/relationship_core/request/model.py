"""请求模型 — 移植自 astrbot_plugin_relationship (core/request/model.py)。

BaseRequest 统一 display/parse/raw 构造; 审批命令通过解析引用消息文本
(【好友申请】/【群邀请】格式)定位申请。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from relationship_core.utils import api_call


class BaseRequest(ABC):
    """申请基类(好友申请/群邀请)。"""

    _HEADER: ClassVar[str]
    _FIELD_MAP: ClassVar[dict[str, str]]

    def to_display_text(self) -> str:
        lines = [self._HEADER]
        for cn, fld in self._FIELD_MAP.items():
            lines.append(f"{cn}：{getattr(self, fld)}")
        return "\n".join(lines)

    @classmethod
    def from_display_text(cls, text: str) -> Optional["BaseRequest"]:
        for sub in cls.__subclasses__():
            req = sub._from_display_text(text)
            if req:
                return req
        return None

    @classmethod
    def _from_display_text(cls, text: str) -> Optional["BaseRequest"]:
        if cls._HEADER not in text:
            return None
        kwargs = {}
        for line in (text or "").splitlines():
            if "：" not in line:
                continue
            key, _, val = line.partition("：")
            fld = cls._FIELD_MAP.get(key.strip())
            if fld:
                kwargs[fld] = val.strip()
        required = set(cls._FIELD_MAP.values()) - {"comment"}
        if not required <= kwargs.keys():
            return None
        kwargs.setdefault("comment", "无")
        return cls(**kwargs)

    @classmethod
    async def from_raw(cls, ws_server, bot_id: str, raw: dict) -> Optional["BaseRequest"]:
        if not isinstance(raw, dict):
            return None
        for sub in cls.__subclasses__():
            req = await sub._from_raw(ws_server, bot_id, raw)
            if req:
                return req
        return None

    @classmethod
    @abstractmethod
    async def _from_raw(cls, ws_server, bot_id: str, raw: dict) -> Optional["BaseRequest"]:
        raise NotImplementedError

    @property
    @abstractmethod
    def requester_id(self) -> str:
        raise NotImplementedError


@dataclass
class FriendRequest(BaseRequest):
    nickname: str
    user_id: str
    flag: str
    comment: str

    _HEADER = "【好友申请】同意/拒绝/拉黑："
    _FIELD_MAP = {"昵称": "nickname", "QQ号": "user_id", "flag": "flag", "验证信息": "comment"}

    @property
    def requester_id(self) -> str:
        return self.user_id

    @classmethod
    async def _from_raw(cls, ws_server, bot_id: str, raw: dict) -> Optional["FriendRequest"]:
        if not (raw.get("post_type") == "request" and raw.get("request_type") == "friend"):
            return None
        user_id = raw.get("user_id", 0)
        info = await api_call(ws_server, bot_id, "get_stranger_info", {"user_id": int(user_id)}) or {}
        return cls(
            nickname=info.get("nickname") or "未知昵称",
            user_id=str(user_id),
            flag=raw.get("flag", ""),
            comment=raw.get("comment") or "无",
        )


@dataclass
class GroupRequest(BaseRequest):
    inviter_nickname: str
    inviter_id: str
    group_name: str
    group_id: str
    flag: str
    comment: str

    _HEADER = "【群邀请】同意/拒绝/拉黑："
    _FIELD_MAP = {
        "邀请人昵称": "inviter_nickname", "邀请人QQ": "inviter_id",
        "群名称": "group_name", "群号": "group_id",
        "flag": "flag", "验证信息": "comment",
    }

    @property
    def requester_id(self) -> str:
        return self.inviter_id

    @classmethod
    async def _from_raw(cls, ws_server, bot_id: str, raw: dict) -> Optional["GroupRequest"]:
        if not (
            raw.get("post_type") == "request"
            and raw.get("request_type") == "group"
            and raw.get("sub_type") == "invite"
        ):
            return None
        inviter_id = raw.get("user_id", 0)
        group_id = raw.get("group_id", 0)

        inviter_info = await api_call(
            ws_server, bot_id, "get_stranger_info", {"user_id": int(inviter_id)}
        ) or {}
        group_info = await api_call(
            ws_server, bot_id, "get_group_info", {"group_id": int(group_id)}
        ) or {}

        return cls(
            inviter_nickname=inviter_info.get("nickname") or "未知昵称",
            inviter_id=str(inviter_id),
            group_name=group_info.get("group_name") or "未知群名",
            group_id=str(group_id),
            flag=raw.get("flag", ""),
            comment=raw.get("comment") or "无",
        )
