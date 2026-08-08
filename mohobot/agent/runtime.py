"""BotAgentRuntime — 按 bot 组装完整的 agent 子系统。

对应 Agent-LuoTianyi 的 AgentRuntime + AgentRegistry,但按 bot_id 隔离:
- BotAgentRuntime   : 每个 bot 一个,持有 bot 级共享资源(记忆/潜意识/意识层 Agent)。
- SessionPipeline   : 每个会话一个(私聊用户 / 群聊),持有话题流水线
                      (TopicPlanner + TopicReplier + ReflectionWorker),
                      保持原有"单用户、多会话"的管理策略。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from mohobot.agent.character_mind import CharacterSubconscious
from mohobot.agent.character_reflex import CharacterReflex
from mohobot.agent.domain import (
    ChatInputEvent, ChatInputEventType, ExtractedTopic, OneResponseLine,
    UnreadMessageSnapshot,
)
from mohobot.agent.llm_module import LLMModule
from mohobot.agent.main_chat import MainChat
from mohobot.agent.mohobot_agent import MohobotAgent
from mohobot.agent.reflection_worker import CompletedTurn, ReflectionWorker
from mohobot.agent.subconscious_memory import SubconsciousMemory
from mohobot.agent.topic_planner import TopicPlanner
from mohobot.agent.topic_replier import TopicReplier
from mohobot.agent.vector_store import VectorStore, create_vector_store


class SessionPipeline:
    """一个会话(私聊用户 / 群聊)的话题流水线。

    planner → replier → reflection 的单向链路,会话之间互不干扰;
    所有 LLM / 记忆操作都委托给所属的 BotAgentRuntime(bot 级隔离)。
    """

    def __init__(
        self,
        runtime: "BotAgentRuntime",
        session_key: str,
        chat_type: str,
        chat_id: str,
        config: Dict[str, Any],
    ):
        self.runtime = runtime
        self.session_key = session_key
        self.chat_type = chat_type
        self.chat_id = chat_id
        self.config = config
        self.logger = logger.bind(agent=runtime.bot_id, session=session_key)

        agent_cfg = config.get("agent", {}) if isinstance(config, dict) else {}

        self.planner = TopicPlanner(
            agent_cfg.get("topic_planner", {}),
            character_id=runtime.bot_id,
            context_provider=self._get_context,
        )
        self.replier = TopicReplier(
            agent_cfg.get("topic_replier", {}),
            character_id=runtime.bot_id,
            context_provider=self._get_context,
        )
        self.reflection = ReflectionWorker(
            agent_cfg.get("reflection_worker", {}),
            character_id=runtime.bot_id,
        )

        # 接线
        self.planner.set_topic_consumer(self.replier.add_topic)
        self.planner.set_extractor(self._extract_topic)
        self.replier.set_reply_one_callback(self._reply_one_topic)
        self.replier.set_send_reply_callback(self._send_reply_items)
        self.replier.set_persist_replies(self._persist_replies)
        self.replier.set_reflection_submitter(self._submit_reflection)
        self.reflection.set_write_memories_callback(self._write_topic_memories)
        self.reflection.set_update_profile_callback(self._update_user_profile)

    # ── 生命周期 ──────────────────────────────────────────────

    def start(self) -> None:
        self.planner.start_processing()
        self.replier.start_processing()
        self.reflection.start_processing()

    async def stop(self) -> None:
        for task in [
            self.planner.processor_task,
            self.replier.processor_task,
            self.reflection.processor_task,
        ]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def handle_event(self, user_id: str, event: ChatInputEvent) -> None:
        """入口: 反射短路 → 话题规划。"""
        # 反射: 戳一戳等低延迟事件
        if await self.runtime.reflex.try_handle(event, self._send_reflex_reply):
            return

        if event.event_type != ChatInputEventType.USER_MESSAGE:
            return

        # 写入数据库(用户消息, history → DB)
        self.runtime.database_manager.add_conversation(
            user_id, self.runtime.bot_id, "user",
            event.content or "", msg_type=event.message_type,
            meta_data={"chat_type": self.chat_type, "chat_id": self.chat_id,
                       "speaker": event.payload.get("speaker", "")},
        )
        await self.planner.feed_unread_message(event)

    # ── 上下文(沿用原有 ContextManager 的会话上下文, context 不变) ──

    async def _get_context(self) -> str:
        if self.runtime.context_provider is None:
            return ""
        return await self.runtime.context_provider(
            self.runtime.bot_id, self.chat_type, self.chat_id,
        )

    # ── 流水线回调 ────────────────────────────────────────────

    async def _extract_topic(
        self,
        user_id: str,
        unread_snapshot: UnreadMessageSnapshot,
        force_complete: bool = False,
        conversation_history: str = "",
    ) -> tuple[Optional[ExtractedTopic], List[Any]]:
        return await self.runtime.agent.extract_topic(
            user_id=user_id,
            unread_snapshot=unread_snapshot,
            force_complete=force_complete,
            conversation_history=conversation_history,
        )

    async def _reply_one_topic(self, topic: ExtractedTopic) -> List[OneResponseLine]:
        """规划 + 实现一个话题的回复(由 replier 调用)。"""
        user_id = self.chat_id
        conversation_history = await self._get_context()
        plan = await self.runtime.agent.plan_topic_turn(
            user_id=user_id,
            topic=topic,
            conversation_history=conversation_history,
        )
        reply_items = await self.runtime.agent.realize_topic_plan(
            user_id=user_id, plan=plan,
        )
        # 记录本次会话信息,供发送回调使用(引用最后一条触发消息)
        self._last_plan = plan
        self._trigger_message_id = (
            topic.source_messages[-1].message_id if topic.source_messages else ""
        )
        return reply_items

    async def _persist_replies(self, reply_items: List[OneResponseLine]) -> None:
        """持久化 agent 回复到数据库(conversations 表)。"""
        for item in reply_items:
            self.runtime.database_manager.add_conversation(
                self.chat_id, self.runtime.bot_id, "agent",
                item.get_content(), msg_type=item.type.value,
                meta_data={"chat_type": self.chat_type, "chat_id": self.chat_id},
            )

    async def _send_reply_items(self, reply_items: List[OneResponseLine]) -> None:
        if self.runtime.reply_handler is None:
            self.logger.warning("No reply handler set, cannot send replies")
            return
        await self.runtime.reply_handler(
            bot_id=self.runtime.bot_id,
            chat_type=self.chat_type,
            chat_id=self.chat_id,
            reply_items=reply_items,
            trigger_message_id=getattr(self, "_trigger_message_id", ""),
        )

    async def _send_reflex_reply(self, text: str) -> None:
        from mohobot.agent.domain import OneSentenceChat
        await self._send_reply_items([OneSentenceChat(content=text)])

    async def _submit_reflection(
        self, topic: ExtractedTopic, reply_items: List[OneResponseLine],
    ) -> None:
        turn = CompletedTurn(
            user_id=self.chat_id,
            character_id=self.runtime.bot_id,
            topic=topic,
            reply_items=reply_items,
            attention_plan=getattr(self, "_last_plan", None),
            conversation_history=await self._get_context(),
        )
        await self.reflection.submit_completed_turn(turn)

    async def _write_topic_memories(self, turn: CompletedTurn) -> None:
        current_dialogue = ReflectionWorker.build_current_dialogue(turn.topic, turn.reply_items)
        memory_hits = getattr(turn.attention_plan, "memory_hits", None) or []
        await self.runtime.agent.write_topic_memories(
            user_id=turn.user_id,
            current_dialogue=current_dialogue,
            related_memories=memory_hits,
            conversation_history=turn.conversation_history,
        )

    async def _update_user_profile(self, turn: CompletedTurn) -> None:
        snapshot = self.runtime.database_manager.get_context_snapshot(
            turn.user_id, self.runtime.bot_id,
        )
        await self.runtime.agent.update_user_profile_by_context(
            user_id=turn.user_id,
            context=snapshot,
        )


class BotAgentRuntime:
    """一个 bot 的完整 agent 运行时(潜意识 + 意识 + 各会话流水线)。"""

    def __init__(
        self,
        config: Dict[str, Any],
        *,
        database_manager,
        bot_id: str,
        bot_nickname: str = "",
        persona: str = "",
        context_provider: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        self.config = config
        self.database_manager = database_manager
        self.bot_id = bot_id
        self.bot_nickname = bot_nickname or f"Bot-{bot_id}"
        self.logger = logger.bind(agent=bot_id)
        self.context_provider = context_provider
        self.reply_handler: Optional[Callable[..., Awaitable[None]]] = None

        agent_cfg = config.get("agent", {}) if isinstance(config, dict) else {}
        persona_cfg = agent_cfg.get("persona", {})
        self.character_name = persona_cfg.get("character_name") or self.bot_nickname
        self.character_persona = persona_cfg.get("character_persona") or persona
        self.speaking_style = persona_cfg.get("speaking_style") or ""

        # 向量存储(可降级)
        memory_cfg = agent_cfg.get("memory", {})
        self.vector_store: VectorStore = create_vector_store(
            memory_cfg.get("vector_store", {})
        )

        # LLM 模块
        llm_cfg = agent_cfg.get("llm_modules", {})
        self.llm_modules = self._build_llm_modules(llm_cfg)

        # 潜意识记忆
        self.memory = SubconsciousMemory(
            memory_cfg,
            self.llm_modules,
            database_manager=database_manager,
            vector_store=self.vector_store,
            owner_character_id=bot_id,
        )

        # 潜意识
        self.mind = CharacterSubconscious(
            agent_cfg,
            database_manager=database_manager,
            memory=self.memory,
            llm_modules=self.llm_modules,
            character_id=bot_id,
            character_name=self.character_name,
        )

        # 意识层
        main_chat_cfg = agent_cfg.get("main_chat", {})
        self.main_chat = MainChat(
            main_chat_cfg,
            self.llm_modules.get("main_chat"),
            character_name=self.character_name,
            character_persona=self.character_persona,
            speaking_style=self.speaking_style,
        )
        self.agent = MohobotAgent(
            agent_cfg,
            database_manager=database_manager,
            main_chat=self.main_chat,
            mind=self.mind,
            character_id=bot_id,
            character_name=self.character_name,
        )

        # 反射
        self.reflex = CharacterReflex(
            agent_cfg.get("reflex", {}),
            character_id=bot_id,
        )

        # 会话流水线: session_key -> SessionPipeline
        self._sessions: Dict[str, SessionPipeline] = {}

    # ── LLM 模块构建 ──────────────────────────────────────────

    def _build_llm_modules(self, llm_cfg: Dict[str, Any]) -> Dict[str, LLMModule]:
        """从配置构建各模块的 LLMModule。"""
        global_llm = self.config.get("llm", {}) if isinstance(self.config, dict) else {}
        modules: Dict[str, LLMModule] = {}

        module_specs = {
            "main_chat": ("topic_reply_prompt", False),
            "topic_extractor": ("topic_extraction_prompt", True),
            "memory_writer": ("memory_write_prompt", True),
            "user_profile_updater": ("user_profile_update_prompt", False),
        }

        for name, (prompt_name, use_json) in module_specs.items():
            spec = (llm_cfg or {}).get(name, {})
            modules[name] = LLMModule(
                module_name=name,
                config=spec,
                prompt_name=prompt_name,
                model=spec.get("model") or global_llm.get("chat_model", ""),
                base_url=spec.get("base_url") or global_llm.get("chat_base_url", ""),
                api_key=spec.get("api_key") or global_llm.get("chat_api_key", ""),
                use_json=use_json,
                max_tokens=int(spec.get("max_tokens", 2048)),
                temperature=float(spec.get("temperature", 0.7)),
            )
            if not modules[name].is_available():
                self.logger.warning(f"LLM module '{name}' NOT available (missing config)")

        return modules

    # ── 会话管理 ──────────────────────────────────────────────

    def session_key_for(self, chat_type: str, chat_id: str) -> str:
        return f"{chat_type}:{chat_id}"

    def get_or_create_session(self, chat_type: str, chat_id: str) -> SessionPipeline:
        key = self.session_key_for(chat_type, chat_id)
        session = self._sessions.get(key)
        if session is None:
            session = SessionPipeline(
                runtime=self,
                session_key=key,
                chat_type=chat_type,
                chat_id=chat_id,
                config=self.config,
            )
            session.start()
            self._sessions[key] = session
            self.logger.debug(f"Session created: {key}")
        return session

    def get_session(self, chat_type: str, chat_id: str) -> SessionPipeline | None:
        return self._sessions.get(self.session_key_for(chat_type, chat_id))

    def set_reply_handler(self, cb) -> None:
        """设置发送回复的回调: cb(bot_id, chat_type, chat_id, reply_items, trigger_message_id)"""
        self.reply_handler = cb

    async def handle_event(self, chat_type: str, chat_id: str, event: ChatInputEvent) -> None:
        """入口: 按会话路由到对应流水线。user_id = chat_id(会话即"用户")。"""
        session = self.get_or_create_session(chat_type, chat_id)
        await session.handle_event(chat_id, event)

    async def stop(self) -> None:
        for session in self._sessions.values():
            await session.stop()
        self._sessions.clear()
        self.logger.info(f"Agent runtime stopped for bot {self.bot_id}")


class BotAgentManager:
    """按 bot_id 管理 BotAgentRuntime。"""

    def __init__(self, config: Dict[str, Any], database_manager):
        self.config = config
        self.database_manager = database_manager
        self._runtimes: Dict[str, BotAgentRuntime] = {}
        self.logger = logger.bind(agent="manager")

    def get_or_create(
        self,
        bot_id: str,
        bot_nickname: str = "",
        persona: str = "",
        context_provider=None,
    ) -> BotAgentRuntime:
        runtime = self._runtimes.get(bot_id)
        if runtime is None:
            runtime = BotAgentRuntime(
                self.config,
                database_manager=self.database_manager,
                bot_id=bot_id,
                bot_nickname=bot_nickname,
                persona=persona,
                context_provider=context_provider,
            )
            self._runtimes[bot_id] = runtime
            self.logger.info(f"Agent runtime created for bot {bot_id}")
        return runtime

    def get(self, bot_id: str) -> BotAgentRuntime | None:
        return self._runtimes.get(bot_id)

    def remove(self, bot_id: str) -> None:
        runtime = self._runtimes.pop(bot_id, None)
        if runtime is not None:
            asyncio.ensure_future(runtime.stop())

    @property
    def all_runtimes(self) -> List[BotAgentRuntime]:
        return list(self._runtimes.values())

    async def stop_all(self) -> None:
        for runtime in self._runtimes.values():
            await runtime.stop()
        self._runtimes.clear()
