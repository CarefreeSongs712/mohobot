"""数据模型 — 移植自 Agent-LuoTianyi (server/src/domain)。

包含话题、记忆、状态、计划、回复行等核心数据结构。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


# ── 工具 ──────────────────────────────────────────────────────


def clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def new_id(prefix: str = "") -> str:
    return f"{prefix}_{uuid.uuid4().hex}" if prefix else str(uuid.uuid4())


# ── 聊天输入事件 ──────────────────────────────────────────────


class ChatInputEventType(str, Enum):
    USER_MESSAGE = "user_message"
    USER_TOUCH = "user_touch"
    USER_TYPING = "user_typing"


@dataclass
class ChatInputEvent:
    """一条来自渠道的聊天输入事件(简化版,无 WebSocket 专属字段)。"""
    event_type: ChatInputEventType = ChatInputEventType.USER_MESSAGE
    user_id: str = ""
    character_id: str = ""
    content: str = ""
    message_id: str = ""
    message_type: str = "text"  # text / image
    terms: list[str] = field(default_factory=list)
    timestamp: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content


@dataclass
class UnreadMessage:
    """未读消息(话题规划器缓冲单元)。"""
    message_id: str
    content: str
    message_type: str = "text"
    target_character_ids: tuple[str, ...] = ("bot",)
    terms: list[str] = field(default_factory=list)
    timestamp: float = 0.0
    speaker: str = ""  # 发言者,如 "{qq}-{nickname}"(群聊多人时用于话题提取)
    song_annotation: str = ""  # 歌曲信息注解(LLM 前注入, 紧跟用户消息下方)


@dataclass
class UnreadMessageSnapshot:
    """未读消息快照。"""
    messages: list[UnreadMessage] = field(default_factory=list)
    version: int = 0


# ── 话题 ──────────────────────────────────────────────────────


@dataclass
class ExtractedTopic:
    """从用户消息中提取的一个回复话题。"""
    topic_id: str
    source_messages: list[UnreadMessage]
    topic_content: str
    memory_attempts: list[str]
    fact_constraints: list[str]
    target_character_ids: tuple[str, ...] = ("bot",)
    source_event_type: str | None = None
    is_forced_from_incomplete: bool = False


# ── 记忆 ──────────────────────────────────────────────────────


class MemoryType(str, Enum):
    USER_PROFILE = "user_profile"
    USER_FACT = "user_fact"
    INTERACTION_EVENT = "interaction_event"
    AGENT_LIFE = "agent_life"
    WORLD_EVENT = "world_event"
    DIARY_SOURCE = "diary_source"
    PUBLIC_DIARY = "public_diary"
    SONG_KNOWLEDGE = "song_knowledge"
    CHARACTER_SETTING = "character_setting"


class MemoryVisibility(str, Enum):
    PRIVATE = "private"
    CHARACTER_PRIVATE = "character_private"
    PUBLIC = "public"


@dataclass(frozen=True)
class MemoryRecord:
    """规范记忆正本。向量/图边都回指它。"""
    owner_character_id: str
    memory_type: MemoryType
    visibility: MemoryVisibility
    source: str
    content: str
    subject_user_id: str | None = None
    summary: str | None = None
    importance: float = 0.5
    confidence: float = 1.0
    emotional_valence: float | None = None
    happened_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class MemoryHit:
    """一次记忆召回命中。"""
    rendered_text: str
    score: float
    query: str
    source: str = "vector"  # canonical_vector / legacy_vector / vector
    record: MemoryRecord | None = None
    vector_id: str | None = None

    @property
    def memory_type(self) -> str:
        return str(self.record.memory_type.value) if self.record else ""

    @property
    def memory_record_id(self) -> str:
        return self.record.id if self.record else ""


@dataclass(frozen=True)
class MemoryContext:
    """话题相关的记忆召回结果。"""
    hits: tuple[MemoryHit, ...] = ()

    def render_for_prompt(self) -> list[str]:
        return [hit.rendered_text for hit in self.hits]

    def by_type(self, memory_type: MemoryType) -> tuple[MemoryHit, ...]:
        return tuple(h for h in self.hits if h.memory_type == memory_type.value)


@dataclass(frozen=True)
class MemoryUpdateCommand:
    """(legacy) 记忆更新命令。"""
    type: str
    content: str
    uuid: str | None = None


# ── 状态 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentState:
    """角色级全局状态快照。"""
    owner_character_id: str
    mood: float = 0.55
    arousal: float = 0.45
    vitality: float = 0.70
    connection_need: float = 0.35
    attention_bias: tuple[str, ...] = ()
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def with_updates(
        self,
        *,
        mood: float | None = None,
        arousal: float | None = None,
        vitality: float | None = None,
        connection_need: float | None = None,
        attention_bias: tuple[str, ...] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "AgentState":
        return replace(
            self,
            mood=clamp_unit(self.mood if mood is None else mood),
            arousal=clamp_unit(self.arousal if arousal is None else arousal),
            vitality=clamp_unit(self.vitality if vitality is None else vitality),
            connection_need=clamp_unit(self.connection_need if connection_need is None else connection_need),
            attention_bias=self.attention_bias if attention_bias is None else tuple(attention_bias),
            metadata=self.metadata if metadata is None else dict(metadata),
            updated_at=datetime.now(),
        )


# ── 动作计划 ──────────────────────────────────────────────────


class ActionType(str, Enum):
    SAY = "say"
    WRITE_MEMORY = "write_memory"
    WRITE_DIARY = "write_diary"
    ASK_FOLLOWUP = "ask_followup"
    CALL_CAPABILITY = "call_capability"
    NO_REPLY = "no_reply"


@dataclass(frozen=True)
class PlannedAction:
    action_type: ActionType
    payload: Mapping[str, Any] = field(default_factory=dict)
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class ActionPlan:
    target_character_id: str
    actions: tuple[PlannedAction, ...]
    attention_notes: tuple[str, ...] = ()
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# ── 注意力计划 ────────────────────────────────────────────────


@dataclass(frozen=True)
class TopicAttentionPlan:
    """意识层对一个话题的注意力计划。"""
    user_id: str
    topic_id: str
    target_character_id: str
    topic_content: str
    conversation_history: str
    memory_context: MemoryContext = field(default_factory=MemoryContext)
    agent_state: AgentState | None = None
    memory_hits: list[str] = field(default_factory=list)
    fact_hits: list[str] = field(default_factory=list)
    attention_notes: tuple[str, ...] = ()
    action_plan: ActionPlan | None = None


# ── 回复行 ────────────────────────────────────────────────────


class ContextType(str, Enum):
    TEXT = "text"
    CMD = "cmd"
    IMAGE = "image"


@dataclass
class OneResponseLine:
    type: ContextType
    uuid: str = ""

    def get_content(self) -> str:
        raise NotImplementedError


@dataclass
class OneSentenceChat(OneResponseLine):
    type: ContextType = ContextType.TEXT
    expression: str = ""
    tone: str = ""
    content: str = ""
    sound_content: str = ""
    uuid: str = ""

    def get_content(self) -> str:
        return self.content
