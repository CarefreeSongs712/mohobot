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
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import websockets  # 类型注解用(运行时只做注解, 不直接调用)

from loguru import logger

from mohobot.models.config import BotConfig


@dataclass
class _ConnectionConfig:
    config: BotConfig


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
        self._shared_config = _ConnectionConfig(config)
        self.bound: bool = bound
        self.client_role: str = "Universal"
        self.connection_qq: int = config.qq
        # 在线时长统一用 wall clock(time.time): 与 WebUI 面板/status 插件的
        # time.time() - connected_at 计算一致, 避免 monotonic 混用导致时长显示异常
        self.connected_at: float = time.time()
        self.message_count: int = 0
        # 连接代次: 同一 QQ 重连时递增, 旧连接被标记 stale 后不再派发事件/响应
        self.generation: int = 0
        self.stale: bool = False
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
        await self.send_guarded(data)

    async def send_guarded(
        self, data: dict[str, Any], dispatch_guard: Callable[[], bool] | None = None,
        *, on_dispatch: Callable[[], None] | None = None,
    ) -> bool:
        """Revalidate a queued operation under the final socket-send lock."""
        if self.stale or self.client_role == "Event":
            raise RuntimeError("connection cannot dispatch API calls")
        async with self._send_lock:
            if self.stale or self.client_role == "Event":
                raise RuntimeError("connection cannot dispatch API calls")
            if dispatch_guard is not None and not dispatch_guard():
                return False
            try:
                payload = json.dumps(data, ensure_ascii=False)
                if on_dispatch is not None:
                    on_dispatch()
                await self.ws.send(payload)
            except Exception as e:
                logger.error(f"Failed to send to bot {self.bot_id}: {e}")
                raise
        return True

    async def call_api(self, action: str, params: dict[str, Any] | None = None,
                       echo: str | None = None) -> dict[str, Any]:
        """Send an API call and wait for the response."""
        request = {"action": action, "params": params or {}}
        if echo:
            request["echo"] = echo
        await self.send(request)
        return request  # Response will be handled via the message handler

    @property
    def config(self) -> BotConfig:
        return self._shared_config.config

    @config.setter
    def config(self, value: BotConfig) -> None:
        # 面板/人设热更新仍可直接替换 config; Event/API 同步看到新对象。
        self._shared_config.config = value

    @property
    def qq(self) -> int:
        return self.connection_qq

    @property
    def nickname(self) -> str:
        if self.config.nickname:
            return self.config.nickname
        if self.bound and self.bot_id:
            return f"Bot-{self.bot_id}"
        return f"QQ{self.config.qq}"


