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

    # ── @我(与我相关)列表 ─────────────────────────────────────
    # 数据源: 网页版「与我相关」页签 = feeds2_html_pav_all + 通知参数。
    # 实测(2026-09, 抓包验证): getappnotification=1&getnotifi=1 时返回
    # 与我相关条目(赞/评论/访问/被@), JSONP _Callback 包装, data.data 为条目数组。

    ATME_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds2_html_pav_all"

    async def get_atme_list(self, *, offset: int = 0, count: int = 10) -> dict[str, Any]:
        """拉取「与我相关」列表, 返回解析后的原始 dict(含 data.data 条目数组)。

        条目种类(实测): appid=217 赞/评论我的说说, 403 访问我的主页,
        被@ 时条目文本含 "@昵称" 标记。失败抛 RuntimeError。
        """
        raw = await self.request(
            "GET",
            self.ATME_URL,
            params=lambda ctx: {
                "uin": ctx.uin,
                "begin_time": 0,
                "end_time": 0,
                "getappnotification": 1,
                "getnotifi": 1,
                "has_get_key": 0,
                "offset": offset,
                "set": 0,
                "count": count,
                "useutf8": 1,
                "outputhtmlfeed": 1,
                "scope": 1,
                "g_tk": ctx.gtk2,
            },
        )
        resp = ApiResponse.from_raw(raw)
        if not resp.ok:
            raise RuntimeError(f"「与我相关」接口失败: {resp.message}(code={resp.code})")
        return resp.data


def parse_atme_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析「与我相关」响应 → 归一化条目列表。

    data 为 get_atme_list 返回的 dict(其 data.data 是条目数组, 条目 html 为
    渲染片段)。归一化字段:
      uin/nickname — 互动者; content — 条目纯文本(动作+内容预览);
      time — abstime; post_uin/post_tid — 从 .../{uin}/mood/{tid} 链接提取
      (无 mood 链接的条目如"访问主页"为 None)。
    """
    inner = data.get("data")
    if isinstance(inner, dict):
        inner = inner.get("data")
    if not isinstance(inner, list):
        return []

    result: list[dict[str, Any]] = []
    for it in inner:
        if not isinstance(it, dict):
            continue
        html = str(it.get("html") or "")
        # 动作文本 + 内容预览(去标签 + HTML 实体转义)
        import re as _re
        import html as _html
        plain = _html.unescape(_re.sub(r"<[^>]+>", " ", html))
        plain = _re.sub(r"\s+", " ", plain).strip()
        # 动作分类(实测文案): "提到我"=正文@, "评论提到我"=评论中@,
        # 其余(赞/评论/回复/访问)不是被@。先匹配长词防子串误判。
        if "评论提到我" in plain or "回复提到我" in plain:
            action = "comment_mention"
        elif "提到我" in plain:
            action = "mention"
        else:
            action = "other"
        # 从链接提取说说归属(第一个 /mood/ 链接)。
        # 实测: 被@条目的 mood 链接 tid 以 "." 结尾(如 .../mood/fde859...0100.),
        # 点赞条目带评论锚点(如 .../mood/fde859...0300.1); get_detail 只认
        # 无后缀的基础 tid, 因此截去第一个 "." 起的锚点后缀。
        post_uin = post_tid = None
        m = _re.search(r"qq\.com/(\d+)/mood/([0-9a-zA-Z.]+)", html)
        if m:
            post_uin = m.group(1)
            post_tid = m.group(2).split(".", 1)[0]
        result.append({
            "uin": it.get("uin"),
            "nickname": it.get("nickname"),
            "content": plain,
            "time": it.get("abstime") or 0,
            "appid": it.get("appid"),
            "action": action,
            "post_uin": post_uin,
            "post_tid": post_tid,
        })
    return result
