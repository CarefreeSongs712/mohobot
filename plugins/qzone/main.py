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

import asyncio
import random
from typing import Any

from loguru import logger

from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent
from mohobot.utils.cq_code import extract_image_urls, extract_plain_text

from qzone_core.auto_publish import AutoPublishStore, build_daily_plan
from qzone_core import (
    AutoReplyStore,
    LitePostService,
    Post,
    QzoneAPI,
    QzoneSession,
    clean_reply_text,
    content_key,
    render_reply_prompt,
)
from qzone_core.qzone.parser import QzoneParser
from qzone_core.qzone.api import parse_atme_items
from qzone_core.utils import (
    extract_at_ids,
    parse_comment_args,
    parse_range,
    parse_reply_args,
)

# 群聊长文本(>=600 字)改用合并转发, 与框架 _send_reply 阈值一致
_FORWARD_MIN_LEN = 600

# 自动回复提示词默认模板({nick}/{content}/{post} 占位符)
_DEFAULT_REPLY_PROMPT = (
    "你的QQ好友 {nick} 在QQ空间里与你互动：\"{content}\"\n"
    "相关说说内容：\"{post}\"\n"
    "请以 bot 的口吻写一条自然、口语化的回复。"
)


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
        "/与我相关",
        "/发日常", "/发布记录",
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
            {"name": "与我相关", "desc": "查看「与我相关」接口原始响应(管理员/私聊, 调试用)", "admin": True},
            {"name": "发日常", "desc": "立即主动发一条说说(管理员/私聊, 测试用)", "admin": True},
            {"name": "发布记录", "desc": "查看主动发布计划与历史(管理员/私聊)", "admin": True},
        ],
    }

    # 框架注入(类级, 热重载后自动重新注入)
    _ws_server = None
    _admin_ids: list[str] = []
    _llm_service = None
    _emotion_manager = None
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
        # ── 自动回复(被@ / 自己说说被评论) ──
        "comment_reply_enabled": False,     # 自己说说收到新评论 → LLM 回复
        "atme_reply_enabled": False,        # 被 @ → LLM 回复
        "atme_mode": "api",                 # api(与我相关接口, 开箱即用) | feeds_scan(动态流扫 @昵称)
        "atme_api_url": "",                 # 高级覆盖: 留空用内置「与我相关」接口
        "atme_match_keyword": "@",          # api 模式条目匹配词(被@条目文本含 "@昵称")
        "atme_keyword": "",                 # feeds_scan 匹配词, 空=自动用 "@{bot昵称}"
        "scan_interval_sec": 300,           # 轮询间隔(秒)
        "scan_count": 10,                   # 每轮扫描自己最新说说条数
        "max_comment_age_sec": 86400,       # 只回复该时间内的评论(0=不限), 防止补回复积压旧评论
        "max_replies_per_post": 10,         # 同一条说说下本 bot 自动回复上限(0=不限)
        "auto_reply_max_length": 60,        # 生成回复最大长度
        "auto_reply_prompt": _DEFAULT_REPLY_PROMPT,
        "auto_reply_fallback": "谢谢你的互动~",  # LLM 失败时的兜底文案({nick} 可用)
        # ── 主动发说说 ──
        "auto_publish_enabled": False,
        "auto_publish_daily_count": 2,      # 每天条数(0=不发)
        "auto_publish_window_start": "09:00",
        "auto_publish_window_end": "22:30",
        "auto_publish_min_gap_sec": 7200,   # 相邻两条最小间隔
        "auto_publish_topic_ttl_sec": 43200,  # 互动话题 12 小时过期
        "auto_publish_topics": [],          # 静态话题池(可叠加, 兜底素材)
        "auto_publish_max_length": 120,
        "auto_publish_exclude_bots": [],    # 排除的 bot_id / QQ
        "auto_publish_notify_admin": False,
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
    _COMMANDS["与我相关"] = ("atme_debug", True)
    _COMMANDS["发日常"] = ("publish_daily", True)
    _COMMANDS["发布记录"] = ("publish_log", True)

    def __init__(self):
        self.plugin_config: dict = dict(self._DEFAULTS)
        # per-bot 实例: {bot_id: QzoneAPI / LitePostService}
        self._apis: dict[str, QzoneAPI] = {}
        self._services: dict[str, LitePostService] = {}
        # 自动回复: per-bot 去重存储 + 登录/请求失败退避(到点前跳过该 bot)
        self._auto_stores: dict[str, AutoReplyStore] = {}
        self._auto_fail_until: dict[str, float] = {}
        # 封禁名单存储(自动回复跳过被封用户; 与封禁系统共用 data/ban JSON)
        self._ban_store = None
        # 主动发说说: per-bot 计划/历史/话题池 + 失败退避
        self._pub_stores: dict[str, AutoPublishStore] = {}
        self._pub_fail_until: dict[str, float] = {}

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
    def inject_emotion_manager(cls, emotion_manager) -> None:
        """框架注入 EmotionManager(主动发说说的话题取材)。"""
        cls._emotion_manager = emotion_manager

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

    async def _cmd_atme_debug(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        """调试: 调「与我相关」接口并返回解析后的条目(验证登录态与识别效果)。"""
        try:
            api, _ = self._get_api(bot_id)
            import json as _json
            items = parse_atme_items(await api.get_atme_list())
            preview = [
                {k: it.get(k) for k in ("nickname", "content", "post_uin", "post_tid", "time")}
                for it in items[:10]
            ]
        except Exception as e:
            return (True, f"「与我相关」接口调用失败: {e}")
        return (True, f"「与我相关」共 {len(items)} 条, 前 10 条:\n{_json.dumps(preview, ensure_ascii=False, indent=1)}")

    # ══ 自动回复(被@ / 自己说说被评论) ══════════════════════════

    @property
    def interval_sec(self) -> int:
        """框架周期任务间隔(每轮循环前重读, 配置热更新即时生效)。"""
        try:
            return max(60, int(self._cfg("scan_interval_sec", 300)))
        except (TypeError, ValueError):
            return 300

    def _get_auto_store(self, bot_id: str) -> AutoReplyStore:
        store = self._auto_stores.get(bot_id)
        if store is None:
            from pathlib import Path
            store = AutoReplyStore(
                Path(self._data_dir) / "plugins_data" / "qzone" / f"auto_reply_{bot_id}.json",
            )
            self._auto_stores[bot_id] = store
        return store

    @staticmethod
    def _comment_key(post: Post, comment) -> str:
        """评论去重 key(tid 缺失时用 时间+内容哈希 兜底)。"""
        fallback = comment.tid or content_key(
            comment.create_time, comment.content[:50],
        )
        return f"selfc:{post.uin}:{post.tid}:{comment.uin}:{fallback}"

    @staticmethod
    def _atme_feeds_candidates(posts: list[Post], keyword: str, self_uin: int,
                               bot_qqs: set[int] | None = None):
        """从动态流(好友说说)中提取 @bot 候选: 正文含关键词或评论含关键词。

        返回 [(post, comment_or_None, key)]; 关键词形如 "@昵称"。
        自己/其它 bot 的说说与评论一律跳过(bot 之间不互动)。
        """
        bots = bot_qqs if bot_qqs is not None else set()
        out: list[tuple[Post, object | None, str]] = []
        for post in posts:
            if int(post.uin) == int(self_uin):
                continue  # 自己的说说交给评论监听处理
            if int(post.uin) in bots:
                continue  # 其它 bot 的说说不处理
            if keyword in post.text or keyword in post.rt_con:
                out.append((post, None, f"atme:{post.uin}:{post.tid}"))
            for idx, c in enumerate(post.comments):
                if keyword not in c.content or int(c.uin) == int(self_uin):
                    continue
                if int(c.uin) in bots:
                    continue
                out.append((post, idx, f"atme:{post.uin}:{post.tid}"))
        return out

    async def on_tick(self) -> None:
        """周期任务: 逐 bot 轮询「自己说说被评论」「被@」与「主动发说说计划」。"""
        publish_on = bool(self._cfg("auto_publish_enabled", False))
        if not (bool(self._cfg("comment_reply_enabled", False))
                or bool(self._cfg("atme_reply_enabled", False))
                or publish_on):
            return
        ws = self._ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        if bm is None:
            return
        import time as _time
        for bot in list(bm.all_bots):
            bot_id = bot.bot_id
            # 自动回复(独立退避)
            until = self._auto_fail_until.get(bot_id, 0.0)
            if not (until and _time.monotonic() < until):
                try:
                    await self._auto_reply_once(bot_id)
                except Exception as e:
                    # 登录失效/网络异常等: 退避 10 分钟再试, 避免每轮刷日志
                    self._auto_fail_until[bot_id] = _time.monotonic() + 600
                    logger.warning(f"[qzone][{bot_id}] 自动回复轮询失败, 10 分钟后重试: {e}")
            # 主动发说说(独立退避, 与回复互不影响)
            if publish_on:
                until = self._pub_fail_until.get(bot_id, 0.0)
                if until and _time.monotonic() < until:
                    continue
                try:
                    await self._auto_publish_tick(bot_id)
                except Exception as e:
                    self._pub_fail_until[bot_id] = _time.monotonic() + 600
                    logger.warning(f"[qzone][{bot_id}] 主动发说说失败, 10 分钟后重试: {e}")

    async def _auto_reply_once(self, bot_id: str) -> None:
        comment_on = bool(self._cfg("comment_reply_enabled", False))
        atme_on = bool(self._cfg("atme_reply_enabled", False))
        if not (comment_on or atme_on):
            return
        api, service = self._get_api(bot_id)
        self_uin = await api.session.get_uin()
        if comment_on:
            await self._monitor_own_comments(bot_id, api, service, self_uin)
        if atme_on:
            await self._monitor_atme(bot_id, api, service, self_uin)

    # ── 自己说说被评论 → 回复 ─────────────────────────────────

    async def _monitor_own_comments(self, bot_id: str, api, service, self_uin: int) -> None:
        store = self._get_auto_store(bot_id)
        scan_count = max(1, min(20, int(self._cfg("scan_count", 10))))
        posts = await service.query_feeds(
            target_id=str(self_uin), pos=0, num=scan_count, with_detail=False,
        )
        if not posts:
            return
        if not await store.is_baselined():
            # 首启基线: 现存评论全部标记已读, 不回复历史
            for post in posts:
                for comment in post.comments:
                    await store.mark_seen(self._comment_key(post, comment))
            await store.set_baselined()
            await store.save()
            logger.info(f"[qzone][{bot_id}] 评论自动回复基线建立: {len(posts)} 条说说")
            return
        max_age = max(0, int(self._cfg("max_comment_age_sec", 86400)))
        import time as _time
        for post in posts:
            if await self._post_limit_reached(store, post.uin, post.tid):
                continue
            for idx, comment in enumerate(post.comments):
                if int(comment.uin) == int(self_uin):
                    continue
                blocked, why = await self._actor_blocked(comment.uin)
                if blocked:
                    await store.mark_seen(self._comment_key(post, comment))
                    await store.save()
                    logger.debug(f"[qzone][{bot_id}] 跳过评论({why}): {comment.uin}")
                    continue
                key = self._comment_key(post, comment)
                if await store.is_seen(key):
                    continue
                await store.mark_seen(key)
                await store.save()
                if (max_age and comment.create_time
                        and _time.time() - comment.create_time > max_age):
                    continue
                text = await self._generate_auto_text(
                    bot_id, comment.nickname, comment.plain_content, post,
                )
                try:
                    if comment.tid:
                        await service.reply_comment(post, idx, text)
                    else:
                        await service.comment_posts(post, text)
                    await store.incr_post_reply(self._post_key(post.uin, post.tid))
                    await store.save()
                    logger.info(
                        f"[qzone][{bot_id}] 自动回复评论: {post.tid} ← {comment.nickname}: {text}"
                    )
                except Exception as e:
                    logger.error(f"[qzone][{bot_id}] 自动回复评论失败: {e}")
                await asyncio.sleep(random.uniform(1.5, 3.0))

    # ── 被@ → 回复(双模式) ────────────────────────────────────

    async def _monitor_atme(self, bot_id: str, api, service, self_uin: int) -> None:
        mode = str(self._cfg("atme_mode", "feeds_scan"))
        if mode == "api":
            await self._monitor_atme_api(bot_id, api, service, self_uin)
        else:
            await self._monitor_atme_feeds(bot_id, api, service, self_uin)

    async def _monitor_atme_feeds(self, bot_id: str, api, service, self_uin: int) -> None:
        """动态流扫描模式: 好友动态正文/评论里出现 @bot昵称 即视为被@。

        去重按 (post_uin, post_tid): 同一条说说只处理一次(与 api 模式共用 key)。
        """
        keyword = str(self._cfg("atme_keyword", "") or "").strip()
        if not keyword:
            keyword = f"@{self._bot_nickname(bot_id)}"
        resp = await api.get_recent_feeds()
        posts = QzoneParser.parse_recent_feeds(resp.data)
        candidates = self._atme_feeds_candidates(
            posts, keyword, self_uin, self._bot_qq_set(),
        )
        if not candidates:
            return
        store = self._get_auto_store(bot_id)
        seen_posts: set[str] = set()
        for post, comment_or_idx, key in candidates:
            if key in seen_posts:
                continue  # 同一条说说的多个候选只处理第一个
            seen_posts.add(key)
            if await store.is_seen(key):
                continue
            await store.mark_seen(key)
            await store.save()
            if await self._post_limit_reached(store, post.uin, post.tid):
                continue
            blocked, why = await self._actor_blocked(
                post.comments[int(comment_or_idx)].uin if comment_or_idx is not None else post.uin,
            )
            if blocked:
                logger.debug(f"[qzone][{bot_id}] 跳过被@条目({why})")
                continue
            if comment_or_idx is None:
                # 说说正文里被 @ → 评论该说说
                content = post.text or post.rt_con
                text = await self._generate_auto_text(bot_id, post.name, content, post)
                try:
                    await service.comment_posts(post, text)
                    await store.incr_post_reply(self._post_key(post.uin, post.tid))
                    await store.save()
                    logger.info(f"[qzone][{bot_id}] 自动回复被@(正文): {post.tid} ← {text}")
                except Exception as e:
                    logger.error(f"[qzone][{bot_id}] 自动回复被@(正文)失败: {e}")
            else:
                idx = int(comment_or_idx)
                if idx >= len(post.comments):
                    continue
                comment = post.comments[idx]
                text = await self._generate_auto_text(
                    bot_id, comment.nickname, comment.plain_content, post,
                )
                try:
                    if comment.tid:
                        await service.reply_comment(post, idx, text)
                    else:
                        await service.comment_posts(post, text)
                    await store.incr_post_reply(self._post_key(post.uin, post.tid))
                    await store.save()
                    logger.info(
                        f"[qzone][{bot_id}] 自动回复被@(评论): {post.tid} ← {comment.nickname}: {text}"
                    )
                except Exception as e:
                    logger.error(f"[qzone][{bot_id}] 自动回复被@(评论)失败: {e}")
            await asyncio.sleep(random.uniform(1.5, 3.0))

    async def _monitor_atme_api(self, bot_id: str, api, service, self_uin: int) -> None:
        """「与我相关」接口模式(实测 feeds2_html_pav_all + 通知参数)。

        只处理被@动作: "提到我"(正文@) → 评论该说说;
        "评论提到我"(评论中@) → 回评那条评论(定位该用户最新一条 @bot 评论);
        其余动作(赞/评论/回复/访问)一律忽略 —— 评论我的说说由评论监听负责。
        去重按 (post_uin, post_tid): 同一条说说上的多次 @/动态只处理一次。
        """
        raw_items = await api.get_atme_list()
        items = parse_atme_items(raw_items)
        if not items:
            return
        keyword = str(self._cfg("atme_match_keyword", "@") or "").strip()
        bot_qqs = self._bot_qq_set()
        store = self._get_auto_store(bot_id)
        for item in items:
            action = item.get("action")
            if action not in ("mention", "comment_mention"):
                continue  # 赞/评论/回复/访问等不是被@
            post_uin = item.get("post_uin")
            post_tid = item.get("post_tid")
            if not post_uin or not post_tid:
                continue  # 无说说归属(访问主页等)
            if str(post_uin) == str(self_uin):
                continue  # 自己的说说 → 评论监听负责
            if int(post_uin) in bot_qqs:
                continue  # 其它 bot 的说说不处理
            content = str(item.get("content") or "")
            if keyword and keyword not in content:
                continue
            blocked, why = await self._actor_blocked(item.get("uin"))
            if blocked:
                logger.debug(f"[qzone][{bot_id}] 跳过被@条目({why}): {item.get('uin')}")
                continue
            key = f"atme:{post_uin}:{post_tid}"
            if await store.is_seen(key):
                continue  # 该说说已处理过(一次唤醒只回一次)
            await store.mark_seen(key)
            await store.save()
            if await self._post_limit_reached(store, post_uin, post_tid):
                logger.debug(f"[qzone][{bot_id}] 说说回复达上限, 跳过: {post_uin}/{post_tid}")
                continue
            post = await self._fetch_post_detail(api, str(post_uin), str(post_tid))
            if post is None:
                continue
            nick = str(item.get("nickname") or item.get("uin") or "好友")
            text = await self._generate_auto_text(bot_id, nick, content, post)
            try:
                if action == "comment_mention":
                    idx = self._find_mention_comment(
                        post, item.get("uin"), self._bot_nickname(bot_id),
                    )
                else:
                    idx = None
                if idx is not None and post.comments[idx].tid:
                    await service.reply_comment(post, idx, text)
                    target = f"回评#{idx}"
                else:
                    await service.comment_posts(post, text)
                    target = "评论说说"
                await store.incr_post_reply(self._post_key(post_uin, post_tid))
                await store.save()
                logger.info(
                    f"[qzone][{bot_id}] 自动回复被@(api/{target}): {post_uin}/{post_tid} ← {text}"
                )
            except Exception as e:
                logger.error(f"[qzone][{bot_id}] 自动回复被@(api)失败: {e}")
            await asyncio.sleep(random.uniform(1.5, 3.0))

    @staticmethod
    def _find_mention_comment(post: Post, actor_uin, bot_nickname: str) -> int | None:
        """定位该用户在说说评论中最新一条 @bot 的评论(倒序找)。

        匹配条件: 评论内容含 "@" 或 bot 昵称; 找不到返回 None(退化为评论说说)。
        """
        nick = (bot_nickname or "").strip()
        for idx in range(len(post.comments) - 1, -1, -1):
            c = post.comments[idx]
            if int(c.uin) != int(actor_uin):
                continue
            content = c.content or ""
            if "@" in content or (nick and nick in content):
                return idx
        return None

    async def _fetch_post_detail(self, api, post_uin: str, post_tid: str) -> Post | None:
        """按 (uin, tid) 拉取说说详情(含评论); 失败返回 None。"""
        try:
            stub = Post(uin=int(post_uin), tid=str(post_tid))
            resp = await api.get_detail(stub)
            parsed = QzoneParser.parse_feeds([resp.data]) if resp.ok and resp.data else []
            return parsed[0] if parsed else None
        except Exception as e:
            logger.warning(f"[qzone] 拉取说说详情失败({post_uin}/{post_tid}): {e}")
            return None

    # ── LLM 回复生成 ──────────────────────────────────────────

    async def _generate_auto_text(
        self, bot_id: str, nick: str, content: str, post: Post,
    ) -> str:
        """LLM 生成回复(带 bot 人设 system); 失败降级为固定文案。"""
        max_len = max(10, int(self._cfg("auto_reply_max_length", 60)))
        fallback_tpl = str(self._cfg("auto_reply_fallback", "谢谢你的互动~"))
        llm = self._llm_service
        if llm is None:
            return clean_reply_text(fallback_tpl.replace("{nick}", nick), max_len)
        post_text = (post.text or post.rt_con or "")[:200]
        prompt = render_reply_prompt(
            str(self._cfg("auto_reply_prompt", _DEFAULT_REPLY_PROMPT)),
            nick=nick, content=content, post=post_text,
        )
        system = (
            f"你是QQ空间用户「{self._bot_nickname(bot_id)}」。"
            f"{self._bot_persona(bot_id)}\n"
            f"直接输出回复内容, 不要任何前缀、引号或解释, 不超过 {max_len} 字, 口语化。"
        )
        raw = await llm.complete_text(
            prompt, system_prompt=system, max_tokens=max_len * 2 + 64,
            temperature=0.8, module="qzone",
        )
        text = clean_reply_text(raw, max_len)
        return text or clean_reply_text(fallback_tpl.replace("{nick}", nick), max_len)

    def _bot_persona(self, bot_id: str) -> str:
        ws = self._ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        inst = bm.get(bot_id) if bm is not None else None
        persona = (getattr(inst.config, "persona", "") or "").strip() if inst is not None else ""
        return persona

    # ── 自动回复过滤(bot/封禁/每说说上限) ─────────────────────

    def _bot_qq_set(self) -> set[int]:
        """全部 bot(含自己)的 QQ 集合 — 自动回复跳过 bot 之间互动。"""
        ws = self._ws_server
        bm = getattr(ws, "_bot_manager", None) if ws is not None else None
        if bm is None:
            return set()
        return {int(b.qq) for b in bm.all_bots if b.qq}

    def _get_ban_store(self):
        if self._ban_store is None:
            from mohobot.ban import BanStore
            self._ban_store = BanStore(data_dir=self._data_dir)
        return self._ban_store

    async def _actor_blocked(self, uin) -> tuple[bool, str]:
        """互动者是否应跳过: 自己/其它 bot, 或封禁名单(私聊会话+全局)。"""
        try:
            uid = int(uin)
        except (TypeError, ValueError):
            return (False, "")
        if uid in self._bot_qq_set():
            return (True, "bot账号")
        store = self._get_ban_store()
        banned, reason = await store.is_banned(f"private:{uid}", str(uid))
        if banned:
            return (True, f"封禁名单({reason or '无理由'})")
        return (False, "")

    def _post_key(self, post_uin, post_tid) -> str:
        return f"{post_uin}:{post_tid}"

    async def _post_limit_reached(self, store: AutoReplyStore, post_uin, post_tid) -> bool:
        """该说说下本 bot 的自动回复是否已达上限(0=不限)。"""
        max_replies = max(0, int(self._cfg("max_replies_per_post", 10)))
        if max_replies <= 0:
            return False
        return await store.post_reply_count(self._post_key(post_uin, post_tid)) >= max_replies

    # ══ 主动发说说 ══════════════════════════════════════════════

    def _get_pub_store(self, bot_id: str) -> AutoPublishStore:
        store = self._pub_stores.get(bot_id)
        if store is None:
            from pathlib import Path
            store = AutoPublishStore(
                Path(self._data_dir) / "plugins_data" / "qzone" / f"auto_publish_{bot_id}.json",
            )
            self._pub_stores[bot_id] = store
        return store

    def _bot_excluded_from_publish(self, bot_id: str) -> bool:
        """排除名单(bot_id 或 QQ)命中则该 bot 不主动发说说。"""
        exclude = {
            str(x).strip() for x in (self._cfg("auto_publish_exclude_bots", []) or [])
            if str(x).strip()
        }
        if not exclude:
            return False
        return bot_id in exclude or self._bot_qq(bot_id) in exclude

    async def _auto_publish_tick(self, bot_id: str) -> None:
        """检查当日计划: 到点的未完成条目触发发布。"""
        if not bool(self._cfg("auto_publish_enabled", False)):
            return
        if self._bot_excluded_from_publish(bot_id):
            return
        count = max(0, int(self._cfg("auto_publish_daily_count", 2)))
        if count <= 0:
            return
        from mohobot.utils.time_utils import format_utc8
        store = self._get_pub_store(bot_id)
        today = format_utc8("%Y-%m-%d")
        created = await store.ensure_plan(today, build_daily_plan(
            count,
            str(self._cfg("auto_publish_window_start", "09:00")),
            str(self._cfg("auto_publish_window_end", "22:30")),
            int(self._cfg("auto_publish_min_gap_sec", 7200)),
        ))
        if created:
            await store.save()
            logger.info(f"[qzone][{bot_id}] 主动发布计划已生成: {store.plan().get('times')}")
        due = await store.due_indices(format_utc8("%H:%M"))
        if not due:
            return
        api, service = self._get_api(bot_id)
        for i in due:
            await store.mark_done(i)
            await store.save()
            try:
                await self._auto_publish_once(bot_id, api, service)
            except Exception as e:
                logger.error(f"[qzone][{bot_id}] 主动发说说失败(计划 {i}): {e}")
            await asyncio.sleep(random.uniform(2.0, 5.0))

    async def _auto_publish_once(self, bot_id: str, api, service) -> Post:
        """生成并发布一条说说; 返回 Post(失败抛异常)。"""
        topic = await self._pick_topic(bot_id)
        text = await self._generate_publish_text(bot_id, topic)
        post = await service.publish_post(text=text)
        from mohobot.utils.time_utils import format_utc8
        store = self._get_pub_store(bot_id)
        await store.add_history({
            "time": format_utc8("%Y-%m-%d %H:%M"),
            "text": text,
            "tid": post.tid,
        })
        await store.save()
        logger.info(f"[qzone][{bot_id}] 主动发布成功(tid={post.tid}): {text[:60]}")
        if bool(self._cfg("auto_publish_notify_admin", False)):
            await self._notify_admins(bot_id, f"【主动发布】{text[:100]}")
        return post

    async def _pick_topic(self, bot_id: str) -> str | None:
        """取话题: 互动话题池(12h TTL, 来自情感系统记忆) + 静态池; 空则 None(自由发挥)。"""
        store = self._get_pub_store(bot_id)
        ttl = max(0, int(self._cfg("auto_publish_topic_ttl_sec", 43200)))
        topics = store.valid_topics(ttl) if ttl > 0 else []
        if not topics:
            extracted = await self._extract_topics(bot_id)
            if extracted:
                await store.replace_topics(extracted)
                await store.save()
                topics = extracted
                logger.info(f"[qzone][{bot_id}] 话题池已更新: {topics}")
        topics = topics + [str(t).strip() for t in (self._cfg("auto_publish_topics", []) or [])
                           if str(t).strip()]
        return random.choice(topics) if topics else None

    async def _extract_topics(self, bot_id: str) -> list[str]:
        """从情感系统长期记忆提炼抽象话题词(不含任何具体内容)。

        情感系统未启用/无记忆/LLM 失败 → 空列表(回退自由发挥)。
        """
        emotion = self._emotion_manager
        llm = self._llm_service
        if emotion is None or llm is None:
            return []
        try:
            recs = emotion.recent_interactions(bot_id, 20)
        except Exception as e:
            logger.debug(f"[qzone][{bot_id}] 读取互动记忆失败: {e}")
            return []
        if not recs:
            return []
        lines = []
        for r in recs[:15]:
            user_msg = str(r.get("user_msg", "") or "")[:60]
            ai_msg = str(r.get("ai_response", "") or "")[:40]
            if user_msg:
                lines.append(f"用户: {user_msg} / bot: {ai_msg}")
        if not lines:
            return []
        prompt = (
            "下面是这个QQ号与用户的近期互动记录。请从中提炼 3~6 个可以公开谈论的"
            "抽象话题词(如: 月饼、考试、猫、天气)。\n"
            "严格要求: 只输出话题词本身, 不得包含任何用户昵称、QQ号、对话原话片段、"
            "可识别个人身份的信息或对话细节。\n"
            '以 JSON 数组输出, 如 ["月饼","开学","猫"]。\n\n'
            "互动记录:\n" + "\n".join(lines)
        )
        raw = await llm.complete_text(
            prompt, system_prompt="你是话题提炼助手, 只输出 JSON 数组。",
            max_tokens=200, temperature=0.3, module="qzone",
        )
        import json as _json
        import re as _re
        m = _re.search(r"\[[\s\S]*\]", raw or "")
        if not m:
            return []
        try:
            data = _json.loads(m.group(0))
        except _json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [str(t).strip()[:12] for t in data if str(t).strip()][:6]

    async def _generate_publish_text(self, bot_id: str, topic: str | None) -> str:
        """生成说说正文(LLM+人设; 隐私禁令写死; 失败重试 1 次)。"""
        max_len = max(20, int(self._cfg("auto_publish_max_length", 120)))
        persona = self._bot_persona(bot_id)
        system = (
            f"你是QQ空间用户「{self._bot_nickname(bot_id)}」。{persona}\n"
            f"你现在要发一条QQ空间说说。要求: 像真人的随手随想, 口语化, 自然;"
            f"只输出说说正文本身, 不超过 {max_len} 字;"
            f"不要引号、不要话题标签、不要 @任何人、不要自报身份。\n"
            f"绝对禁止: 提及任何用户昵称/QQ号/聊天原话/能识别出具体某个人的信息。"
            f"话题本身可以自然提到。"
        )
        from mohobot.utils.time_utils import format_utc8
        hour = int(format_utc8("%H"))
        period = ("清晨" if 5 <= hour < 9 else "上午" if 9 <= hour < 12
                  else "午后" if 12 <= hour < 14 else "下午" if 14 <= hour < 18
                  else "晚上" if 18 <= hour < 23 else "深夜")
        weekday = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[
            (int(format_utc8("%w")) + 6) % 7  # %w: 周日=0 → 转 周一=0
        ]
        time_hint = f"{period} {weekday}"
        store = self._get_pub_store(bot_id)
        recent = store.recent_texts(5)
        recent_str = "；".join(recent) if recent else "（暂无）"
        topic_str = f"围绕话题「{topic}」" if topic else "自由发挥一个日常小话题"
        prompt = (
            f"现在是{time_hint}。请{topic_str}写一条说说。\n"
            f"你最近发过的内容(避免重复): {recent_str}"
        )
        for attempt in range(2):
            if self._llm_service is None:
                raw = ""
            else:
                raw = await self._llm_service.complete_text(
                    prompt, system_prompt=system, max_tokens=max_len * 2 + 64,
                    temperature=0.9, module="qzone",
                )
            text = clean_reply_text(raw, max_len, keep_tail=True)
            if len(text) >= 2:
                return text
            logger.warning(f"[qzone][{bot_id}] 说说生成第 {attempt + 1} 次为空/过短")
        raise RuntimeError("说说生成失败(LLM 返回空)")

    async def _notify_admins(self, bot_id: str, text: str) -> None:
        ws = self._ws_server
        if ws is None:
            return
        for admin in self._admin_ids:
            if not admin.isdigit():
                continue
            try:
                await ws.send_private_msg(bot_id, int(admin), text)
            except Exception as e:
                logger.warning(f"[qzone][{bot_id}] 管理员通知失败({admin}): {e}")

    async def _cmd_publish_daily(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        """管理员立即主动发一条(测试用, 不占当日计划)。"""
        api, service = self._get_api(bot_id)
        post = await self._auto_publish_once(bot_id, api, service)
        return (True, f"已主动发布\n{post.to_str()}")

    async def _cmd_publish_log(self, bot_id: str, event, text: str) -> tuple[bool, str | None]:
        """查看当日计划与最近发布历史。"""
        store = self._get_pub_store(bot_id)
        plan = store.plan()
        lines = []
        times = plan.get("times") or []
        if times:
            done = plan.get("done") or []
            parts = [
                f"{t}{'✅' if i < len(done) and done[i] else '⬜'}"
                for i, t in enumerate(times)
            ]
            lines.append(f"今日计划({plan.get('date')}): " + " ".join(parts))
        else:
            lines.append("今日暂无发布计划(开关未开或条数为 0)")
        history = store._data.get("history") or []
        if history:
            lines.append("最近发布:")
            for h in reversed(history[-5:]):
                lines.append(f"  {h.get('time')} tid={h.get('tid')}: {str(h.get('text'))[:40]}")
        return (True, "\n".join(lines))
