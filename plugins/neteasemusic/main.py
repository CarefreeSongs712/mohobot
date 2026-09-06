"""Mohobot 网易云点歌插件 — 迁移自 astrbot_plugin_netease_music (v2.0.0, NachoCrazy)。

功能:
- /点歌 <关键词> (别名 /music /听歌 /网易云) → 搜索展示编号列表 → 回复数字选歌
- 群内任意成员回复数字即可选歌(无需 @ bot); 60 秒内有效
- 选中后: 歌曲信息卡片(封面图 + 歌名/歌手/专辑/时长) + record 语音
  (NapCat 直接拉取 URL 转码; 发送失败降级为播放链接文本)
- 多 bot 群内: 点歌命令只由随机选中的一个 bot 回复(global_triggers 去重),
  数字选择由发起搜索的 bot 处理(会话状态按 bot 隔离)

明确不支持自然语言模糊匹配(如"来一首xxx")。
依赖: 自行部署的 NeteaseCloudMusicApi 服务(配置项 api_url)。
"""

from __future__ import annotations

import base64
import time
import urllib.parse
from typing import Any

from loguru import logger

try:
    import aiohttp
except ImportError:
    aiohttp = None

TRIGGERS = {"/点歌", "/music", "/听歌", "/网易云"}


class Plugin:
    """网易云点歌: /点歌 <关键词> → 列表 → 数字选择 → 播放。"""

    # 全局指令: 群内多 bot 时只由随机选中的一个 bot 回复(框架去重)
    global_triggers = TRIGGERS

    info = {
        "commands": [
            {"name": "点歌", "desc": "点歌 <关键词> — 搜索网易云歌曲,回复数字播放(别名: music/听歌/网易云)"},
        ],
    }

    # WS server injected by main.py via inject_ws_server() classmethod
    _ws_server = None
    _data_dir = "./data"

    _DEFAULTS = {
        "api_url": "http://127.0.0.1:3000",
        "quality": "exhigh",
        "search_limit": 5,
        "cookie": "",
    }

    def __init__(self):
        # 插件配置由框架注入(_conf_schema.json), 缺失时回退默认
        self.plugin_config: dict = dict(self._DEFAULTS)
        # 等待选歌的会话: {(bot_id, chat_type, chat_id): {"key": str, "expire": float}}
        self._waiting_users: dict[tuple, dict] = {}
        # 搜索结果缓存: cache_key -> [song dict, ...]
        self._song_cache: dict[str, list] = {}
        self._http_session: Any = None
        self._last_cleanup: float = 0.0

    # ── 框架注入 ─────────────────────────────────────────────

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    # ── 内部工具 ─────────────────────────────────────────────

    def _cfg(self, key: str, default):
        cfg = getattr(self, "plugin_config", None) or {}
        value = cfg.get(key, default)
        return value if value is not None and value != "" else default

    def _http(self) -> Any:
        """惰性创建 aiohttp 会话(20s 超时)。"""
        if aiohttp is None:
            raise RuntimeError("aiohttp 未安装, 无法访问网易云 API")
        if self._http_session is None or getattr(self._http_session, "closed", False):
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self._http_session

    async def on_shutdown(self) -> None:
        session = self._http_session
        self._http_session = None
        if session is not None and not getattr(session, "closed", False):
            await session.close()
        self._waiting_users.clear()
        self._song_cache.clear()

    @staticmethod
    def _extract_text(event: Any) -> str:
        if isinstance(event.message, str):
            return event.message.strip()
        text = ""
        if isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
        return text.strip()

    @staticmethod
    def _chat_of(event: Any) -> tuple[str, str]:
        """从事件取 (chat_type, chat_id)。"""
        from mohobot.models.onebot import GroupMessageEvent, PrivateMessageEvent
        if isinstance(event, GroupMessageEvent):
            return ("group", str(event.group_id))
        return ("private", str(event.user_id))

    def _cleanup_expired(self) -> None:
        """惰性清理过期会话与缓存(每 30s 最多跑一次, 避免字典膨胀)。"""
        now = time.time()
        if now - self._last_cleanup < 30:
            return
        self._last_cleanup = now
        expired = [k for k, s in self._waiting_users.items() if s["expire"] < now]
        for k in expired:
            ws = self._waiting_users.pop(k, None)
            if ws and ws["key"] in self._song_cache:
                del self._song_cache[ws["key"]]

    # ── 网易云 API ───────────────────────────────────────────

    async def _api_search(self, keyword: str, limit: int) -> list[dict]:
        url = (
            f"{self._cfg('api_url', 'http://127.0.0.1:3000').rstrip('/')}"
            f"/search?keywords={urllib.parse.quote(keyword)}&limit={limit}&type=1"
        )
        async with self._http().get(url) as r:
            r.raise_for_status()
            data = await r.json()
        return data.get("result", {}).get("songs", []) or []

    async def _api_song_detail(self, song_id: int) -> dict | None:
        url = (
            f"{self._cfg('api_url', 'http://127.0.0.1:3000').rstrip('/')}"
            f"/song/detail?ids={song_id}"
        )
        async with self._http().get(url) as r:
            r.raise_for_status()
            data = await r.json()
        songs = data.get("songs") or []
        return songs[0] if songs else None

    async def _api_audio_url(self, song_id: int, quality: str, cookie: str) -> str | None:
        """获取播放地址, 按音质自动回退(exhigh → higher → standard)。"""
        base = self._cfg("api_url", "http://127.0.0.1:3000").rstrip("/")
        for q in list(dict.fromkeys([quality, "exhigh", "higher", "standard"])):
            url = f"{base}/song/url/v1?id={song_id}&level={q}&cookie={urllib.parse.quote(cookie)}"
            async with self._http().get(url) as r:
                r.raise_for_status()
                data = await r.json()
            info = (data.get("data") or [{}])[0]
            if info.get("url"):
                return info["url"]
        return None

    async def _api_download_image(self, url: str) -> bytes | None:
        if not url:
            return None
        async with self._http().get(url) as r:
            if r.status == 200:
                return await r.read()
        return None

    # ── 消息发送 ─────────────────────────────────────────────

    async def _send_text(self, bot_id: str, event: Any, text: str) -> None:
        ws = self._ws_server
        if ws is None:
            return
        chat_type, chat_id = self._chat_of(event)
        if chat_type == "group":
            await ws.send_group_msg(bot_id, chat_id, text)
        else:
            await ws.send_private_msg(bot_id, chat_id, text)

    async def _send_card(self, bot_id: str, event: Any, text: str, cover_url: str) -> None:
        """发送"详情文本 + 封面图"同一条消息(text 段 + image 段)。

        封面下载/编码失败时仅发送文本段。
        """
        ws = self._ws_server
        if ws is None:
            return
        segments: list[dict] = [{"type": "text", "data": {"text": text}}]
        try:
            image_data = await self._api_download_image(cover_url)
            if image_data:
                b64 = base64.b64encode(image_data).decode()
                segments.append({"type": "image", "data": {"file": f"base64://{b64}"}})
        except Exception as e:
            logger.warning(f"下载歌曲封面失败, 仅发送文本: {e}")
        chat_type, chat_id = self._chat_of(event)
        if chat_type == "group":
            await ws.send_group_msg(bot_id, chat_id, segments)
        else:
            await ws.send_private_msg(bot_id, chat_id, segments)

    async def _send_record(self, bot_id: str, event: Any, audio_url: str) -> bool:
        """发送 record 语音(NapCat 拉取 URL); 失败返回 False(调用方降级为链接)。"""
        ws = self._ws_server
        if ws is None:
            return False
        try:
            chat_type, chat_id = self._chat_of(event)
            message: list[dict] = [{"type": "record", "data": {"file": audio_url}}]
            if chat_type == "group":
                await ws.send_group_msg(bot_id, chat_id, message)
            else:
                await ws.send_private_msg(bot_id, chat_id, message)
            return True
        except Exception as e:
            logger.warning(f"发送语音失败, 降级为链接: {e}")
            return False

    # ── 命令处理(拦截链, 群内去重后由选中 bot 执行) ─────────

    async def on_message(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """处理 /点歌 <关键词> 系列命令。"""
        text = self._extract_text(event)
        if not text:
            return (False, None)
        for trigger in TRIGGERS:
            if text == trigger:
                return (True, "请告诉我您想听什么歌喵~ 例如: /点歌 白鸟过河滩")
            if text.startswith(trigger + " "):
                keyword = text[len(trigger) + 1:].strip()
                if keyword:
                    return await self._search_and_show(bot_id, event, keyword)
                return (True, "请告诉我您想听什么歌喵~ 例如: /点歌 白鸟过河滩")
        return (False, None)

    # ── 数字选择(观察钩子, gate 前执行, 群内无需 @) ─────────

    async def on_message_observed(
        self,
        bot_id: str,
        event: Any,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """回复数字选歌: 纯数字 + 存在未过期的等待会话时消费并播放。"""
        self._cleanup_expired()
        text = self._extract_text(event)
        if not text.isdigit() or len(text) > 3:
            return (False, None)

        key = (bot_id, *self._chat_of(event))
        session = self._waiting_users.get(key)
        if session is None:
            return (False, None)  # 非点歌 bot / 无等待会话, 不消费
        if time.time() > session["expire"]:
            self._waiting_users.pop(key, None)
            if session["key"] in self._song_cache:
                del self._song_cache[session["key"]]
            return (False, None)

        # 消费: 先清掉等待会话, 防止重复事件导致重复播放
        del self._waiting_users[key]
        num = int(text)
        limit = int(self._cfg("search_limit", 5))
        if not (1 <= num <= limit):
            await self._send_text(bot_id, event, "您输入的数字不对哦,请选择列表里的歌曲编号喵~")
            return (True, None)
        await self._play_selected(bot_id, event, session["key"], num)
        return (True, None)

    # ── 核心逻辑 ─────────────────────────────────────────────

    async def _search_and_show(
        self, bot_id: str, event: Any, keyword: str,
    ) -> tuple[bool, str | None]:
        """搜索歌曲并展示编号列表, 返回列表文本。"""
        limit = int(self._cfg("search_limit", 5))
        try:
            songs = await self._api_search(keyword, limit)
        except Exception as e:
            logger.error(f"网易云 API 搜索失败: {e}")
            return (True, "呜喵...和音乐服务器的连接断掉了...请检查一下API服务是否正常运行")

        if not songs:
            return (True, f"没能找到「{keyword}」这首歌喵... T_T")

        cache_key = f"{bot_id}_{int(time.time() * 1000)}"
        self._song_cache[cache_key] = songs

        lines = [f"为您找到了 {len(songs)} 首歌曲喵！请回复数字告诉我您想听哪一首~"]
        for i, song in enumerate(songs, 1):
            artists = " / ".join(a.get("name", "") for a in song.get("artists", []) or [])
            album = (song.get("album") or {}).get("name", "未知专辑")
            duration_ms = song.get("duration", 0)
            dur_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
            lines.append(f"{i}. {song.get('name', '')} - {artists} 《{album}》 [{dur_str}]")

        # 设置等待会话(群内任意成员可回数字, 私聊仅本人)
        self._waiting_users[(bot_id, *self._chat_of(event))] = {
            "key": cache_key,
            "expire": time.time() + 60,
        }
        return (True, "\n".join(lines))

    async def _play_selected(
        self, bot_id: str, event: Any, cache_key: str, num: int,
    ) -> None:
        """播放选中歌曲: 详情文本 + 封面图 + record 语音(失败降级链接)。"""
        songs = self._song_cache.get(cache_key)
        if songs is None:
            await self._send_text(bot_id, event, "喵呜~ 选择得太久了,搜索结果已经凉掉了哦,请重新点歌吧~")
            return
        if not (1 <= num <= len(songs)):
            await self._send_text(bot_id, event, "您输入的数字不对哦,请选择列表里的歌曲编号喵~")
            return
        try:
            self._song_cache.pop(cache_key, None)
            selected = songs[num - 1]
            song_id = int(selected["id"])
            quality = self._cfg("quality", "exhigh")

            detail = await self._api_song_detail(song_id)
            if detail is None:
                await self._send_text(bot_id, event, "呜...获取歌曲信息的时候失败了喵...")
                return
            audio_url = await self._api_audio_url(
                song_id, quality, self._cfg("cookie", ""),
            )
            if not audio_url:
                await self._send_text(bot_id, event, "喵~ 这首歌可能需要VIP或者没有版权,暂时不能播放呢...")
                return

            title = detail.get("name", "")
            artists = " / ".join(a.get("name", "") for a in detail.get("ar", []) or [])
            album = (detail.get("al") or {}).get("name", "未知专辑")
            cover_url = (detail.get("al") or {}).get("picUrl", "")
            duration_ms = detail.get("dt", 0)
            dur_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"

            detail_text = (
                f"遵命,为您播放第 {num} 首歌曲~\n\n"
                f"♪ 歌名: {title}\n"
                f"🎤 歌手: {artists}\n"
                f"💿 专辑: {album}\n"
                f"⏳ 时长: {dur_str}\n"
                f"✨ 音质: {quality}\n\n"
                f"请主人享用喵~"
            )
            await self._send_card(bot_id, event, detail_text, cover_url)
            ok = await self._send_record(bot_id, event, audio_url)
            if not ok:
                await self._send_text(bot_id, event, f"🔊 点击播放: {audio_url}")
        except Exception as e:
            logger.error(f"网易云播放失败: {e}")
            await self._send_text(bot_id, event, "呜...获取歌曲信息的时候失败了喵...")
