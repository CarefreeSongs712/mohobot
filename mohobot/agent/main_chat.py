"""主聊天 — 移植自 Agent-LuoTianyi (src/agent/main_chat.py + prompt_assembly.py + response_parser.py)。

把"话题 + 用户信息 + 记忆/事实命中 + 歌曲信息注解"渲染成 prompt,调 LLM,
解析成结构化回复行([tone]content)。唱歌([sing])机制已移除。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from mohobot.agent.domain import ContextType, OneResponseLine, OneSentenceChat
from mohobot.agent.llm_module import LLMModule


DEFAULT_TONE = "中性"


# ── Prompt 组装 ───────────────────────────────────────────────


@dataclass(frozen=True)
class RealizationPromptInput:
    character_name: str
    character_persona: str
    speaking_style: str
    user_persona: str
    preference_context: str
    conversation_history: str
    current_time: str
    reply_topic: str
    extra_knowledge: str
    song_annotation: str = ""


class RealizationPromptAssembler:
    """把业务字段转换成 topic_reply_prompt 模板的变量。"""

    def build(
        self,
        *,
        character_name: str,
        character_persona: str,
        speaking_style: str,
        reply_topic: str,
        user_nickname: str,
        user_description: str,
        preference_context: str = "",
        conversation_history: str = "",
        fact_hits: Optional[list[str]] = None,
        memory_hits: Optional[list[str]] = None,
        song_annotation: str = "",
    ) -> RealizationPromptInput:
        from mohobot.utils.time_utils import format_utc8
        return RealizationPromptInput(
            character_name=character_name,
            character_persona=character_persona,
            speaking_style=speaking_style,
            user_persona=self.build_user_persona(user_nickname, user_description),
            preference_context=preference_context,
            conversation_history=conversation_history or "无",
            current_time=format_utc8("%Y-%m-%d %H:%M:%S"),
            reply_topic=reply_topic or "",
            extra_knowledge=self.build_extra_knowledge(fact_hits or [], memory_hits or []),
            song_annotation=song_annotation or "",
        )

    def build_user_persona(self, user_nickname: str, user_description: str) -> str:
        return (user_description or "").strip()

    def build_extra_knowledge(self, fact_hits: list[str], memory_hits: list[str]) -> str:
        merged: list[str] = []
        seen: set[str] = set()
        for item in fact_hits + memory_hits:
            text = (item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
        if not merged:
            return "无"
        return "\n".join(merged)


# ── 响应解析 ──────────────────────────────────────────────────


class StructuredResponseParser:
    """把 LLM 的多行文本解析成回复行([tone]content)。"""

    tone_pattern = re.compile(r"^\[([^\]]+)\](.*)$", flags=re.IGNORECASE)

    def __init__(self):
        self.default_response = OneSentenceChat(content="", tone=DEFAULT_TONE)

    def parse(self, response: str) -> list[OneResponseLine]:
        if not response:
            return [self.default_response]

        text = self._strip_code_fence(response)
        results: list[OneResponseLine] = []
        structured_found = False

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # 唱歌标记(旧[sing]<歌名>)已移除: 整行丢弃, 不再产生回复对象
            if line.lower().startswith("[sing]"):
                continue

            tone_match = self.tone_pattern.match(line)
            if tone_match:
                content = tone_match.group(2).strip()
                if content:
                    results.append(OneSentenceChat(
                        content=content,
                        tone=tone_match.group(1).strip().lower() or DEFAULT_TONE,
                    ))
                    structured_found = True
                continue

        if structured_found:
            return results or [self.default_response]

        logger.warning("No structured format detected in LLM response, returning empty text")
        return [self.default_response]

    def _strip_code_fence(self, response: str) -> str:
        text = response.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if len(lines) >= 2:
                return "\n".join(lines[1:-1]).strip()
        return text


# ── MainChat ──────────────────────────────────────────────────


class MainChat:
    """风格化回复后端。"""

    def __init__(
        self,
        config: Dict[str, Any],
        llm_module: LLMModule | None,
        *,
        character_name: str = "",
        character_persona: str = "",
        speaking_style: str = "",
    ):
        self.config = config or {}
        self.llm = llm_module
        self.character_name = character_name
        self.character_persona = character_persona
        self.speaking_style = speaking_style
        self.prompt_assembler = RealizationPromptAssembler()
        self.response_parser = StructuredResponseParser()

    async def generate_response(
        self,
        reply_topic: str,
        user_nickname: str,
        user_description: str,
        preference_context: str = "",
        conversation_history: str = "",
        fact_hits: Optional[List[str]] = None,
        memory_hits: Optional[List[str]] = None,
        song_annotation: str = "",
    ) -> List[OneResponseLine]:
        if self.llm is None:
            return [OneSentenceChat(content="(LLM 未配置)", tone=DEFAULT_TONE)]

        prompt_input = self.prompt_assembler.build(
            character_name=self.character_name,
            character_persona=self.character_persona,
            speaking_style=self.speaking_style,
            reply_topic=reply_topic,
            user_nickname=user_nickname,
            user_description=user_description,
            preference_context=preference_context,
            conversation_history=conversation_history,
            fact_hits=fact_hits,
            memory_hits=memory_hits,
            song_annotation=song_annotation,
        )
        response = await self._call_llm(**asdict(prompt_input))
        return self._parse_response(response)

    async def _call_llm(self, **kwargs) -> str:
        try:
            return await self.llm.generate_response(**kwargs)
        except Exception as e:
            logger.error(f"MainChat LLM call failed: {e}")
            return ""

    def _parse_response(self, response: str) -> List[OneResponseLine]:
        return self.response_parser.parse(response)