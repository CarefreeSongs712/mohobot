import json
import re
from pathlib import Path

import pytest

from mohobot.plugins.installer import InstallError
from mohobot.plugins.market import parse_market_index


def _payload() -> dict:
    return {
        "$meta": {"schema_version": 1, "name": "mohobot-plugin-index"},
        "plugins": {
            "CarefreeSongs712/demo": {
                "author": "CarefreeSongs712",
                "name": "demo",
                "version": "1.0.0",
                "repo": "https://github.com/CarefreeSongs712/mohobot-plugin-demo",
                "description": "demo",
                "sha256": "a" * 64,
                "api_version": 1,
            }
        },
    }


def test_parse_market_index(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    index = parse_market_index(json.loads(path.read_text(encoding="utf-8")))
    assert index.name == "mohobot-plugin-index"
    assert index.plugins["CarefreeSongs712/demo"].name == "demo"


def test_market_key_must_match_author_name() -> None:
    payload = _payload()
    payload["plugins"]["other/demo"] = payload["plugins"].pop("CarefreeSongs712/demo")
    with pytest.raises(InstallError):
        parse_market_index(payload)


def _docs_index_payload(doc: str) -> dict:
    """取文档中真正的索引示例块(含 plugins 的对象), 不盲取第一个 json 代码块。"""
    for block in re.findall(r"```json\s*\n(.*?)\n```", doc, re.DOTALL):
        try:
            candidate = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "plugins" in candidate:
            return candidate
    raise AssertionError("docs/PLUGIN-MARKET.md 缺少可解析的 index.json 示例")


def test_docs_index_example_matches_parser() -> None:
    """docs/PLUGIN-MARKET.md 的索引示例必须能被 parse_market_index 接受，防止文档与校验规则漂移。"""
    doc = (Path(__file__).resolve().parents[1] / "docs" / "PLUGIN-MARKET.md").read_text(encoding="utf-8")
    payload = _docs_index_payload(doc)
    for entry in payload["plugins"].values():
        if entry.get("sha256") == "<64 位十六进制>":
            entry["sha256"] = "a" * 64
    index = parse_market_index(payload)
    assert index.plugins["CarefreeSongs712/demo"].name == "demo"


def test_market_requires_sha256() -> None:
    payload = _payload()
    del payload["plugins"]["CarefreeSongs712/demo"]["sha256"]
    with pytest.raises(InstallError):
        parse_market_index(payload)


def test_market_rejects_non_github_download_url(tmp_path: Path) -> None:
    from mohobot.plugins.installer import InstallError
    from mohobot.plugins.market import parse_market_index

    payload = {
        "$meta": {"schema_version": 1},
        "plugins": {
            "CarefreeSongs712/demo": {
                "author": "CarefreeSongs712",
                "name": "demo",
                "version": "1.0.0",
                "repo": "https://github.com/CarefreeSongs712/mohobot-plugin-demo",
                "description": "demo",
                "sha256": "a" * 64,
                "api_version": 1,
                "download_url": "file:///etc/passwd",
            }
        },
    }
    try:
        parse_market_index(payload)
    except InstallError as exc:
        assert "download_url" in str(exc)
    else:
        raise AssertionError("download_url 未校验")
