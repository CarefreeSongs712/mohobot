"""QQ 空间说说数据模型（移植自 astrbot_plugin_qzone_lite core/model.py）。"""

from __future__ import annotations

import re
from datetime import datetime

import pydantic
from pydantic import BaseModel


def extract_and_replace_nickname(input_string: str) -> str:
    """把 {nick:xxx, ...} 标记替换为昵称文本。"""
    pattern = r"\{[^{}]*\}"

    def replace_func(match):
        content = match.group(0)
        pairs = content[1:-1].split(",")
        nick_value = ""
        for pair in pairs:
            if ":" not in pair:
                continue
            key, value = pair.split(":", 1)
            if key.strip() == "nick":
                nick_value = value.strip()
                break
        return f"{nick_value} " if nick_value else ""

    return re.sub(pattern, replace_func, input_string)


def remove_em_tags(text: str) -> str:
    return re.sub(r"\[em\].*?\[/em\]", "", text)


class Comment(BaseModel):
    uin: int
    nickname: str
    content: str
    create_time: int
    create_time_str: str = ""
    tid: int = 0
    parent_tid: int | None = None
    source_name: str = ""
    source_url: str = ""

    @property
    def plain_content(self) -> str:
        return re.sub(r"\[em\]e\d+\[/em\]", "", self.content)

    @staticmethod
    def from_raw(raw: dict, parent_tid: int | None = None) -> "Comment":
        return Comment(
            uin=int(raw.get("uin") or 0),
            nickname=raw.get("name") or "",
            content=raw.get("content") or "",
            create_time=int(raw.get("create_time") or 0),
            create_time_str=raw.get("createTime2") or "",
            tid=int(raw.get("tid") or 0),
            parent_tid=parent_tid,
            source_name=raw.get("source_name") or "",
            source_url=raw.get("source_url") or "",
        )

    @staticmethod
    def build_list(comment_list: list[dict]) -> list["Comment"]:
        res: list["Comment"] = []
        for main in comment_list:
            main_tid = int(main.get("tid") or 0)
            res.append(Comment.from_raw(main, parent_tid=None))
            for sub in main.get("list_3") or []:
                res.append(Comment.from_raw(sub, parent_tid=main_tid))
        return res

    def __str__(self) -> str:
        flag = "└─↩" if self.parent_tid else "●"
        return f"{flag} {self.nickname}({self.uin}): {self.plain_content}"


class Post(pydantic.BaseModel):
    id: int | None = None
    tid: str | None = None
    uin: int = 0
    name: str = ""
    gin: int = 0
    text: str = ""
    images: list[str] = pydantic.Field(default_factory=list)
    videos: list[str] = pydantic.Field(default_factory=list)
    anon: bool = False
    status: str = "approved"
    create_time: int = pydantic.Field(default_factory=lambda: int(datetime.now().timestamp()))
    rt_con: str = ""
    comments: list[Comment] = pydantic.Field(default_factory=list)
    extra_text: str | None = None

    def replyable_comments(self) -> list[Comment]:
        return self.comments

    def to_str(self) -> str:
        dt = datetime.fromtimestamp(
            int(self.create_time) if str(self.create_time).isdigit()
            else int(datetime.now().timestamp())
        )
        lines = [f"### {self.name}({self.uin}) 发布于 {dt.strftime('%Y-%m-%d %H:%M')}"]
        if self.text:
            lines.append(f"\n{remove_em_tags(self.text)}\n")
        if self.rt_con:
            lines.append(f"\n[转发]：{remove_em_tags(self.rt_con)}\n")
        if self.images:
            lines.append("\n图片：")
            lines.extend(self.images)
        if self.extra_text:
            lines.append(f"\n图片分析：\n{self.extra_text}")
        if self.videos:
            lines.append("\n视频：")
            lines.extend(self.videos)
        comments = self.replyable_comments()
        if comments:
            lines.append("\n评论：")
            for idx, c in enumerate(comments):
                lines.append(
                    f"{idx}. {remove_em_tags(c.nickname)}："
                    f"{remove_em_tags(extract_and_replace_nickname(c.content))}"
                )
        return "\n".join(lines)
