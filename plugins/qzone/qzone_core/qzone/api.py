"""QQ 空间 API（移植自 astrbot_plugin_qzone_lite core/qzone/api.py）。"""

from __future__ import annotations

import base64
import re
import time
from typing import Any

from loguru import logger

from ..model import Comment, Post
from .client import QzoneHttpClient
from .model import ApiResponse
from .parser import QzoneParser
from .utils import normalize_images


class QzoneAPI(QzoneHttpClient):
    BASE_URL = "https://user.qzone.qq.com"
    UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
    EMOTION_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
    REPLY_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    DELETE_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
    DETAIL_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"

    async def _upload_image(self, image: bytes) -> ApiResponse:
        encoded = base64.b64encode(image).decode()
        raw = await self.request(
            "POST",
            self.UPLOAD_IMAGE_URL,
            data=lambda ctx: {
                "filename": "filename",
                "uploadtype": "1",
                "albumtype": "7",
                "skey": ctx.skey,
                "uin": ctx.uin,
                "p_skey": ctx.p_skey,
                "output_type": "json",
                "base64": "1",
                "picfile": encoded,
            },
            headers=lambda ctx: {
                "referer": f"{self.BASE_URL}/{ctx.uin}",
                "origin": self.BASE_URL,
            },
            timeout=60,
        )
        logger.debug(raw)
        return ApiResponse.from_raw(raw, code_key="ret", msg_key="msg")

    async def publish(self, post: Post) -> ApiResponse:
        data: dict[str, Any] = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": post.text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "code_version": "1",
            "format": "json",
        }
        if post.images:
            logger.debug(f"正在上传图片: {post.images}")
            pic_bos, richvals = [], []
            imgs: list[bytes] = await normalize_images(post.images)
            if not imgs:
                raise RuntimeError("未能读取任何待上传图片")
            for img in imgs:
                resp = await self._upload_image(img)
                if not resp.ok:
                    raise RuntimeError(f"上传图片失败: {resp.message}")
                picbo, richval = QzoneParser.parse_upload_result(resp.data)
                pic_bos.append(picbo)
                richvals.append(richval)
            data.update(pic_bo=",".join(pic_bos), richtype="1", richval="\t".join(richvals))

        raw = await self.request(
            "POST",
            self.EMOTION_URL,
            params=lambda ctx: {"g_tk": ctx.gtk2, "uin": ctx.uin},
            data=lambda ctx: {
                **data,
                "hostuin": ctx.uin,
                "qzreferrer": f"{self.BASE_URL}/{ctx.uin}",
            },
        )
        return ApiResponse.from_raw(raw)

    async def _get_qzonetoken(self) -> str:
        _, text = await self.request_text(
            "GET",
            lambda ctx: f"{self.BASE_URL}/{ctx.uin}",
            headers=lambda ctx: ctx.headers(),
            timeout=30,
        )
        if not text:
            logger.warning("获取 qzonetoken 失败：页面响应为空")
            return ""
        match = re.search(r'g_qzonetoken\s*=\s*"([^"]+)"', text)
        if match:
            return match.group(1)
        logger.warning(f"未能获取 qzonetoken，响应长度={len(text)}")
        return ""

    async def like(self, post: Post) -> ApiResponse:
        qzonetoken = await self._get_qzonetoken()
        mood_url = f"http://user.qzone.qq.com/{post.uin}/mood/{post.tid}"

        raw = await self.request(
            "POST",
            self.DOLIKE_URL,
            params=lambda ctx: {
                "g_tk": ctx.gtk2,
                **({"qzonetoken": qzonetoken} if qzonetoken else {}),
            },
            data=lambda ctx: {
                "qzreferrer": f"{self.BASE_URL}/{ctx.uin}",
                "opuin": ctx.uin,
                "unikey": mood_url,
                "curkey": mood_url,
                "appid": 311,
                "from": 1,
                "typeid": 0,
                "abstime": int(time.time()),
                "fid": post.tid,
                "active": 0,
                "format": "json",
                "fupdate": 1,
            },
        )
        return ApiResponse.from_raw(raw)

    async def comment(self, post: Post, content: str) -> ApiResponse:
        raw = await self.request(
            "POST",
            self.COMMENT_URL,
            params=lambda ctx: {"g_tk": ctx.gtk2},
            data=lambda ctx: {
                "topicId": f"{post.uin}_{post.tid}__1",
                "uin": ctx.uin,
                "hostUin": post.uin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": content,
            },
        )
        return ApiResponse.from_raw(raw)

    async def reply(self, post: Post, comment: Comment, content: str) -> ApiResponse:
        raw = await self.request(
            "POST",
            self.REPLY_URL,
            params=lambda ctx: {"g_tk": ctx.gtk2},
            data=lambda ctx: {
                "topicId": f"{post.uin}_{post.tid}__1",
                "uin": ctx.uin,
                "hostUin": post.uin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": content,
                "commentId": comment.tid,
                "commentUin": comment.uin,
                "richval": "",
                "richtype": "",
                "private": "0",
                "paramstr": "2",
                "qzreferrer": f"https://user.qzone.qq.com/{ctx.uin}/main",
            },
        )
        return ApiResponse.from_raw(raw)

    async def delete(self, tid: str) -> ApiResponse:
        raw = await self.request(
            "POST",
            self.DELETE_URL,
            params=lambda ctx: {"g_tk": ctx.gtk2},
            data=lambda ctx: {
                "uin": ctx.uin,
                "topicId": f"{ctx.uin}_{tid}__1",
                "feedsType": 0,
                "feedsFlag": 0,
                "feedsKey": tid,
                "feedsAppid": 311,
                "feedsTime": int(time.time()),
                "fupdate": 1,
                "ref": "feeds",
                "qzreferrer": "https://user.qzone.qq.com/",
            },
        )
        return ApiResponse.from_raw(raw)

    async def get_feeds(self, target_id: str, *, pos: int = 0, num: int = 1) -> ApiResponse:
        raw = await self.request(
            "GET",
            self.LIST_URL,
            params=lambda ctx: {
                "g_tk": ctx.gtk2,
                "uin": target_id,
                "ftype": 0,
                "sort": 0,
                "pos": pos,
                "num": num,
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "json",
                "need_comment": 1,
                "need_private_comment": 1,
            },
        )
        return ApiResponse.from_raw(raw)

    async def get_detail(self, post: Post) -> ApiResponse:
        raw = await self.request(
            "GET",
            self.DETAIL_URL,
            params=lambda ctx: {
                "uin": post.uin,
                "tid": post.tid,
                "format": "jsonp",
                "g_tk": ctx.gtk2,
            },
        )
        return ApiResponse.from_raw(raw)

    async def get_recent_feeds(self, page: int = 1) -> ApiResponse:
        raw = await self.request(
            "GET",
            self.ZONE_LIST_URL,
            params=lambda ctx: {
                "uin": ctx.uin,
                "scope": 0,
                "view": 1,
                "filter": "all",
                "flag": 1,
                "applist": "all",
                "pagenum": page,
                "aisortEndTime": 0,
                "aisortOffset": 0,
                "aisortBeginTime": 0,
                "begintime": 0,
                "format": "json",
                "g_tk": ctx.gtk2,
                "useutf8": 1,
                "outputhtmlfeed": 1,
            },
        )
        return ApiResponse.from_raw(raw)
