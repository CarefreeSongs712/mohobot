"""通知模型 — 移植自 astrbot_plugin_relationship (core/notice/model.py)。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NoticeMessage:
    post_type: str
    notice_type: str
    sub_type: str

    user_id: str
    self_id: str
    group_id: str
    operator_id: str

    duration: int = 0

    @classmethod
    def from_raw(cls, raw: dict) -> "NoticeMessage":
        return cls(
            post_type=raw.get("post_type", ""),
            notice_type=raw.get("notice_type", ""),
            sub_type=raw.get("sub_type", ""),
            user_id=str(raw.get("user_id", "")),
            self_id=str(raw.get("self_id", "")),
            group_id=str(raw.get("group_id", "")),
            operator_id=str(raw.get("operator_id", "")),
            duration=int(raw.get("duration", 0)),
        )

    def is_self_notice(self) -> bool:
        """是否是关于 bot 自己的通知(被拉群/被踢/被禁言/管理员变动)。"""
        return (
            self.post_type == "notice"
            and self.user_id == self.self_id
            and self.operator_id != self.self_id
        )
