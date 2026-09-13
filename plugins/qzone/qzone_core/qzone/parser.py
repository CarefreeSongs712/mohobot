"""QQ 空间响应解析器（移植自 astrbot_plugin_qzone_lite）。

json5 为可选依赖：缺失时降级为 json.loads（响应中 undefined→null 已预处理，
QQ 空间接口实际返回的基本是合法 JSON）。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from ..model import Comment, Post
from .constants import (
    QZONE_CODE_UNKNOWN,
    QZONE_MSG_EMPTY_RESPONSE,
    QZONE_MSG_INVALID_RESPONSE,
    QZONE_MSG_JSON_PARSE_ERROR,
    QZONE_MSG_NON_OBJECT_RESPONSE,
)

try:
    import json5

    def _json_loads(text: str):
        return json5.loads(text)
except ImportError:  # pragma: no cover - 取决于环境是否安装 json5
    def _json_loads(text: str):
        return json.loads(text)


class QzoneParser:
    @staticmethod
    def _error_payload(message: str) -> dict[str, Any]:
        return {"code": QZONE_CODE_UNKNOWN, "message": message, "data": {}}

    @staticmethod
    def _extract_json_object_from(text: str, start: int) -> str | None:
        depth = 0
        in_string = False
        quote = ""
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                    continue
                if ch == quote:
                    in_string = False
                continue

            if ch in ("'", '"'):
                in_string = True
                quote = ch
                continue
            if ch == "{":
                depth += 1
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start: idx + 1]
        return None

    @staticmethod
    def _iter_json_objects(text: str):
        for start, ch in enumerate(text):
            if ch != "{":
                continue
            json_str = QzoneParser._extract_json_object_from(text, start)
            if json_str is not None:
                yield json_str

    @staticmethod
    def parse_response(text: str, *, debug: bool = False) -> dict[str, Any]:
        if debug:
            logger.debug(f"响应数据: {text}")
        if not text or not text.strip():
            logger.warning("响应内容为空")
            return QzoneParser._error_payload(QZONE_MSG_EMPTY_RESPONSE)

        parse_error: Exception | None = None
        for json_str in QzoneParser._iter_json_objects(text):
            json_str = json_str.replace("undefined", "null").strip()
            try:
                data = _json_loads(json_str)
            except (ValueError, json.JSONDecodeError) as e:
                parse_error = e
                continue

            if not isinstance(data, dict):
                logger.error("JSON 解析结果不是 dict")
                return QzoneParser._error_payload(QZONE_MSG_NON_OBJECT_RESPONSE)
            return data

        if parse_error is None:
            logger.warning("响应内容缺少 JSON 片段")
            return QzoneParser._error_payload(QZONE_MSG_INVALID_RESPONSE)

        logger.error(f"JSON 解析错误: {parse_error}")
        return QzoneParser._error_payload(QZONE_MSG_JSON_PARSE_ERROR)

    @staticmethod
    def parse_upload_result(payload: dict[str, Any]) -> tuple[str, str]:
        data = payload["data"]
        picbo = data["url"].split("&bo=", 1)[1]
        richval = ",{},{},{},{},{},{},,{},{}".format(
            data["albumid"],
            data["lloc"],
            data["sloc"],
            data["type"],
            data["height"],
            data["width"],
            data["height"],
            data["width"],
        )
        return picbo, richval

    @staticmethod
    def parse_feeds(msglist: list[dict]) -> list[Post]:
        """解析说说列表接口(msglist)。"""
        try:
            posts = []
            for msg in msglist:
                image_urls = []
                for img_data in msg.get("pic", []):
                    for key in ("url2", "url3", "url1", "smallurl"):
                        if raw := img_data.get(key):
                            image_urls.append(raw)
                            break
                for video in msg.get("video") or []:
                    video_image_url = video.get("url1") or video.get("pic_url")
                    if video_image_url:
                        image_urls.append(video_image_url)
                video_urls = []
                for video in msg.get("video") or []:
                    url = video.get("url3")
                    if url:
                        video_urls.append(url)
                rt_con = msg.get("rt_con", {}).get("content", "")
                comments = Comment.build_list(msg.get("commentlist") or [])
                post = Post(
                    tid=str(msg.get("tid", 0)),
                    uin=int(msg.get("uin", 0)),
                    name=msg.get("name", ""),
                    gin=0,
                    text=str(msg.get("content", "")).strip(),
                    images=image_urls,
                    videos=video_urls,
                    anon=False,
                    status="approved",
                    create_time=int(msg.get("created_time", 0) or 0),
                    rt_con=rt_con,
                    comments=comments,
                    extra_text=msg.get("source_name"),
                )
                posts.append(post)
            return posts
        except Exception as e:
            logger.error(f"解析说说列表失败: {e}")
            return []

    @staticmethod
    def parse_recent_feeds(data: dict) -> list[Post]:
        """解析动态流接口(HTML 片段, 需 bs4)。"""
        try:
            import bs4
        except ImportError:
            logger.error("beautifulsoup4 未安装, 无法解析动态流")
            return []

        feeds: list = data.get("data", {}).get("data", {}) or []
        try:
            posts = []
            for feed in feeds:
                if not feed:
                    continue
                if str(feed.get("appid", "")) != "311":
                    continue
                uin = feed.get("uin", "")
                tid = feed.get("key", "")
                if not uin or not tid:
                    continue
                create_time = feed.get("abstime", 0)
                nickname = feed.get("nickname", "")
                html_content = feed.get("html", "")
                if not html_content:
                    continue

                soup = bs4.BeautifulSoup(html_content, "html.parser")

                text_div = soup.find("div", class_="f-info")
                text = text_div.get_text(strip=True) if text_div else ""

                rt_con = ""
                txt_box = soup.select_one("div.txt-box")
                if txt_box:
                    rt_con = txt_box.get_text(strip=True)
                    if "：" in rt_con:
                        rt_con = rt_con.split("：", 1)[1].strip()

                image_urls = []
                if img_box := soup.find("div", class_="img-box"):
                    for img in img_box.find_all("img"):
                        src = img.get("src")
                        if src and not str(src).startswith("http://qzonestyle.gtimg.cn"):
                            image_urls.append(src)

                img_tag = soup.select_one("div.video-img img")
                if img_tag and "src" in img_tag.attrs:
                    image_urls.append(img_tag["src"])

                videos = []
                video_div = soup.select_one("div.img-box.f-video-wrap.play")
                if video_div and "url3" in video_div.attrs:
                    videos.append(video_div["url3"])

                comments: list[Comment] = []
                comment_items = soup.select("li.comments-item.bor3")
                for item in comment_items:
                    data_uin = str(item.get("data-uin", ""))
                    comment_tid = str(item.get("data-tid", ""))
                    nick = str(item.get("data-nick", ""))

                    content_div = item.select_one("div.comments-content")
                    if content_div:
                        for op in content_div.select("div.comments-op"):
                            op.decompose()
                        content = content_div.get_text(" ", strip=True).split(":", 1)[-1]
                    else:
                        content = ""

                    comment_time_span = item.select_one("span.state")
                    comment_time = comment_time_span.get_text(strip=True) if comment_time_span else ""

                    parent_tid = None
                    parent_div = item.find_parent("div", class_="mod-comments-sub")
                    if parent_div:
                        parent_li = parent_div.find_parent("li", class_="comments-item")
                        if parent_li:
                            parent_tid = str(parent_li.get("data-tid"))

                    comments.append(
                        Comment(
                            uin=int(data_uin) if data_uin.isdigit() else 0,
                            nickname=nick,
                            content=content,
                            create_time=0,
                            create_time_str=comment_time,
                            tid=int(comment_tid) if comment_tid.isdigit() else 0,
                            parent_tid=int(parent_tid) if parent_tid and parent_tid.isdigit() else None,
                        )
                    )

                post = Post(
                    tid=str(tid),
                    uin=int(uin),
                    name=str(nickname),
                    text=text,
                    images=list(set(image_urls)),
                    videos=videos,
                    create_time=int(create_time) if str(create_time).isdigit() else 0,
                    rt_con=rt_con,
                    comments=comments,
                )
                posts.append(post)
            logger.info(f"成功解析 {len(posts)} 条最新说说")
            return posts
        except Exception as e:
            logger.error(f"解析说说错误：{e}")
            return []
