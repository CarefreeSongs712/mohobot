"""知识库切块器回归: md 按 H2 切 / index.json 逐条成卡 / README 跳过 / 超长再切。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.knowledge.chunker import chunk_source_dir, scan_source_dir


def _write(tmp: Path, rel: str, text: str) -> None:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_markdown_split_by_h2():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, "虚拟歌手/洛天依.md", (
            "# 洛天依\n"
            "导言行。\n"
            "\n"
            "## 基本资料\n"
            "生日 07-12\n"
            "\n"
            "## 简介\n"
            "世界首位中文 VOCALOID 虚拟歌手。\n"
        ))
        chunks = chunk_source_dir(tmp)
        titles = [c.title for c in chunks]
        # 导言有正文(导言行)→ 单独成块; 纯标题导言不会产出块
        assert titles == ["洛天依", "洛天依 · 基本资料", "洛天依 · 简介"], titles
        assert chunks[0].source == "虚拟歌手/洛天依.md"
        assert "导言行" in chunks[0].text
        assert "生日 07-12" in chunks[1].text
        assert all(c.chunk_id for c in chunks)


def test_markdown_preamble_chunk_and_long_split():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        long_body = "\n\n".join(f"第{i}段" + "字" * 300 for i in range(5))
        _write(tmp, "概述.md", (
            "# 概述\n"
            "# 占位标题\n"
            "## 总览\n" + long_body + "\n"
        ))
        chunks = chunk_source_dir(tmp)
        # 超长段按空行切成多块, 全部带段标题
        overview = [c for c in chunks if c.title == "概述 · 总览"]
        assert len(overview) >= 2
        assert all(len(c.text) <= 1000 or "\n" in c.text for c in overview)
        # chunk_id 在同文件内不重复
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))


def test_index_json_cards_and_aliases_source():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        index = {
            "meta": {"name": "测试库"},
            "singers": [{
                "id": "luo-tianyi", "name": "洛天依", "en_name": "Luo Tianyi",
                "aliases": ["洛神", "吃货殿下"], "category": "vocaloid-vsinger",
                "engines": ["VOCALOID3"], "company": "上海禾念",
                "debut_date": "2012-07-12", "voice_provider": "山新",
                "character": {"birthday": "07-12", "height": "156cm"},
                "representative_songs": ["普通DISCO"], "file": "虚拟歌手/洛天依.md",
                "summary": "测试简介。",
            }],
            "songs": [{
                "title": "普通DISCO", "singers": ["洛天依"], "producer": "ilem",
                "date": "2015-03-21", "engine": "VOCALOID3",
                "achievement": "神话曲", "file": "03-代表名曲.md",
            }],
            "producers": [{
                "name": "ilem", "active": "2014–", "role": "全能",
                "works": ["普通DISCO"], "highlight": "测试亮点", "file": "04-知名创作者.md",
            }],
        }
        (tmp / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        chunks = chunk_source_dir(tmp)
        cards = {c.title.split(" · ")[0]: c for c in chunks}
        assert set(cards) == {"歌手档案", "歌曲档案", "创作者档案"}
        singer = cards["歌手档案"]
        assert "【歌手档案】洛天依 (Luo Tianyi)" in singer.text
        assert "别名: 洛神、吃货殿下" in singer.text
        assert "代表曲: 普通DISCO" in singer.text
        assert cards["歌曲档案"].text.startswith("【歌曲档案】普通DISCO")
        assert "亮点: 测试亮点" in cards["创作者档案"].text


def test_readme_skipped_and_scan_states():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, "README.md", "# 导航\n无检索价值\n")
        _write(tmp, "01-历史.md", "# 历史\n## 年表\n2012 洛天依出道\n")
        _write(tmp, "其它.txt", "不该被收进去")
        files = scan_source_dir(tmp)
        assert [f[0] for f in files] == ["01-历史.md"]
        rel, abs_path, mtime, size = files[0]
        assert abs_path.name == "01-历史.md"
        assert mtime > 0 and size > 0
        assert chunk_source_dir(tmp)  # md 正常切块


def test_missing_dir_returns_empty():
    assert chunk_source_dir("Z:/no/such/dir") == []
    assert scan_source_dir("Z:/no/such/dir") == []
