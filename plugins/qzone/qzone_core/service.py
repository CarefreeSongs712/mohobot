"""说说业务服务层（移植自 astrbot_plugin_qzone_lite core/service.py）。

保留: 查询缓存(post/query 两级 LRU+TTL)、详情补全、错误映射、评论/回评/点赞/删除/发布。
配置访问改为 cfg_get(key, default) 回调。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from .model import Comment, Post
from .qzone import QzoneAPI, QzoneParser, QzoneSession
from .qzone.constants import (
    HTTP_STATUS_FORBIDDEN,
    QZONE_CODE_LOGIN_EXPIRED,
    QZONE_CODE_PERMISSION_DENIED,
    QZONE_CODE_PERMISSION_DENIED_LEGACY,
    QZONE_CODE_UNKNOWN,
    QZONE_INTERNAL_HTTP_STATUS_KEY,
    QZONE_INTERNAL_META_KEY,
    QZONE_MSG_EMPTY_RESPONSE,
    QZONE_MSG_INVALID_RESPONSE,
    QZONE_MSG_JSON_PARSE_ERROR,
    QZONE_MSG_NON_OBJECT_RESPONSE,
    QZONE_MSG_PERMISSION_DENIED,
)


@dataclass
class _PostCacheEntry:
    post: Post
    ts: float


@dataclass
class _QueryCacheEntry:
    post_keys: list[tuple[int, str]]
    ts: float


class LitePostService:
    def __init__(
        self,
        qzone: QzoneAPI,
        session: QzoneSession,
        cfg_get: Callable[[str, Any], Any],
    ):
        self.qzone = qzone
        self.session = session
        self.cfg_get = cfg_get
        self._post_cache: OrderedDict[tuple[int, str], _PostCacheEntry] = OrderedDict()
        self._query_cache: OrderedDict[tuple[str, int, int], _QueryCacheEntry] = OrderedDict()

    async def query_feeds(
        self,
        *,
        target_id: str | None = None,
        pos: int = 0,
        num: int = 1,
        with_detail: bool = False,
        no_self: bool = False,
        no_commented: bool = False,
    ) -> list[Post]:
        cache_key = self._query_cache_key(target_id, pos, num)
        posts = self._get_cached_query_posts(cache_key) if with_detail else []
        if not posts:
            if target_id:
                resp = await self.qzone.get_feeds(target_id, pos=pos, num=num)
                if not resp.ok:
                    raise RuntimeError(self._map_feed_error(resp, target_id=target_id))
                msglist = resp.data.get("msglist") or []
                if not msglist:
                    raise RuntimeError(f"QQ {target_id} 暂无可见说说")
                posts = QzoneParser.parse_feeds(msglist)
            else:
                resp = await self.qzone.get_recent_feeds()
                if not resp.ok:
                    raise RuntimeError(self._map_feed_error(resp))
                posts = QzoneParser.parse_recent_feeds(resp.data)[pos: pos + num]
                if not posts:
                    raise RuntimeError("动态流暂无可见说说")
            if with_detail:
                self._set_query_cache(cache_key, posts)

        if no_self:
            uin = await self.session.get_uin()
            posts = [p for p in posts if p.uin != uin]

        if with_detail:
            posts = await self._fill_post_detail(posts)
            if not posts:
                raise RuntimeError("获取详情后无有效说说")

        if no_commented:
            posts = await self._filter_not_commented(posts)

        return posts

    def _cache_enabled(self) -> bool:
        return int(self.cfg_get("feed_cache_max_size", 50) or 0) > 0 and int(
            self.cfg_get("feed_cache_ttl_seconds", 1800) or 0
        ) > 0

    def _cache_expired(self, ts: float) -> bool:
        return time.monotonic() - ts > int(self.cfg_get("feed_cache_ttl_seconds", 1800) or 0)

    @staticmethod
    def _query_cache_key(target_id: str | None, pos: int, num: int) -> tuple[str, int, int]:
        return (str(target_id or "__recent__"), int(pos), int(num))

    @staticmethod
    def _post_cache_key(post: Post) -> tuple[int, str] | None:
        if not post.tid:
            return None
        return (int(post.uin), str(post.tid))

    def _copy_post(self, post: Post) -> Post:
        if hasattr(post, "model_copy"):
            return post.model_copy(deep=True)
        return post.copy(deep=True)

    def _trim_cache(self) -> None:
        max_size = int(self.cfg_get("feed_cache_max_size", 50) or 0)
        while len(self._post_cache) > max_size:
            self._post_cache.popitem(last=False)
        while len(self._query_cache) > max_size:
            self._query_cache.popitem(last=False)

    def _get_cached_post(self, key: tuple[int, str]) -> Post | None:
        if not self._cache_enabled():
            return None
        entry = self._post_cache.get(key)
        if not entry:
            return None
        if self._cache_expired(entry.ts):
            self._post_cache.pop(key, None)
            return None
        self._post_cache.move_to_end(key)
        return self._copy_post(entry.post)

    def _set_post_cache(self, post: Post) -> None:
        if not self._cache_enabled():
            return
        key = self._post_cache_key(post)
        if not key:
            return
        self._post_cache[key] = _PostCacheEntry(self._copy_post(post), time.monotonic())
        self._post_cache.move_to_end(key)
        self._trim_cache()

    def _get_cached_query_posts(self, key: tuple[str, int, int]) -> list[Post]:
        if not self._cache_enabled():
            return []
        entry = self._query_cache.get(key)
        if not entry:
            return []
        if self._cache_expired(entry.ts):
            self._query_cache.pop(key, None)
            return []
        posts = []
        for post_key in entry.post_keys:
            post = self._get_cached_post(post_key)
            if not post:
                return []
            posts.append(post)
        self._query_cache.move_to_end(key)
        return posts

    def _set_query_cache(self, key: tuple[str, int, int], posts: list[Post]) -> None:
        if not self._cache_enabled():
            return
        post_keys = []
        for post in posts:
            post_key = self._post_cache_key(post)
            if post_key:
                post_keys.append(post_key)
                self._set_post_cache(post)
        if len(post_keys) != len(posts):
            return
        self._query_cache[key] = _QueryCacheEntry(post_keys, time.monotonic())
        self._query_cache.move_to_end(key)
        self._trim_cache()

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(k in text for k in keywords)

    def _map_feed_error(self, resp, *, target_id: str | None = None) -> str:
        message = str(resp.message or "").strip()
        lower_message = message.lower()
        code = resp.code
        http_status = self._extract_http_status(resp.raw)

        permission_keywords = (
            "无权限", "权限", "私密", "不可见", "拒绝访问", "受限", "forbidden",
            QZONE_MSG_PERMISSION_DENIED, "access denied",
        )
        login_keywords = ("登录", "失效", "skey", "g_tk", "cookie", "expired")

        if code == QZONE_CODE_LOGIN_EXPIRED or self._contains_any(
            lower_message, login_keywords
        ):
            return "登录状态失效，请重新登录后重试"

        if (
            code in (QZONE_CODE_PERMISSION_DENIED, QZONE_CODE_PERMISSION_DENIED_LEGACY)
            or http_status == HTTP_STATUS_FORBIDDEN
            or self._contains_any(lower_message, permission_keywords)
        ):
            if target_id:
                return f"无权限查看 QQ {target_id} 的说说"
            return "无权限访问动态流"

        if code == QZONE_CODE_UNKNOWN and message == QZONE_MSG_EMPTY_RESPONSE:
            if target_id:
                return f"无权限查看 QQ {target_id} 的说说（接口返回空响应）"
            return "动态接口返回空响应，请稍后重试"

        if code == QZONE_CODE_UNKNOWN and message in (
            QZONE_MSG_INVALID_RESPONSE,
            QZONE_MSG_JSON_PARSE_ERROR,
            QZONE_MSG_NON_OBJECT_RESPONSE,
        ):
            return "接口响应格式异常，请稍后重试"

        if message:
            return f"查询说说失败：{message}"
        return f"查询说说失败：code={code}"

    @staticmethod
    def _extract_http_status(raw: dict[str, Any]) -> int | None:
        meta = raw.get(QZONE_INTERNAL_META_KEY)
        if not isinstance(meta, dict):
            return None
        status = meta.get(QZONE_INTERNAL_HTTP_STATUS_KEY)
        return status if isinstance(status, int) else None

    @staticmethod
    def _has_comment_from_uin(post: Post, uin: int) -> bool:
        return any(comment.uin == uin for comment in post.comments)

    async def _fill_post_detail(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []
        for post in posts:
            resp = await self.qzone.get_detail(post)
            if not resp.ok or not resp.data:
                logger.warning(f"获取详情失败：{resp.data}")
                continue
            parsed = QzoneParser.parse_feeds([resp.data])
            if not parsed:
                logger.warning(f"解析详情失败：{resp.data}")
                continue
            detailed = parsed[0]
            key = self._post_cache_key(detailed)
            cached = self._get_cached_post(key) if key else None
            if cached:
                cached.comments = detailed.comments
                self._set_post_cache(cached)
                result.append(cached)
                continue
            self._set_post_cache(detailed)
            result.append(detailed)
        return result

    async def _filter_not_commented(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []
        uin = await self.session.get_uin()
        for post in posts:
            if self._has_comment_from_uin(post, uin):
                continue
            if not post.comments:
                resp = await self.qzone.get_detail(post)
                if not resp.ok or not resp.data:
                    continue
                parsed = QzoneParser.parse_feeds([resp.data])
                if not parsed:
                    continue
                post = parsed[0]
            if self._has_comment_from_uin(post, uin):
                continue
            result.append(post)
        return result

    async def publish_post(
        self,
        *,
        post: Post | None = None,
        text: str | None = None,
        images: list | None = None,
    ) -> Post:
        if post is None and not text and not images:
            raise ValueError("post、text、images 不能同时为空")
        if post is None:
            uin = await self.session.get_uin()
            name = await self.session.get_nickname()
            post = Post(uin=uin, name=name, text=text or "", images=images or [])
        resp = await self.qzone.publish(post)
        if not resp.ok:
            raise RuntimeError(f"发布说说失败：{resp.data}")
        post.tid = resp.data.get("tid")
        post.status = "approved"
        post.create_time = resp.data.get("now", post.create_time)
        return post

    async def like_post(self, post: Post):
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        resp = await self.qzone.like(post)
        if not resp.ok:
            raise RuntimeError(f"点赞失败：{resp.message}")
        self._set_post_cache(post)

    async def delete_post(self, post: Post):
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        resp = await self.qzone.delete(post.tid)
        if not resp.ok:
            raise RuntimeError(f"删除说说失败：{resp.message}")
        key = self._post_cache_key(post)
        if not key:
            logger.debug(f"删除说说后跳过缓存清理：无效 cache key, tid={post.tid}")
            return
        self._post_cache.pop(key, None)
        for query_key in list(self._query_cache.keys()):
            entry = self._query_cache.get(query_key)
            if not entry:
                continue
            if key in entry.post_keys:
                self._query_cache.pop(query_key, None)

    async def comment_posts(self, post: Post, content: str):
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        content = (content or "").strip()
        if not content:
            raise ValueError("评论内容为空")
        await self.qzone.comment(post, content)
        uin = await self.session.get_uin()
        name = await self.session.get_nickname()
        post.comments.append(
            Comment(
                uin=uin,
                nickname=name,
                content=content,
                create_time=int(time.time()),
                tid=0,
                parent_tid=None,
            )
        )
        self._set_post_cache(post)

    async def reply_comment(self, post: Post, index: int, content: str):
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        comments = post.replyable_comments()
        n = len(comments)
        if n == 0:
            raise ValueError("没有可回复的评论")
        if not (-n <= index < n):
            raise ValueError(f"索引越界, 当前共有 {n} 条评论")
        comment = comments[index]
        if not getattr(comment, "tid", 0):
            raise ValueError("该评论缺少 tid，可能是本地临时记录，无法回复；请重新获取详情后再试")
        content = (content or "").strip()
        if not content:
            raise ValueError("回复内容为空")
        resp = await self.qzone.reply(post, comment, content)
        if not resp.ok:
            raise RuntimeError(resp.message)
        uin = await self.session.get_uin()
        name = await self.session.get_nickname()
        post.comments.append(
            Comment(
                uin=uin,
                nickname=name,
                content=content,
                create_time=int(time.time()),
                parent_tid=comment.tid,
            )
        )
        self._set_post_cache(post)
