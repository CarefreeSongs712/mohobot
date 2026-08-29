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
    """按 cmtitle/cmtype 返回预置分页结果的 fake AnubisClient。

    pages: category -> {"pages": [第一页, 续页...], "subcats": [子分类...]}
    """

    def __init__(self, pages: Dict[str, Dict[str, Any]]):
        self.pages = pages
        self.calls: List[Dict[str, Any]] = []

    def get(self, url: str, params: Dict[str, Any] | None = None, **kwargs):
        params = params or {}
        self.calls.append(dict(params))
        category = params.get("cmtitle", "")
        spec = self.pages.get(category, {})
        want_subcats = params.get("cmtype") == "subcat"
        chunks = (spec.get("subcats") if want_subcats else spec.get("pages")) or []
        page_idx = 0
        if params.get("cmcontinue"):
            page_idx = int(params["cmcontinue"])
        chunk = chunks[page_idx] if page_idx < len(chunks) else []
        members = [{"title": t} for t in chunk]
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
        "Category:A": {"pages": [["歌1", "歌2", "歌3"]]},
        "Category:B": {"pages": [["歌2", "歌4"], ["歌5"]]},
    })
    titles = fetcher_song_titles(_fetcher(client), {"categories": ["Category:A", "Category:B"]})
    assert titles == ["歌1", "歌2", "歌3", "歌4", "歌5"], titles
    print("[2] 多分类合并去重 OK")


def test_recursive_subcategories() -> None:
    """父分类只含子分类(殿堂曲/传说曲形态) → 递归枚举叶子分类页面, 去重防环。"""
    client = FakeClient({
        "Category:殿堂曲": {
            "pages": [],
            "subcats": [["Category:VOCALOID殿堂曲", "Category:SV殿堂曲"]],
        },
        "Category:VOCALOID殿堂曲": {
            "pages": [["歌A", "歌B"]],
            # 子分类反向指回父分类 → 必须被环防护拦下
            "subcats": [["Category:殿堂曲"]],
        },
        "Category:SV殿堂曲": {
            "pages": [["歌B", "歌C"]],
        },
    })
    titles = fetcher_song_titles(_fetcher(client), {"categories": ["Category:殿堂曲"]})
    assert titles == ["歌A", "歌B", "歌C"], titles
    # 环防护: 每个分类最多访问"页面 + 子分类"两次(父分类反向引用被拦下)
    visited = [p["cmtitle"] for p in client.calls]
    assert all(visited.count(x) <= 2 for x in visited), visited
    assert visited.count("Category:殿堂曲") == 2, visited
    print("[3] 递归子分类枚举 + 环防护 OK")


async def main() -> None:
    test_normalize_categories()
    test_multi_category_merge_dedup()
    test_recursive_subcategories()
    print("\nALL MULTI-CATEGORY TESTS PASSED")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())