class BotManager:
    """Manages all connected bot instances and bot↔QQ bindings."""

    # 每条消息随机选中结果的缓存时长(秒): 消息处理在秒级完成, 60s 绰绰有余
    _PICK_CACHE_TTL = 60.0
    # 全局指令跨账号认领的缓存时长(秒): 覆盖各账号依次推送同一条消息的窗口
    _COMMAND_CLAIM_TTL = 120.0

    def __init__(self, data_dir: str = "./data"):
        self._bots: dict[str, BotInstance] = {}   # bot_id -> instance (已绑定)
        self._unbound: dict[str, BotInstance] = {}  # qq(str) -> instance (未绑定连接)
        # OneBot 可将事件和 API 分别连接; 每个角色独立替换, 出站优先 API。
        self._connections: dict[str, dict[str, BotInstance]] = {}
        self._data_dir = data_dir
        self._bots_dir = Path(data_dir) / "bots"
        # 按 bot_id 隔离的 pending 响应: bot_id -> {echo: future}
        # (多 bot 并发时, 无 echo 的错误响应只与该 bot 自己的 pending 匹配,
        #  不会误配到其它 bot 的等待者)
        self._pending_responses: dict[str, dict[str, asyncio.Future]] = {}
        # Track sent messages awaiting message_id: echo -> (bot_id, chat_type, chat_id)
        self._pending_sent: dict[str, tuple[str, str, str]] = {}
        # 群内在线 bot 集合(消息驱动): group_id(str) -> set[bot_id]
        # 用于全局指令去重: 群内多 bot 时只由随机选中的一个 bot 回复
        self._group_bots: dict[str, set[str]] = {}
        # 每条消息的随机选中结果缓存: (group_id, message_id) -> (bot_id, 抽样时间)
        # 同一条消息经多 bot 连接各推送一次, 所有协程共享同一抽签结果
        self._group_pick_cache: dict[tuple[str, str], tuple[str, float]] = {}
        # 全局指令跨账号认领: (group_id, 稳定消息指纹) -> (owner_bot_id, 认领时间)
        # 各账号的 message_id 不同, 但同一物理消息的指纹相同 (#127)
        self._command_claims: dict[tuple[str, str], tuple[str, float]] = {}
        # 连接代次序号: 每次 register 新连接递增
        self._generation_seq: int = 0
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
            if not entry.is_dir() or not (entry / "config.json").is_file():
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
            if not entry.is_dir() or not (entry / "config.json").is_file():
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
        # 若创建时就填了 QQ, 必须把"已在线但未绑定"的连接晋升为本 bot,
        # 否则该 QQ 要断开重连后才生效(消息在此期间被忽略)。
        if qq_int:
            self.bind_qq(bot_id, qq_int)
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

        # 先降级旧 QQ 的所有角色, 再晋升目标 QQ, 避免覆盖 _bots 后留下旧绑定。
        inst = self._bots.get(bot_id)
        if inst is not None and old_qq and old_qq != qq_int:
            self._bots.pop(bot_id, None)
            inst.bound, inst.bot_id, inst.config = False, "", cfg
            self._sync_connection_binding(inst)
            self._unbound[str(inst.connection_qq)] = inst
            logger.info(f"{bot_id} 换绑: QQ{old_qq} 连接降级为未绑定")

        # 连接迁移: 未绑定连接晋升 / 原 QQ 更新配置
        qq_str = str(qq_int)
        if qq_str in self._unbound:
            inst = self._unbound.pop(qq_str)
            inst.bound = True
            inst.bot_id = bot_id
            inst.config = cfg
            self._sync_connection_binding(inst)
            self._bots[bot_id] = inst
            logger.info(f"QQ {qq_int} 的未绑定连接已晋升为 {bot_id}")
        else:
            inst = self._bots.get(bot_id)
            if inst is not None:
                inst.config = cfg
                self._sync_connection_binding(inst)

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
            self._sync_connection_binding(inst)
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
            if not entry.is_dir() or not (entry / "config.json").is_file():
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
                    self._sync_connection_binding(inst)
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

    def note_group_left(self, bot_id: str, group_id: int | str) -> None:
        """bot 已离开该群(被踢/主动退群): 从群集合剔除并失效抽签缓存。

        issue #9 第 4 点: 原实现只在群消息里累加, 被踢但连接仍在的 bot
        仍会被全局指令抽签选中, 导致命令无响应。
        """
        gid = str(group_id)
        bots = self._group_bots.get(gid)
        if bots is not None:
            bots.discard(bot_id)
            if not bots:
                self._group_bots.pop(gid, None)
        # 失效该群的 (群号, message_id) 抽签缓存, 避免继续返回已离群 bot
        for key in [k for k in self._group_pick_cache if str(k[0]) == gid]:
            self._group_pick_cache.pop(key, None)
        logger.info(f"Bot {bot_id} 已从群 {gid} 的在线集合移除")

    def _is_eligible(self, bot_id: str) -> bool:
        """bot 可作为群内回复候选: 已连接、非失效旧连接、配置仍启用。

        issue #127: 禁用但仍保持 WS 连接的 bot 仍在 _bots 中, 但入站事件会被
        _dispatch 丢弃。若抽签仍选中它, 该条消息就无人回复, 因此候选必须
        实时读取 enabled, 而不是连接时的快照。
        """
        instance = self._bots.get(bot_id)
        if instance is None or getattr(instance, "stale", False):
            return False
        role = getattr(instance, "client_role", "Universal")
        if role == "Event":
            return False  # Event 连接无法发送 API, 不能认领需要回复的全局指令。
        if (role == "API"
                and "Event" not in self._connections.get(str(instance.qq), {})):
            return False  # 只有 API 连接时无法收到群事件, 不能抢占全局指令。
        config = getattr(instance, "config", None)
        return bool(getattr(config, "enabled", True))

    def _eligible_candidates(self, group_id: int | str) -> list[str]:
        return [
            b for b in self._group_bots.get(str(group_id), set())
            if self._is_eligible(b)
        ]

    def claim_global_command(
        self, group_id: int | str, fingerprint: str,
    ) -> str | None:
        """跨账号原子认领一条全局指令消息, 返回该消息唯一 owner bot_id。

        同一条物理消息经 N 个 bot 连接推送时 message_id 各不相同, 因此调用方
        传入与账号无关的稳定指纹(发送者+时间+内容)。首次认领随机选出 owner,
        之后所有账号在 TTL 内得到同一 owner; owner 失效(离线/禁用/离开该群)
        时重新选择。返回 None 表示当前没有可用候选。
        """
        candidates = self._eligible_candidates(group_id)
        if not candidates:
            return None
        key = (str(group_id), str(fingerprint))
        now = time.time()
        cached = self._command_claims.get(key)
        if (
            cached
            and now - cached[1] < self._COMMAND_CLAIM_TTL
            and cached[0] in candidates
        ):
            # 认领命中时同步刷新抽签缓存, 保证后续按指纹 pick 也拿到同一 owner。
            self._group_pick_cache[key] = (cached[0], now)
            return cached[0]
        owner = self.pick_bot_for_group(group_id, str(fingerprint))
        if owner not in candidates:
            # pick 的缓存可能已被禁用/离线失效; 兜底重选后回写抽签缓存,
            # 否则同一指纹的后续调用会再次走到这里(每次重新随机)。
            owner = random.choice(candidates)
            self._group_pick_cache[key] = (owner, now)
        # 惰性清理过期认领, 防止无限增长
        if len(self._command_claims) > 2000:
            for k in [k for k, (_, ts) in self._command_claims.items()
                      if now - ts >= self._COMMAND_CLAIM_TTL]:
                del self._command_claims[k]
        self._command_claims[key] = (owner, now)
        return owner

    def pick_bot_for_group(
        self, group_id: int | str, message_id: int | str | None = None,
    ) -> str | None:
        """该群在线且启用的 bot 中随机选一个(用于全局指令去重)。

        同一条消息会经每个 bot 的连接各推送一次(多 bot 群内 N 个协程并发
        调用本方法)。传 message_id 时按 (群号, message_id) 缓存抽样结果
        (TTL 60s), 保证同一条消息的所有协程得到同一个选中 bot ——
        恰好一个 bot 回复, 不会"没人回"或"多个 bot 都回"。
        不传 message_id 则每次独立抽取(兼容无消息上下文的调用)。

        #127: message_id 也可以是跨账号稳定的消息指纹(各账号 message_id
        不同); 候选实时过滤 enabled/stale, 禁用的 bot 不会被选中。
        """
        candidates = self._eligible_candidates(group_id)
        if not candidates:
            return None
        if message_id is None or not str(message_id):
            return random.choice(candidates)

        key = (str(group_id), str(message_id))
        now = time.time()
        cached = self._group_pick_cache.get(key)
        if cached and now - cached[1] < self._PICK_CACHE_TTL and cached[0] in candidates:
            return cached[0]
        chosen = random.choice(candidates)
        # 惰性清理过期缓存, 防止无限增长
        if len(self._group_pick_cache) > 2000:
            for k in [k for k, (_, ts) in self._group_pick_cache.items()
                      if now - ts >= self._PICK_CACHE_TTL]:
                del self._group_pick_cache[k]
        self._group_pick_cache[key] = (chosen, now)
        return chosen

    def bots_in_group(self, group_id: int | str) -> list[str]:
        """该群在线且启用的 bot_id 列表(排序, 供合并回复按序收集)。"""
        return sorted(self._eligible_candidates(group_id))

    def forget_bot_groups(self, bot_id: str) -> None:
        """bot 断开连接时从所有群记录移除。"""
        for gid in list(self._group_bots):
            self._group_bots[gid].discard(bot_id)
            if not self._group_bots[gid]:
                del self._group_bots[gid]

    # ── 连接注册 / 注销 ───────────────────────────────────────

    def register(
        self, qq: int | str, websocket: "websockets.WebSocketServerProtocol",
        client_role: str = "Universal",
    ) -> BotInstance:
        """Register one transport; Event/API coexist, equal roles replace each other."""
        qq_str = str(qq)
        role = {"event": "Event", "api": "API"}.get(str(client_role).lower(), "Universal")
        connections = self._connections.setdefault(qq_str, {})
        for previous_role, previous in tuple(connections.items()):
            if role == "Universal" or previous_role in (role, "Universal"):
                del connections[previous_role]
                self._retire_instance(previous)
        peer = next(iter(connections.values()), None)
        bot_cfg = peer.config if peer is not None and peer.bound else self.find_bot_by_qq(qq)
        if bot_cfg is not None:
            instance = BotInstance(bot_cfg.bot_id, websocket, bot_cfg, bound=True)
        else:
            instance = BotInstance("", websocket, BotConfig(qq=int(qq), nickname=f"QQ{qq}"), bound=False)
        instance.client_role = role
        instance.generation = self._next_generation()
        if peer is not None:
            instance._shared_config = peer._shared_config
            instance._sent_messages = peer._sent_messages
        connections[role] = instance
        self._select_connection(qq_str)
        logger.info(f"Connection registered: QQ={qq}, bot={instance.bot_id or 'unbound'}, role={role}")
        return instance

    def _select_connection(self, qq: str) -> None:
        """Expose the API-capable transport through the existing bot lookup API."""
        connections = self._connections.get(qq, {})
        primary = next((connections[r] for r in ("Universal", "API", "Event") if r in connections), None)
        if primary is None:
            self._connections.pop(qq, None)
            return
        if primary.bound:
            self._bots[primary.bot_id] = primary
            self._unbound.pop(qq, None)
        else:
            self._unbound[qq] = primary

    def _sync_connection_binding(self, instance: BotInstance) -> None:
        """Keep both transports attached to the same live binding/config object."""
        for peer in self._connections.get(str(instance.connection_qq), {}).values():
            peer.bound, peer.bot_id, peer.config = instance.bound, instance.bot_id, instance.config

    def _next_generation(self) -> int:
        self._generation_seq += 1
        return self._generation_seq

    def _retire_instance(self, instance: BotInstance) -> None:
        """标记旧连接失效并请求关闭其 WS(不等待, 避免阻塞新连接注册)。"""
        instance.stale = True
        ws = getattr(instance, "ws", None)
        close = getattr(ws, "close", None)
        if close is None:
            return
        try:
            result = close()
        except Exception as e:  # noqa: BLE001 - 关闭旧连接失败不影响新连接
            logger.debug(f"关闭旧连接失败 (bot={instance.bot_id or instance.qq}): {e}")
            return
        if not asyncio.iscoroutine(result):
            return
        try:
            asyncio.get_running_loop().create_task(result)
        except RuntimeError:
            # 无运行中的事件循环(同步测试): 释放协程避免 "never awaited" 警告
            result.close()
        logger.info(
            f"旧连接已失效 (bot={instance.bot_id or instance.qq}, "
            f"generation={instance.generation})"
        )

    def is_current(self, instance: BotInstance) -> bool:
        """该连接实例是否仍是当前注册实例(旧连接返回 False)。"""
        if instance is None or getattr(instance, "stale", False):
            return False
        connections = self._connections.get(str(getattr(instance, "connection_qq", instance.qq)))
        if connections is not None:
            return connections.get(instance.client_role) is instance
        if getattr(instance, "bound", False):
            return self._bots.get(instance.bot_id) is instance
        return self._unbound.get(str(instance.qq)) is instance

    def unregister(self, instance: BotInstance) -> None:
        """Remove a disconnected bot instance (bound or unbound).

        传实例而非 key, 防止旧连接断开误删同 QQ 的新实例(重连竞态)。
        """
        qq_str = str(getattr(instance, "connection_qq", instance.qq))
        connections = self._connections.get(qq_str)
        if connections is not None:
            if connections.get(instance.client_role) is not instance:
                return
            del connections[instance.client_role]
            instance.stale = True
            if connections:
                self._select_connection(qq_str)
                return
            self._connections.pop(qq_str, None)
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

    def bot_ids_for_qqs(self, qqs) -> set[str]:
        """被 @ 的 QQ 号 → 已绑定且当前在线的 bot_id 集合。

        只认已绑定实例: 未绑定连接不处理消息, 也不该被当作"被 @ 的 bot"。
        qq=0 表示未绑定 QQ, 不能与真实 QQ 0 混淆, 一并排除。
        """
        wanted = {str(q) for q in qqs if str(q).strip() and str(q) != "all"}
        if not wanted:
            return set()
        return {
            bot_id for bot_id, inst in self._bots.items()
            if str(getattr(inst, "qq", "") or "") in wanted
            and str(getattr(inst, "qq", "") or "") not in ("", "0")
        }

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
