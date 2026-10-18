"""歌曲来源适配器契约 — 所有来源只负责"发现标题 + 抓取单页 → 结构化字段"。

设计约束:
- 适配器不直接写数据库, 也不做跨来源合并(那是 merge.py 的职责),
  这样才能对每个来源单独做夹具测试和限速。
- `list_titles()` 与 `fetch()` 都是同步阻塞实现。调用方负责丢进线程池;
  适配器内部只做自身来源的限速与退避。
- 每个来源必须声明 license, 入库时随记录快照, 便于回溯与合规审计。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable


class SourceError(RuntimeError):
    """来源级错误(网络/反爬/解析失败)。调用方据此统计失败并决定是否熔断。"""


@dataclass
class SourceRecord:
    """单个来源页面解析出的歌曲字段。

    只填"能确定"的字段; 取不到的留空, 由合并阶段决定是否用其它来源补。
    """

    source: str
    source_title: str
    source_url: str = ""
    license: str = ""
    name: str = ""
    singers: str = ""
    uploader: str = ""
    lyricist: str = ""
    composer: str = ""
    arranger: str = ""
    mixer: str = ""
    tuner: str = ""
    mastering: str = ""
    pv: str = ""
    illustrator: str = ""
    year: int | None = None
    introduction: str = ""
    lyrics: str = ""
    bvid: str = ""
    aliases: list[str] = field(default_factory=list)
    # 附加信息: 演唱者分类、版本标记、原译文等, 不直接进 songs 宽表
    extra: dict[str, Any] = field(default_factory=dict)

    def fields(self) -> dict[str, Any]:
        """可合并的字段集合(排除来源元数据)。"""
        return {
            "name": self.name,
            "singers": self.singers,
            "uploader": self.uploader,
            "lyricist": self.lyricist,
            "composer": self.composer,
            "arranger": self.arranger,
            "mixer": self.mixer,
            "tuner": self.tuner,
            "mastering": self.mastering,
            "pv": self.pv,
            "illustrator": self.illustrator,
            "year": self.year,
            "introduction": self.introduction,
            "lyrics": self.lyrics,
            "bvid": self.bvid,
        }

    def content_hash(self) -> str:
        """内容指纹: 判断同一页面是否需要重新合并。"""
        payload = json.dumps(
            {k: v for k, v in self.fields().items() if v not in (None, "")},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class SourceAdapter:
    """来源适配器基类。子类实现 list_titles() 与 fetch()。"""

    name: str = ""
    license: str = ""
    # 建议的抓取节奏, 供同步编排读取(实际值可被配置覆盖)
    default_interval: float = 1.0
    default_concurrency: int = 1
    # 单页请求是否需要额外的重试/退避
    max_retries: int = 2

    def list_titles(self, *, limit: int | None = None) -> Iterable[str]:
        """枚举待抓取的页面标题(可增量: 由实现自己读游标)。"""
        raise NotImplementedError

    def fetch(self, title: str) -> SourceRecord:
        """抓取并解析单个页面; 解析不出必要字段时抛 SourceError。"""
        raise NotImplementedError

    def close(self) -> None:
        """释放连接/会话(可选)。"""
        return None

    def __enter__(self) -> "SourceAdapter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
