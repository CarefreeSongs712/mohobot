"""mohobot 插件约定：入口文件、钩子形态与 _conf_schema.json 的一致性。"""

import ast
import json
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
# mohobot PluginSystem._coerce_config 支持的类型
SCHEMA_TYPES = {"string", "text", "int", "float", "bool", "list", "object"}


def _plugin_class() -> ast.ClassDef:
    tree = ast.parse((PLUGIN_DIR / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Plugin":
            return node
    raise AssertionError("main.py 必须定义字面名为 Plugin 的类")


def _methods(cls: ast.ClassDef) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_plugin_entry_has_hooks():
    methods = _methods(_plugin_class())
    for hook in ("on_message", "on_message_observed", "on_perception", "on_startup", "on_shutdown"):
        assert isinstance(methods.get(hook), ast.AsyncFunctionDef), hook


def test_on_config_update_is_sync():
    """mohobot 同步调用 on_config_update，写成 async 会被丢弃不执行。"""
    assert isinstance(_methods(_plugin_class()).get("on_config_update"), ast.FunctionDef)


def test_injectors_are_classmethods():
    methods = _methods(_plugin_class())
    for name, node in methods.items():
        if name.startswith("inject_"):
            decorators = {getattr(d, "id", "") for d in node.decorator_list}
            assert "classmethod" in decorators, name


@pytest.mark.parametrize("key", list(SCHEMA))
def test_schema_entry_is_valid(key):
    spec = SCHEMA[key]
    assert spec["type"] in SCHEMA_TYPES
    assert spec.get("description"), key
    # 面板保存时 invisible 字段不会提交，会被重置成默认值
    assert not spec.get("invisible"), key
    default = spec["default"]
    expected = {
        "string": str,
        "text": str,
        "int": int,
        "float": (int, float),
        "bool": bool,
        "list": list,
    }[spec["type"]]
    assert isinstance(default, expected), key
    if spec["type"] == "int":
        assert not isinstance(default, bool), key


def test_schema_defaults_match_plugin_config(tmp_path):
    from meme_stealer_core.core.config.config import PluginConfig

    fields = PluginConfig.model_fields
    cfg = PluginConfig({}, tmp_path)
    for key, spec in SCHEMA.items():
        assert key in fields, f"{key} 不是 PluginConfig 字段"
        assert getattr(cfg, key) == spec["default"], key


def test_prompts_file_has_required_keys():
    prompts = json.loads((PLUGIN_DIR / "prompts.json").read_text(encoding="utf-8"))
    for key in ("EMOJI_CLASSIFICATION_PROMPT", "MEME_HINT_PROMPT", "MEME_QUERY_REWRITE_PROMPT"):
        assert isinstance(prompts.get(key), str) and prompts[key].strip(), key


def test_dashboard_assets_present():
    dashboard = PLUGIN_DIR / "pages" / "dashboard"
    for name in ("index.html", "app.js", "app.css", "template.js", "vendor/vue.global.prod.js"):
        assert (dashboard / name).is_file(), name
    for locale in ("zh-CN", "en-US"):
        data = json.loads((PLUGIN_DIR / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        assert data["pages"]["dashboard"], locale
