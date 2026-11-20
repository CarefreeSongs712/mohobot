import json
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


def test_market_requires_sha256() -> None:
    payload = _payload()
    del payload["plugins"]["CarefreeSongs712/demo"]["sha256"]
    with pytest.raises(InstallError):
        parse_market_index(payload)
