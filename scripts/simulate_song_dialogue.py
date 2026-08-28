"""模拟用户对话 — 歌曲识别 + LLM 前注入 全链路演示(真实库)。

构造一组接近真实聊天的用户消息, 逐条走:
  1. SongInfoMatcher 识别(书名号 / 裸歌名+语境词 / 歌词片段)
  2. Legacy 路径: LLMService._build_messages 生成的 user 消息(注解追加在用户消息下方)
  3. Agent 路径: TOPIC_REPLY_PROMPT 渲染(【歌曲信息】紧随对话历史之后)

用法:
  python scripts/simulate_song_dialogue.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.music_knowledge import SongInfoMatcher

DB_FOLDER = "./data/song_knowledge"
DB_FILE = "knowledge_db.db"

# 模拟的真实对话(说人话、带语气)
CASES = [
    "你会唱《千年食谱颂》吗",                 # 书名号点歌
    "唱一首达拉崩吧",                         # 裸歌名 + 点歌前缀
    "来一首新·九九八十一",                    # 新式前缀 (此前 bug 场景)
    "小笼包 叉烧包 奶黄芝麻豆沙包",           # 歌词片段(千年食谱颂)
    "很久很久以前 巨龙突然出现 带来灾难 带走公主",  # 歌词长片段(达拉崩吧)
    "你知道白鸟过河滩吗",                     # 裸歌名 + 语境词(是否, 不带前缀)
    "你是信的开头诗的内容这是哪首的？",      # 无歌名 — 应为歌词片段命中, 否则无注入
    "晚上吃什么呀",                           # 闲聊 — 不应注入
    "今天天气好好，出去走走吧",              # 闲聊 — 不应注入
    "/点歌 白鸟过河滩",                       # 网易云命令 — 应放行, 不触发歌曲注解
]


async def legacy_inject(matcher, text: str) -> str:
    """Legacy 路径: 走 LLMService._build_messages, 返回最终 user 消息内容。"""
    from mohobot.llm_service import LLMService
    from mohobot.models.config import GlobalConfig
    from mohobot.models.onebot import PrivateMessageEvent, Sender

    async def annotator(event):
        from mohobot.utils.cq_code import extract_plain_text
        t = (extract_plain_text(event.message) or "").strip()
        match = matcher.match(t) if t else None
        return match.build_annotation() if match else ""

    svc = LLMService(GlobalConfig(), song_annotator=annotator)
    ev = PrivateMessageEvent(
        time=0, self_id=0, post_type="message",
        message=[{"type": "text", "data": {"text": text}}],
        user_id=10001, message_id=1, sender=Sender(user_id=10001, nickname="测试用户"),
    )
    msgs = await svc._build_messages("bot_001", ev, context=[
        {"role": "user", "content": "早上好", "timestamp": 0},
        {"role": "assistant", "content": "早上好呀～", "timestamp": 0},
    ])
    return msgs[-1]["content"]


def agent_prompt(matcher, text: str) -> str:
    """Agent 路径: TOPIC_REPLY_PROMPT 渲染, 返回完整 prompt。"""
    from jinja2 import Template
    from mohobot.agent.main_chat import RealizationPromptAssembler
    from mohobot.agent.prompts import TOPIC_REPLY_PROMPT

    match = matcher.match(text)
    ann = match.build_annotation() if match else ""
    inp = RealizationPromptAssembler().build(
        character_name="洛天依",
        character_persona="天真活泼的虚拟歌姬",
        speaking_style="温柔可爱",
        reply_topic="回应对方提到的歌曲/话题",
        user_nickname="小测",
        user_description="",
        preference_context="",
        conversation_history="user: 早上好\nbot: 早上好呀～",
        fact_hits=[],
        memory_hits=[],
        song_annotation=ann,
    )
    return Template(TOPIC_REPLY_PROMPT).render(**asdict(inp))


def brief(text: str, limit: int = 150) -> str:
    """截断显示(保留首行与结构)。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    return head + f" …(+{len(text) - limit} chars)"


async def main() -> None:
    matcher = SongInfoMatcher(db_folder=DB_FOLDER, db_file=DB_FILE)
    print(f"== 歌曲库加载完成: 索引 {len(matcher._index)} 首 ==\n")

    for text in CASES:
        match = matcher.match(text)
        print("─" * 72)
        print(f"💬 用户: {text}")
        if match is None:
            print("   识别: (无) — 未触发歌曲注入")
        else:
            ann = match.build_annotation()
            print(f"   🎵 识别: {match.name}")
            print(f"   注解(截断): {brief(ann)}")
            if text.startswith("/"):
                print("   ⚠ 注意: 这是网易云命令, 不应被歌曲库拦截!")
        # 两条 LLM 路径注入结果(无论是否命中都展示最终消息形态)
        legacy = await legacy_inject(matcher, text)
        print(f"   [Legacy] user 消息尾部: {brief(legacy[-220:], 220)}")
        prompt = agent_prompt(matcher, text)
        if "【歌曲信息】" in prompt:
            idx = prompt.find("【歌曲信息】")
            print(f"   [Agent ] prompt 尾部: {brief(prompt[idx:idx + 200], 200)}")
        else:
            print(f"   [Agent ] prompt 尾部: {brief(prompt[-120:], 120)}")

    print("\n" + "─" * 72)
    print("用例全部跑完。")


if __name__ == "__main__":
    asyncio.run(main())