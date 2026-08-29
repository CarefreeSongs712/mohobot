"""爬虫多分类标题拉取回归测试(离线 fake client, 不发真实请求)。

覆盖:
1. 分类配置归一化: 单字符串 / categories 列表 / 缺省
2. fetcher_song_titles: 多分类标题合并去重, 保持顺序
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.music_knowledge.vcpedia import (
    VCPediaFetcher,
    _normalize_categories,
    fetcher_song_titles,
)


class FakeClient:
    """按 cmtitle 返回预置分页结果的 fake AnubisClient。"""

    def __init__(self, pages: Dict[str, List[List[str]]]):
        # pages: category -> [第一页标题, 续页标题...]
        self.pages = pages

    def get(self, url: str, params: Dict[str, Any] | None = None, **kwargs):
        params = params or {}
        category = params.get("cmtitle", "")
        chunks = self.pages.get(category, [])
        page_idx = 0
        if params.get("cmcontinue"):
            page_idx = int(params["cmcontinue"])
        members = [{"title": t} for t in (chunks[page_idx] if page_idx < len(chunks) else [])]
        has_next = page_idx + 1 < len(chunks)
        payload = {"query": {"categorymembers": members}}
        if has_next:
            payload["continue"] = {"cmcontinue": str(page_idx + 1)}
        return SimpleNamespace(status_code=200, json=lambda: payload)


def _fetcher(client: FakeClient) -> VCPediaFetcher:
    fetcher = VCPediaFetcher.__new__(VCPediaFetcher)
    fetcher.base_url = "https://vcpedia.cn"
    fetcher.anubis = client
    fetcher.config = {}
    return fetcher


def test_normalize_categories() -> None:
    assert _normalize_categories({"category": "Category:A"}) == ["Category:A"]
    assert _normalize_categories({"categories": ["Category:A", "Category:B"]}) == ["Category:A", "Category:B"]
    assert _normalize_categories({}) == ["Category:洛天依歌曲"]
    assert _normalize_categories({"category": ""}) == ["Category:洛天依歌曲"]
    assert _normalize_categories({"categories": []}) == ["Category:洛天依歌曲"]
    print("[1] 分类配置归一化 OK")


def test_multi_category_merge_dedup() -> None:
    client = FakeClient({
        "Category:A": [["歌1", "歌2", "歌3"]],
        "Category:B": [["歌2", "歌4"], ["歌5"]],
    })
    titles = fetcher_song_titles(_fetcher(client), {"categories": ["Category:A", "Category:B"]})
    assert titles == ["歌1", "歌2", "歌3", "歌4", "歌5"], titles
    print("[2] 多分类合并去重 OK")


async def main() -> None:
    test_normalize_categories()
    test_multi_category_merge_dedup()
    print("\nALL MULTI-CATEGORY TESTS PASSED")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())