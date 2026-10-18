"""VCPedia 来源适配器 — 包装现有爬虫(Anubis PoW + wikitext 解析)。

现有 `mohobot.music_knowledge.vcpedia` 已经解决了该站的反爬与词条解析,
这里只把它适配成统一的 SourceAdapter 接口, 不复制解析逻辑:
- `fetch()` 复用 `VCPediaFetcher.fetch_entity()`;
- `list_titles()` 复用 `fetcher_song_titles()`;
- 现有 `sync_vcpedia_new_songs()` 保留不动, 供旧脚本/旧调用方继续使用。
"""

from __future__ import annotations

from typing import Any, Iterable

from loguru import logger

from mohobot.music_knowledge.normalize import clean_year, detect_language
from mohobot.music_knowledge.sources.base import SourceAdapter, SourceError, SourceRecord
from mohobot.music_knowledge.vcpedia import (
    VCPediaFetcher,
    fetcher_song_titles,
)

LICENSE = "CC BY-NC-SA 4.0 (https://vcpedia.cn)"


class VcpediaAdapter(SourceAdapter):
    """把 VCPediaFetcher 包装为统一来源接口。"""

    name = "vcpedia"
    license = LICENSE
    default_interval = 0.8
    default_concurrency = 1
    max_retries = 2

    def __init__(self, config: dict | None = None) -> None:
        self.config = dict(config or {})
        crawler_cfg = dict(self.config.get("crawler") or self.config)
        # 兼容两种写法: 直接给 crawler 段, 或包一层 {"crawler": {...}}
        self._crawler_cfg = crawler_cfg
        self._fetcher = VCPediaFetcher(crawler_cfg)
        self._interval = float(crawler_cfg.get("interval", self.default_interval))
        self._titles_cache: list[str] | None = None

    def list_titles(self, *, limit: int | None = None) -> Iterable[str]:
        try:
            titles = fetcher_song_titles(self._fetcher, self._crawler_cfg)
        except Exception as e:
            raise SourceError(f"VCPedia 列表获取失败: {e}") from e
        if limit is not None:
            titles = titles[:max(0, int(limit))]
        return titles

    def fetch(self, title: str) -> SourceRecord:
        try:
            entity = self._fetcher.fetch_entity(title)
        except Exception as e:
            raise SourceError(f"VCPedia 抓取失败 {title}: {e}") from e
        if not entity:
            raise SourceError(f"VCPedia 无内容 {title}")
        credits = entity.get("credits") or {}
        lyrics = entity.get("lyrics") or ""
        introduction = entity.get("introduction") or ""
        if not lyrics and not introduction:
            raise SourceError(f"VCPedia 词条为空 {title}")

        record = SourceRecord(
            source=self.name,
            source_title=title,
            source_url=f"{self._fetcher.base_url}/{title}",
            license=LICENSE,
            name=entity.get("name") or title,
            singers=credits.get("singers") or "",
            uploader=credits.get("uploader") or "",
            lyricist=credits.get("lyricist") or "",
            composer=credits.get("composer") or "",
            arranger=credits.get("arranger") or "",
            mixer=credits.get("mixer") or "",
            tuner=credits.get("tuner") or "",
            mastering=credits.get("mastering") or "",
            pv=credits.get("pv") or "",
            illustrator=credits.get("illustrator") or "",
            year=clean_year(credits.get("year")),
            introduction=introduction,
            lyrics=lyrics,
            bvid="",
            aliases=[title],
        )
        record.extra["lang"] = detect_language(lyrics or record.name)
        logger.debug(f"[vcpedia] 解析 {title}: lyrics={len(lyrics)} intro={len(introduction)}")
        return record

    def close(self) -> None:
        return None
