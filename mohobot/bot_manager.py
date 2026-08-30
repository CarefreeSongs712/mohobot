"""Bot lifecycle management — bot_id 与 QQ 分离。

- bot_id: 内部标识(自动编号 bot_001...), 决定 data/bots/{bot_id}/ 目录
  与数据库 character_id。
- qq: bot 绑定的 QQ 号(一个 bot 只能绑定一个 QQ; QQ 唯一绑定 ——
  一个 QQ 只能被一个 bot 绑定, 换绑需先解绑)。
- 未绑定 QQ 的 WS 连接: 接受连接但不处理任何消息, 在面板显示"待绑定"。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.models.config import BotConfig


class BotInstance:
    """Represents a connected bot instance (bound) or unbound connection."""

    def __init__(
        self,
        bot_id: str,
        websocket: "websockets.WebSocketServerProtocol",
        config: BotConfig,
        bound: bool = True,
    ):
        self.bot_id: str = bot_id
        self.ws: "websockets.WebSocketServerProtocol" = websocket
        self.config: BotConfig = config
        self.bound: bool = bound
        # 在线时长统一用 wall clock(time.time): 与 WebUI 面板/status 插件的
        # time.time() - connected_at 计算一致, 避免 monotonic 混用导致时长显示异常
        self.connected_at: float = time.time()
        self.message_count: int = 0
        self._send_lock: asyncio.Lock = asyncio.Lock()
        # Track message IDs this bot has SENT, per chat: {"group:123": {"456", "789"}}
        self._sent_messages: dict[str, set[str]] = {}

    def record_sent_message(self, chat_type: str, chat_id: int | str, message_id: int | str) -> None:
        """Record a message ID the bot sent, so replies quoting it can be detected."""
        key = f"{chat_type}:{chat_id}"
        self._sent_messages.setdefault(key, set()).add(str(message_id))

    def is_my_message(self, chat_type: str, chat_id: int | str, message_id: int | str) -> bool:
        """Check if a given message_id was sent by this bot in the given chat."""
        key = f"{chat_type}:{chat_id}"
        return str(message_id) in self._sent_messages.get(key, set())

    async def send(self, data: dict[str, Any]) -> None:
        """Send a JSON message to this bot via WebSocket (thread-safe)."""
        async with self._send_lock:
            try:
                payload = json.dumps(data, ensure_ascii=False)
                await self.ws.send(payload)
            except Exception as e:
                logger.error(f"Failed to send to bot {self.bot_id}: {e}")
                raise

    async def call_api(self, action: str, params: dict[str, Any] | None = None,
                       echo: str | None = None) -> dict[str, Any]:
        """Send an API call and wait for the response."""
        request = {"action": action, "params": params or {}}
        if echo:
            request["echo"] = echo
        await self.send(request)
        return request  # Response will be handled via the message handler

    @property
    def qq(self) -> int:
        return self.config.qq

    @property
    def nickname(self) -> str:
        if self.config.nickname:
            return self.config.nickname
        if self.bound and self.bot_id:
            return f"Bot-{self.bot_id}"
        return f"QQ{self.config.qq}"


class BotManager:
    """Manages all connected bot instances and bot↔QQ bindings."""

    def __init__(self, data_dir: str = "./data"):
        self._bots: dict[str, BotInstance] = {}   # bot_id -> instance (已绑定)
        self._unbound: dict[str, BotInstance] = {}  # qq(str) -> instance (未绑定连接)
        self._data_dir = data_dir
        self._bots_dir = Path(data_dir) / "bots"
        # 按 bot_id 隔离的 pending 响应: bot_id -> {echo: future}
        # (多 bot 并发时, 无 echo 的错误响应只与该 bot 自己的 pending 匹配,
        #  不会误配到其它 bot 的等待者)
        self._pending_responses: dict[str, dict[str, asyncio.Future]] = {}
        # Track sent messages awaiting message_id: echo -> (bot_id, chat_type, chat_id)
        self._pending_sent: dict[str, tuple[str, str, str]] = {}
        # 群内在线 bot 集合(消息驱动): group_id(str) -> set[bot_id]
        # 用于全局指令去重: 群内多 bot 时只由 bot_id 最小者回复
        self._group_bots: dict[str, set[str]] = {}
        logger.info(f"BotManager initialized (data_dir={data_dir})")

    # ── Bot↔QQ 绑定与磁盘配置 ────────────────────────────────

    def _bot_config_path(self, bot_id: str) -> Path:
        return self._bots_dir / bot_id / "config.json"

    def next_bot_id(self) -> str:
        """生成下一个自动编号 bot_id (bot_001, bot_002, ...)。"""
        existing: list[int] = []
        if self._bots_dir.exists():
            for entry in self._bots_dir.iterdir():
                if not entry.is_dir():
                    continue
                m = re.fullmatch(r"bot_(\d+)", entry.name)
                if m:
                    existing.append(int(m.group(1)))
        n = max(existing, default=0) + 1
        return f"bot_{n:03d}"

    def find_bot_by_qq(self, qq: int | str) -> BotConfig | None:
        """扫描磁盘配置, 返回绑定了该 QQ 的 bot (QQ 唯一绑定)。"""
        qq_str = str(qq)
        if not self._bots_dir.exists():
            return None
        for entry in sorted(self._bots_dir.iterdir()):
            if not entry.is_dir():
                continue
            cfg = BotConfig.load(entry / "config.json")
            if cfg.bot_id and str(cfg.qq) == qq_str:
                return cfg
        return None

    def load_bot_config(self, bot_id: str) -> BotConfig:
        return BotConfig.load(self._bot_config_path(bot_id))

    def save_bot_config(self, bot_id: str) -> None:
        """Save the bot's config to disk."""
        instance = self._bots.get(bot_id)
        if instance:
            config_path = self._bot_config_path(bot_id)
            instance.config.save(config_path)
            logger.info(f"Bot config saved: {bot_id}")

    def list_bot_configs(self) -> list[BotConfig]:
        """扫描磁盘上的全部 bot 配置(含未绑定 QQ 的)。"""
        result: list[BotConfig] = []
        if not self._bots_dir.exists():
            return result
        for entry in sorted(self._bots_dir.iterdir()):
            if not entry.is_dir():
                continue
            cfg = BotConfig.load(entry / "config.json")
            if cfg.bot_id:
                result.append(cfg)
        return result

    # ── 创建 / 绑定 / 解绑 ───────────────────────────────────

    def create_bot(self, nickname: str = "", qq: int | str = 0) -> BotConfig:
        """面板手动创建新 bot (可选绑定 QQ)。QQ 唯一: 会解绑其他 bot。"""
        qq_int = int(qq or 0)
        bot_id = self.next_bot_id()
        cfg = BotConfig(bot_id=bot_id, nickname=nickname or bot_id, qq=qq_int)
        if qq_int:
            self._unbind_qq_from_others(qq_int, exclude_bot_id=bot_id)
        self._bots_dir.mkdir(parents=True, exist_ok=True)
        cfg.save(self._bot_config_path(bot_id))
        logger.info(f"Bot created: {bot_id} (nickname={cfg.nickname}, qq={qq_int or '未绑定'})")
        return cfg

    def bind_qq(self, bot_id: str, qq: int | str) -> bool:
        """把 QQ 绑定到指定 bot。QQ 唯一绑定: 自动解绑其他 bot。

        若该 QQ 有未绑定连接, 连接直接晋升为 bot 实例。
        """
        qq_int = int(qq or 0)
        cfg_path = self._bot_config_path(bot_id)
        cfg = BotConfig.load(cfg_path)
        if not cfg.bot_id:
            logger.warning(f"Bind failed: bot {bot_id} 不存在")
            return False

        old_qq = cfg.qq
        # QQ 唯一: 解绑其他 bot 对该 QQ 的绑定
        self._unbind_qq_from_others(qq_int, exclude_bot_id=bot_id)
        cfg.qq = qq_int
        cfg.save(cfg_path)

        # 连接迁移: 未绑定连接晋升 / 旧连接降级
        qq_str = str(qq_int)
        if qq_str in self._unbound:
            inst = self._unbound.pop(qq_str)
            inst.bound = True
            inst.bot_id = bot_id
            inst.config = cfg
            self._bots[bot_id] = inst
            logger.info(f"QQ {qq_int} 的未绑定连接已晋升为 {bot_id}")
        else:
            inst = self._bots.get(bot_id)
            if inst is not None:
                inst.config = cfg
                if old_qq and old_qq != qq_int:
                    # 旧 QQ 的连接不再属于本 bot → 降级为未绑定
                    self._bots.pop(bot_id, None)
                    inst.bound = False
                    inst.bot_id = ""
                    self._unbound[str(old_qq)] = inst
                    logger.info(f"{bot_id} 换绑: QQ{old_qq} 连接降级为未绑定")

        logger.info(f"Bot {bot_id} 绑定 QQ {qq_int}")
        return True

    def unbind_qq(self, bot_id: str) -> bool:
        """解绑 bot 的 QQ (qq 置 0)。若 bot 在线, 连接降级为未绑定。"""
        cfg_path = self._bot_config_path(bot_id)
        cfg = BotConfig.load(cfg_path)
        if not cfg.bot_id:
            return False
        old_qq = cfg.qq
        cfg.qq = 0
        cfg.save(cfg_path)

        inst = self._bots.get(bot_id)
        if inst is not None:
            self._bots.pop(bot_id, None)
            inst.bound = False
            inst.bot_id = ""
            inst.config = cfg
            if old_qq:
                self._unbound[str(old_qq)] = inst
            logger.info(f"{bot_id} 解绑 QQ{old_qq}, 连接降级为未绑定")

        logger.info(f"Bot {bot_id} 已解绑 QQ")
        return True

    def _unbind_qq_from_others(self, qq: int, exclude_bot_id: str) -> None:
        """QQ 唯一绑定: 把该 QQ 从其他 bot 上解绑。"""
        qq_str = str(qq)
        if not self._bots_dir.exists():
            return
        for entry in sorted(self._bots_dir.iterdir()):
            if not entry.is_dir():
                continue
            cfg = BotConfig.load(entry / "config.json")
            if cfg.bot_id and cfg.bot_id != exclude_bot_id and str(cfg.qq) == qq_str:
                cfg.qq = 0
                cfg.save(entry / "config.json")
                inst = self._bots.get(cfg.bot_id)
                if inst is not None:
                    self._bots.pop(cfg.bot_id, None)
                    inst.bound = False
                    inst.bot_id = ""
                    inst.config = cfg
                    self._unbound[qq_str] = inst
                logger.info(f"QQ 唯一绑定: {cfg.bot_id} 已被解绑 (QQ {qq} 转给 {exclude_bot_id})")

    # ── 旧格式迁移 ────────────────────────────────────────────

    def migrate_legacy_bots(self) -> int:
        """启动迁移: 旧版 data/bots/{qq}/config.json (无 bot_id) → bot_id 目录。

        为每个旧 bot 分配自动编号 bot_id 并写入 config, 目录改名;
        返回迁移数量。
        """
        if not self._bots_dir.exists():
            return 0
        migrated = 0
        for entry in sorted(self._bots_dir.iterdir()):
            if not entry.is_dir():
                continue
            cfg_path = entry / "config.json"
            cfg = BotConfig.load(cfg_path)
            if cfg.bot_id:
                continue  # 已是新格式

            if entry.name.isdigit():
                # 旧格式: 目录名即 QQ 号
                qq = int(entry.name)
                cfg.qq = qq
            cfg.bot_id = self.next_bot_id()
            if not cfg.nickname:
                cfg.nickname = cfg.bot_id
            cfg.save(cfg_path)

            new_dir = self._bots_dir / cfg.bot_id
            if new_dir != entry and not new_dir.exists():
                entry.rename(new_dir)
            migrated += 1
            logger.info(f"迁移旧 bot: QQ={cfg.qq or '?'} → {cfg.bot_id} (目录 {entry.name})")
        if migrated:
            logger.info(f"Legacy bot migration complete: {migrated} bot(s)")
        return migrated

    # ── 群内 bot 集合(全局指令去重) ──────────────────────────

    def note_group_message(self, bot_id: str, group_id: int | str) -> None:
        """记录 bot 在群内的存在(消息驱动: 收到群消息即在该群)。"""
        self._group_bots.setdefault(str(group_id), set()).add(bot_id)

    def min_bot_for_group(self, group_id: int | str) -> str | None:
        """该群在线 bot 中 bot_id 最小者(用于全局指令去重)。"""
        candidates = [
            b for b in self._group_bots.get(str(group_id), set())
            if b in self._bots
        ]
        return min(candidates) if candidates else None

    def bots_in_group(self, group_id: int | str) -> list[str]:
        """该群在线 bot 的 bot_id 列表(排序, 供合并回复按序收集)。"""
        return sorted(
            b for b in self._group_bots.get(str(group_id), set())
            if b in self._bots
        )

    def forget_bot_groups(self, bot_id: str) -> None:
        """bot 断开连接时从所有群记录移除。"""
        for gid in list(self._group_bots):
            self._group_bots[gid].discard(bot_id)
            if not self._group_bots[gid]:
                del self._group_bots[gid]

    # ── 连接注册 / 注销 ───────────────────────────────────────

    def register(self, qq: int | str, websocket: "websockets.WebSocketServerProtocol") -> BotInstance:
        """按 QQ 号注册连接: 已绑定 → bot 实例; 未绑定 → 未绑定连接(不处理消息)。"""
        qq_str = str(qq)
        bot_cfg = self.find_bot_by_qq(qq)

        if bot_cfg is not None:
            instance = BotInstance(bot_cfg.bot_id, websocket, bot_cfg, bound=True)
            self._bots[bot_cfg.bot_id] = instance
            self._unbound.pop(qq_str, None)
            logger.info(
                f"Bot {bot_cfg.bot_id} connected (QQ={bot_cfg.qq}, nickname={bot_cfg.nickname})"
            )
            return instance

        # 未绑定: 接受连接但不处理消息
        unbound_cfg = BotConfig(qq=int(qq), nickname=f"QQ{qq}")
        instance = BotInstance("", websocket, unbound_cfg, bound=False)
        self._unbound[qq_str] = instance
        logger.warning(
            f"QQ {qq} 已连接但未绑定任何 bot — 消息将被忽略, "
            f"请在 Web 面板创建 bot 并绑定该 QQ"
        )
        return instance

    def unregister(self, instance: BotInstance) -> None:
        """Remove a disconnected bot instance (bound or unbound).

        传实例而非 key, 防止旧连接断开误删同 QQ 的新实例(重连竞态)。
        """
        if instance.bound:
            if self._bots.get(instance.bot_id) is instance:
                del self._bots[instance.bot_id]
                self.forget_bot_groups(instance.bot_id)
                logger.info(f"Bot unregistered: {instance.bot_id}")
        else:
            if self._unbound.get(str(instance.qq)) is instance:
                del self._unbound[str(instance.qq)]
                logger.info(f"Unbound connection unregistered: QQ {instance.qq}")

    def get(self, bot_id: str) -> BotInstance | None:
        """Get a bound bot instance by bot_id."""
        return self._bots.get(bot_id)

    def get_by_qq(self, qq: int | str) -> BotInstance | None:
        """Get the bound bot instance connected via the given QQ."""
        qq_str = str(qq)
        for inst in self._bots.values():
            if str(inst.qq) == qq_str:
                return inst
        return None

    @property
    def all_bots(self) -> list[BotInstance]:
        return list(self._bots.values())

    @property
    def bot_count(self) -> int:
        return len(self._bots)

    @property
    def unbound_connections(self) -> list[BotInstance]:
        return list(self._unbound.values())

    # ── API 响应路由 ─────────────────────────────────────────

    async def handle_api_response(self, bot_id: str, response: dict[str, Any]) -> None:
        """Route an API response to the waiting caller, and record sent message IDs."""
        echo = response.get("echo")

        # Resolve any future waiting on this echo (per-bot pending)
        pending = self._pending_responses.get(bot_id)
        if echo and pending and echo in pending:
            future = pending.pop(echo)
            if not pending:
                self._pending_responses.pop(bot_id, None)
            if not future.done():
                future.set_result(response)
        elif not echo and pending and len(pending) == 1:
            # Some clients omit echo on error responses — if exactly one API
            # call is pending for THIS bot, match it (safe per-bot: one bot
            # may still await multiple parallel calls, but echo-less responses
            # only resolve when there's no ambiguity for that bot).
            key, future = next(iter(pending.items()))
            pending.pop(key)
            if not future.done():
                future.set_result(response)
        elif echo and str(echo).startswith("api_"):
            # 诊断: 查询类响应带 echo 但 pending 中无匹配(超时后到达/echo 被改)
            # (send: 前缀是发送类消息的响应, 只匹配 _pending_sent, 属正常)
            logger.debug(
                f"API response echo {echo!r} 无匹配 pending "
                f"(bot {bot_id}, pending={len(pending or {})}, "
                f"action 可能已超时)"
            )

        # Record message_id for a tracked sent message (reply-quote detection)
        if echo and echo in self._pending_sent:
            pending_bot, chat_type, chat_id = self._pending_sent.pop(echo)
            data = response.get("data") or {}
            mid = data.get("message_id")
            if mid is not None:
                instance = self._bots.get(pending_bot)
                if instance:
                    instance.record_sent_message(chat_type, chat_id, mid)

    def create_response_future(self, bot_id: str, echo: str) -> asyncio.Future:
        """Create a future for awaiting an API response (per-bot pending)."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses.setdefault(bot_id, {})[echo] = future
        return future

    def remove_response_future(self, bot_id: str, echo: str) -> None:
        """Remove a pending response future (e.g. after a timeout)."""
        pending = self._pending_responses.get(bot_id)
        if pending:
            pending.pop(echo, None)
            if not pending:
                self._pending_responses.pop(bot_id, None)

    def drop_pending_sent(self, echo: str) -> None:
        """Remove a tracked sent-message entry (e.g. after a send failure)."""
        self._pending_sent.pop(echo, None)
