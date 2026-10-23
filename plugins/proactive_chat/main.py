"""私聊主动聊天插件 — bot 在私聊中按随机窗口主动发起消息。

机制移植自 astrbot_plugin_proactive_chat (AGPL-3.0, 见本目录 LICENSE 与 README):

- 自动跟踪: 任何与 bot 的私聊消息都会登记会话并开始排程(exclude_users 兜底);
- 触发模型: 用户每发一条私聊 → 未回复计数归零 → 在 [min, max] 分钟内随机排
  下一次主动消息; 主动消息发送成功后计数 +1 并排下一窗口;
- 打扰控制: 免打扰时段(quiet_hours)跳过并重排; 连续 max_unanswered_times 次
  未回复则暂停, 用户回复即恢复;
- 冷启动: enable_auto_trigger 开启时, 启动后静默 N 分钟且无消息的会话补建任务;
- 重启恢复: 到期任务持久化在 data/proactive_chat/state.json, 重启后照常触发
  (超过容忍期的过期任务清理, 不补发);
- 发送形态与被动回复一致: 复用 MessageHandler 的分段/段间延迟/TTS 管线,
  上下文回写只追加 assistant 一条(主动提示词当轮生效, 不落盘)。

本插件只做装配(钩子转发 + 服务注入); 核心逻辑在 proactive_chat_core 包。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

# 目录插件: 把插件目录加入 sys.path, 用绝对导入加载 core 包
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from mohobot.models.onebot import PrivateMessageEvent

from proactive_chat_core.engine import ProactiveEngine


class Plugin:
    """私聊主动聊天: 随机窗口主动发起消息, 形态与被动回复一致。"""

    info = {"commands": []}

    # 周期任务: 轮询到期任务(实例属性, on_config_update 热更新生效)
    interval_sec = 30

    # 注入引用(PluginSystem.apply_injections), 必须是 classmethod
    _ws_server = None
    _bot_manager = None
    _llm_service = None
    _context_manager = None
    _persona_service = None
    _message_handler = None
    _data_dir = "./data"

    @classmethod
    def inject_ws_server(cls, ws_server) -> None:
        cls._ws_server = ws_server

    @classmethod
    def inject_bot_manager(cls, bot_manager) -> None:
        cls._bot_manager = bot_manager

    @classmethod
    def inject_llm_service(cls, llm_service) -> None:
        cls._llm_service = llm_service

    @classmethod
    def inject_context_manager(cls, context_manager) -> None:
        cls._context_manager = context_manager

    @classmethod
    def inject_persona_service(cls, persona_service) -> None:
        cls._persona_service = persona_service

    @classmethod
    def inject_message_handler(cls, message_handler) -> None:
        cls._message_handler = message_handler

    @classmethod
    def inject_data_dir(cls, data_dir: str) -> None:
        cls._data_dir = data_dir

    def __init__(self) -> None:
        self.plugin_config: dict = {}
        self.engine: ProactiveEngine | None = None

    def _engine_ref(self, name: str):
        """引擎惰性取引用; ban_store 按需构造(与 qzone 同款模式)。

        issue #9 第 2 点: 被全局封禁的用户若在排队期间被封, 到期后不得再发
        主动消息。引擎在生成/发送前通过 ``banned_check`` 查询本存储。
        """
        if name == "ban_store":
            store = getattr(type(self), "_ban_store_instance", None)
            if store is None:
                try:
                    from mohobot.ban.store import BanStore
                    store = BanStore(data_dir=str(self._data_dir))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[主动消息] BanStore 不可用: {exc}")
                    return None
                type(self)._ban_store_instance = store
            return store
        return getattr(type(self), f"_{name}", None)

    def _ensure_engine(self) -> ProactiveEngine:
        """惰性创建引擎(注入按类属性在调用时解析, 与创建时序解耦)。"""
        if self.engine is None:
            self.engine = ProactiveEngine(
                state_path=Path(self._data_dir) / "proactive_chat" / "state.json",
                config=dict(self.plugin_config or {}),
                # 引擎引用名无下划线前缀, 插件类属性带 _ 前缀, 此处统一映射
                get_ref=self._engine_ref,
            )
        else:
            self.engine.update_config(dict(self.plugin_config or {}))
        return self.engine

    async def on_startup(self) -> None:
        engine = self._ensure_engine()
        await engine.on_startup()
        logger.info(
            f"[主动消息] 插件已启动, tick={self.interval_sec}s, "
            f"enable={self.plugin_config.get('enable', True)}",
        )

    async def on_shutdown(self) -> None:
        # 状态全程即时落盘, 无需额外保存; tick 循环由 PluginSystem 统一停止
        logger.info("[主动消息] 插件已停止")

    async def on_message_observed(self, bot_id: str, event, raw: dict) -> tuple[bool, None]:
        """观察私聊消息: 登记/重置/重排(不消费消息, 后续流程照常)。"""
        if not isinstance(event, PrivateMessageEvent):
            return (False, None)
        user_id = str(getattr(event, "user_id", "") or "")
        if not user_id:
            return (False, None)
        sender = getattr(event, "sender", None)
        sender_name = str(getattr(sender, "nickname", "") or "") if sender else ""
        engine = self._ensure_engine()
        try:
            await engine.on_private_message(bot_id, user_id, sender_name)
        except Exception as exc:
            logger.error(f"[主动消息] {bot_id}:{user_id} 消息观察处理失败: {exc}")
        return (False, None)

    async def on_tick(self) -> None:
        engine = self._ensure_engine()
        self.interval_sec = engine.tick_interval()
        try:
            await engine.tick()
        except Exception as exc:
            logger.error(f"[主动消息] tick 异常: {exc}")

    def on_config_update(self, config: dict | None = None) -> None:
        """配置热更新(同步钩子): 刷新引擎配置快照。

        框架以 ``updater(merged)`` 调用(传新配置 dict); 旧签名不带参数会抛
        TypeError 导致本次热更新失败(issue #9 第 2 点)。这里接受可选参数并在
        收到时优先使用, 同时仍兼容无参调用。
        """
        if config is not None and isinstance(config, dict):
            self.plugin_config = dict(config)
        if self.engine is not None:
            self.engine.update_config(dict(self.plugin_config or {}))
