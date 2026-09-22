"""Mohobot QQ空间说说插件（Lite）— 移植自 astrbot_plugin_qzone_lite (GPL-3.0)。

命令(仅 / 前缀):
  /看说说 [@QQ] [序号/范围]          查看(默认动态流, 带 @ 查指定用户)
  /发说说 <文本> [图片]              发布(管理员, 仅私聊)
  /删说说 [序号/范围]                删除自己的说说(管理员, 仅私聊)
  /评说说 [@QQ] [序号/范围] <内容>    评论
  /回评 [@QQ] [说说序号] [评论序号] <内容>
  /赞说说 [@QQ] [序号/范围]          点赞
  /重置QQCookies                    清空登录态缓存(管理员, 仅私聊)

mohobot 适配要点:
- 登录态 per-bot: 经该 bot 的 OneBot 连接 get_cookies 自动获取,
  缓存于 data/plugins_data/qzone/cookies_{bot_id}.json。
- 管理员 = 全局 admins(inject_admin_ids); 管理员命令群聊内一律静默忽略。
- 图片来源: 消息自带图 + 引用(回复)消息中的图(经 get_msg API)。
- 看说说图片分析(可选): 经 inject_llm_service 调框架视觉模型(带缓存)。
- 不迁移 LLM Tools。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent
from mohobot.utils.cq_code import extract_image_urls, extract_plain_text

from qzone_core import LitePostService, Post, QzoneAPI, QzoneSession
from qzone_core.utils import (
    extract_at_ids,
    parse_comment_args,
    parse_range,
    parse_reply_args,
)

# 群聊长文本(>=600 字)改用合并转发, 与框架 _send_reply 阈值一致
_FORWARD_MIN_LEN = 600


class Plugin:
    """QQ空间说说: 看/发/删/评/回评/赞(登录态 per-bot 自动获取)。"""

    # 多 bot 去重: 群内只由 bot_id 最小者回复(含别名)
    global_triggers = {
        "/看说说", "/查看说说",
        "/发说说",
        "/删说说", "/删除说说",
        "/评说说", "/评论说说", "/读说说",
        "/回评", "/回复评论",
        "/赞说说", "/点赞说说",
        "/重置QQCookies", "/重置cookies", "/重置qqcookies",
    }

    info = {
        "commands": [
            {"name": "看说说", "desc": "查看QQ空间说说: /看说说 [@QQ] [序号/范围]", "admin": True},
            {"name": "发说说", "desc": "发布说说(管理员/私聊): /发说说 <文本> [图片]", "admin": True},
            {"name": "删说说", "desc": "删除自己的说说(管理员/私聊): /删说说 [序号/范围]", "admin": True},
            {"name": "评说说", "desc": "评论说说: /评说说 [@QQ] [序号] <内容>", "admin": True},
            {"name": "回评", "desc": "回复评论: /回评 [@QQ] [说说序号] [评论序号] <内容>", "admin": True},
            {"name": "赞说说", "desc": "点赞说说: /赞说说 [@QQ] [序号/范围]", "admin": True},
            {"name": "重置QQCookies", "desc": "重置QQ空间登录态(管理员/私聊)", "admin": True},
        ],
    }

    # 框架注入(类级, 热重载后自动重新注入)
    _ws_server = None
    _admin_ids: list[str] = []
    _llm_service = None
    _data_dir = "./data"

    _DEFAULTS = {
        "auto_reset_on_login_expired": True,
        "send_feedback": True,
        "analyze_images_on_view_feed": False,
        "feed_cache_max_size": 50,
        "feed_cache_ttl_seconds": 1800,
        "timeout": 10,
        "request_interval": 0.8,
        "request_jitter": 0.6,
    }

    # 命令别名表(小写): -> (handler 名, 需管理员)
    _COMMANDS: dict[str, tuple[str, bool]] = {}
    for _alias in ("看说说", "查看说说"):
        _COMMANDS[_alias] = ("view", True)
    for _alias in ("发说说",):
        _COMMANDS[_alias] = ("publish", True)
    for _alias in ("删说说", "删除说说"):
        _COMMANDS[_alias] = ("delete", True)
    for _alias in ("评说说", "评论说说", "读说说"):
        _COMMANDS[_alias] = ("comment", True)
    for _alias in ("回评", "回复评论"):
        _COMMANDS[_alias] = ("reply", True)
    for _alias in ("赞说说", "点赞说说"):
        _COMMANDS[_alias] = ("like", True)
    for _alias in ("重置qqcookies", "重置cookies"):
        _COMMANDS[_alias] = ("reset_cookies", True)

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)
        # per-bot 实例: {bot_id: QzoneAPI / LitePostService}
        self._apis: dict[str, QzoneAPI] = {}
        self._services: dict[str, LitePostService] = {}

    # ── 框架注入 ──────────────────────────────────────────────

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_admin_ids(cls, admin_ids) -> None:
        cls._admin_ids = [str(a) for a in (admin_ids or [])]

    @classmethod
    def inject_llm_service(cls, llm_service) -> None:
        """框架注入 LLMService(看说说的图片分析用)。"""
        cls._llm_service = llm_service

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    # ── 配置 ──────────────────────────────────────────────────

    def _cfg(self, key: str, default):
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None and value != "" else default

    def _cfg_get(self):
        """供 qzone_core 使用的配置回调。"""
        return self._cfg

    # ── per-bot API 实例 ──────────────────────────────────────

    def _get_api(self, bot_id: str) -> tuple[QzoneAPI, LitePostService]:
        """取(或惰性创建)该 bot 的 QzoneAPI + Service。"""
        api = self._apis.get(bot_id)
        if api is None:
            session = QzoneSession(
                bot_id=bot_id,
                ws_server=self._ws_server,
                data_dir=self._data_dir,
                cfg_get=self._cfg_get(),
            )
            api = QzoneAPI(session, self._cfg_get())
            service = LitePostService(api, session, self._cfg_get())
            self._apis[bot_id] = api
            self._services[bot_id] = service
        return api, self._services[bot_id]

    async def on_shutdown(self) -> None:
        """关闭所有 per-bot HTTP 会话(热重载/停机时由框架调用)。"""
        for bot_id, api in list(self._apis.items()):
            try:
                await api.close()
            except Exception as e:
                logger.warning(f"[qzone] 关闭 {bot_id} HTTP 会话失败: {e}")
        self._apis.clear()

    # ── 工具 ──────────────────────────────────────────────────

    def _is_admin(self, event) -> bool:
        try:
            uid = str(event.user_id)
        except Exception:
            return False
        return uid in {a for a in self._admin_ids if a.isdigit()}

    def _bot_qq(self, bot_id: str) -> str:
        ws = self._ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        inst = bm.get(bot_id) if bm is not None else None
        return str(inst.qq) if inst is not None else ""

    def _bot_nickname(self, bot_id: str) -> str:
        ws = self._ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        inst = bm.get(bot_id) if bm is not None else None
        if inst is not None and (getattr(inst.config, "nickname", "") or "").strip():
            return inst.config.nickname.strip()
        return bot_id

    async def _send_text(self, bot_id: str, event, text: str) -> None:
        """发送反馈文本; 群聊超长自动改合并转发(与框架阈值一致)。"""
        ws = self._ws_server
        if ws is None or not text:
            return
        if isinstance(event, GroupMessageEvent) and len(text) >= _FORWARD_MIN_LEN:
            try:
                nodes = [{
                    "type": "node",
                    "data": {
                        "user_id": self._bot_qq(bot_id) or bot_id,
                        "nickname": self._bot_nickname(bot_id),
                        "content": [{"type": "text", "data": {"text": text}}],
                    },
                }]
                await ws.send_group_forward_msg(bot_id, event.group_id, nodes)
                return
            except Exception as e:
                logger.warning(f"[qzone] 合并转发失败, 退化为普通消息: {e}")
        if isinstance(event, GroupMessageEvent):
            await ws.send_group_msg(bot_id, event.group_id, text)
        else:
            await ws.send_private_msg(bot_id, event.user_id, text)

    async def _collect_images(self, bot_id: str, event) -> list[str]:
        """收集图片: 消息自带图 + 引用消息中的图(经 get_msg)。"""
        urls: list[str] = extract_image_urls(event.message)
        if isinstance(event.message, list):
            for seg in event.message:
                if not (isinstance(seg, dict) and seg.get("type") == "reply"):
                    continue
                mid = (seg.get("data") or {}).get("id")
                if mid is None:
                    continue
                ws = self._ws_server
                if ws is None:
                    continue
                try:
                    resp = await ws.send_to_bot(
                        bot_id, "get_msg", {"message_id": mid},
                        wait_response=True, timeout=8.0,
                    )
                    quoted = ((resp or {}).get("data") or {}).get("message")
                    urls.extend(extract_image_urls(quoted))
                except Exception as e:
                    logger.warning(f"[qzone] 获取引用消息图片失败: {e}")
        return list(dict.fromkeys(urls))

    async def _analyze_post_images(self, post: Post) -> None:
        """看说说的图片分析(可选, 经框架视觉模型, 自带 phash 缓存)。"""
        if not bool(self._cfg("analyze_images_on_view_feed", False)) or not post.images:
            return
        if post.extra_text:
            return
        llm = self._llm_service
        if llm is None:
            post.extra_text = "图片分析失败：视觉模型不可用"
            return
        descs: list[str] = []
        for idx, url in enumerate(post.images[:3]):  # 最多分析 3 张
            try:
                desc = await llm.describe_image(url)
                if desc:
                    descs.append(f"图片{idx + 1}：{desc}" if len(post.images) > 1 else desc)
            except Exception as e:
                logger.warning(f"[qzone] 图片分析失败: {e}")
        post.extra_text = "\n".join(descs) if descs else "图片分析失败"

    # ── 消息入口 ──────────────────────────────────────────────

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        text = extract_plain_text(event.message)
        if not text.startswith("/"):
            return (False, None)
        body = text[1:].strip()
        if not body:
            return (False, None)

        tokens = body.split()
        cmd = tokens[0].lower()
        # "/重置QQ cookies"(带空格)兼容: 取前两个 token 拼接
        if cmd == "重置qq" and len(tokens) > 1 and tokens[1].lower() == "cookies":
            cmd = "重置qqcookies"

        entry = self._COMMANDS.get(cmd)
        if entry is None:
            return (False, None)
        action, need_admin = entry

        # 管理员命令: 群聊内一律静默忽略(不处理, 不回复)
        if need_admin:
            if isinstance(event, GroupMessageEvent):
                return (True, None)
            if not self._is_admin(event):
                return (True, "该指令仅管理员可用, 且仅限私聊。")

        handler = getattr(self, f"_cmd_{action}")
        try:
            return await handler(bot_id, event, text)
        except Exception as e:
            logger.error(f"[qzone] /{cmd} 执行失败: {e}")
            return (True, str(e))

    # ── 命令实现 ──────────────────────────────────────────────

    async def _cmd_view(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        at_ids = extract_at_ids(event.message, self._bot_qq(bot_id))
        target_id = at_ids[0] if at_ids else None
        pos, num = parse_range(text)
        _, service = self._get_api(bot_id)
        posts = await service.query_feeds(target_id=target_id, pos=pos, num=num, with_detail=True)
        for post in posts:
            await self._analyze_post_images(post)
            await self._send_text(bot_id, event, post.to_str())
        return (True, None)

    async def _cmd_publish(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        body = text[1:].strip()
        parts = body.split(None, 1)
        content = parts[1].strip() if len(parts) > 1 else ""
        images = await self._collect_images(bot_id, event)
        if not content and not images:
            return (True, "请提供说说内容，例如：/发说说 今天天气不错")
        _, service = self._get_api(bot_id)
        post = await service.publish_post(text=content, images=images)
        if bool(self._cfg("send_feedback", True)):
            return (True, f"已发布\n{post.to_str()}")
        return (True, None)

    async def _cmd_delete(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        # 仅删除自己的说说: 以 bot 自身 QQ 为目标
        target_id = self._bot_qq(bot_id) or None
        pos, num = parse_range(text)
        _, service = self._get_api(bot_id)
        posts = await service.query_feeds(target_id=target_id, pos=pos, num=num, with_detail=False)
        if not posts:
            return (True, "没有找到要删除的说说")
        deleted = failed = 0
        for post in posts:
            try:
                await service.delete_post(post)
                deleted += 1
                if bool(self._cfg("send_feedback", True)):
                    await self._send_text(bot_id, event, f"已删除说说\n{post.to_str()}")
            except Exception as e:
                failed += 1
                await self._send_text(bot_id, event, str(e))
                logger.error(f"[qzone] 删除说说失败: {e}")
        return (True, f"删除完成：成功 {deleted} 条，失败 {failed} 条")

    async def _cmd_comment(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        at_ids = extract_at_ids(event.message, self._bot_qq(bot_id))
        target_id, pos, num, content = parse_comment_args(text, at_ids)
        if not content:
            return (True, "请在命令末尾提供评论内容，例如：/评说说 0 路过~")
        _, service = self._get_api(bot_id)
        posts = await service.query_feeds(target_id=target_id, pos=pos, num=num, with_detail=False)
        for post in posts:
            try:
                await service.comment_posts(post, content)
                if bool(self._cfg("send_feedback", True)):
                    await self._send_text(bot_id, event, f"已评论\n{post.to_str()}")
            except Exception as e:
                await self._send_text(bot_id, event, str(e))
                logger.error(f"[qzone] 评论失败: {e}")
        return (True, None)

    async def _cmd_reply(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        at_ids = extract_at_ids(event.message, self._bot_qq(bot_id))
        target_id, pos, comment_index, content = parse_reply_args(text, at_ids)
        if not content:
            return (True, "请提供回复内容，例如：/回评 0 -1 谢谢你的评论")
        _, service = self._get_api(bot_id)
        posts = await service.query_feeds(target_id=target_id, pos=pos, num=1, with_detail=True)
        if not posts:
            return (True, "查询结果为空")
        post = posts[0]
        await service.reply_comment(post, comment_index, content)
        if bool(self._cfg("send_feedback", True)):
            return (True, f"已回复评论\n{post.to_str()}")
        return (True, None)

    async def _cmd_like(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        at_ids = extract_at_ids(event.message, self._bot_qq(bot_id))
        target_id = at_ids[0] if at_ids else None
        pos, num = parse_range(text)
        _, service = self._get_api(bot_id)
        posts = await service.query_feeds(target_id=target_id, pos=pos, num=num, with_detail=False)
        for post in posts:
            try:
                await service.like_post(post)
                if bool(self._cfg("send_feedback", True)):
                    await self._send_text(bot_id, event, f"已点赞\n{post.to_str()}")
            except Exception as e:
                await self._send_text(bot_id, event, str(e))
                logger.error(f"[qzone] 点赞失败: {e}")
        return (True, None)

    async def _cmd_reset_cookies(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        api, _ = self._get_api(bot_id)
        await api.session.reset_login_state(clear_cookies=True)
        return (True, "QQ Cookies 已重置，下次需要时会重新获取")
