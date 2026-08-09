"""FlashText 关键词链接器 — 移植自 Agent-LuoTianyi (jargon.py)。

SongEntityLinker 用 FlashText KeywordProcessor 把两份 txt 加载成
Aho-Corasick 风格的多模式匹配器(离线、极快)。
触发条件(extract_and_verify): 用户消息必须命中触发动词
{听, 唱, 点, 循环, 安利, 写, 作曲, 调教, 歌} 才激活歌名识别,
防止日常对话误触发。命中后产出《歌名》是一首歌 / 歌词是《歌名》的歌词。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from flashtext import KeywordProcessor
from loguru import logger

# 默认知识库位置: 项目 res/song_knowledge/(git 内置默认)
_DEFAULT_RES_DIR = Path(__file__).resolve().parents[3] / "res" / "song_knowledge"


class SongEntityLinker:
    """Fast song-name and lyric entity linker."""

    def __init__(
        self,
        config: dict | None = None,
        songname_file: str | None = None,
        lyric_file: str | None = None,
    ):
        self.config = config or {}
        self.songname_retriver = KeywordProcessor()
        self.lyric_retriver = KeywordProcessor()

        configured_songname = self.config.get("songname_file")
        configured_lyric = self.config.get("lyric_file")
        self.songname_file = songname_file or configured_songname or str(
            _DEFAULT_RES_DIR / "song_name_keywords.txt"
        )
        self.lyric_file = lyric_file or configured_lyric or str(
            _DEFAULT_RES_DIR / "song_lyric_keywords.txt"
        )
        self._load_keywords_from_file()

        self.trigger_verbs = {"听", "唱", "点", "循环", "安利", "写", "作曲", "调教", "歌"}

    def extract_and_verify(self, user_input: str | None) -> List[str]:
        """提取歌曲实体并验证触发条件。"""
        if not user_input:
            return []

        songnames_found = self.songname_retriver.extract_keywords(user_input)
        lyrics_found = self.lyric_retriver.extract_keywords(user_input)

        triggered = any(verb in user_input for verb in self.trigger_verbs)
        if not triggered:
            songnames_found = []

        results = []
        for song in songnames_found:
            results.append(f"《{song}》是一首歌")
        for lyric in lyrics_found:
            results.append(f"{lyric}")

        return results

    def _load_keywords_from_file(self) -> None:
        """加载关键词文件(不存在时静默降级, 不阻断启动)。"""
        songname_path = Path(self.songname_file)
        lyric_path = Path(self.lyric_file)

        loaded = 0
        if songname_path.exists():
            self.songname_retriver.add_keyword_from_file(str(songname_path))
            loaded += 1
        else:
            logger.warning(f"Song name keywords file not found: {songname_path}")

        if lyric_path.exists():
            self.lyric_retriver.add_keyword_from_file(str(lyric_path))
            loaded += 1
        else:
            logger.warning(f"Song lyric keywords file not found: {lyric_path}")

        if loaded < 2:
            logger.warning(
                "歌曲知识关键词文件缺失(仅加载 %d/2) — 歌名/歌词识别不可用", loaded
            )
