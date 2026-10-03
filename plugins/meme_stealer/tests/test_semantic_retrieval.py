"""语义检索：VLM 标注字段、嵌入文档拼接、提示词与配置默认值。"""

import json
import types
from pathlib import Path

from meme_stealer_core.core.processing.classification_parser import ClassificationParser
from meme_stealer_core.core.processing.semantic_schema import build_meme_search_text
from meme_stealer_core.core.search.embedding_service import EmbeddingService
from meme_stealer_core.core.search.meme_smart_select_service import MemeSmartSelectService

ROOT = Path(__file__).resolve().parents[1]


def _parser_with_categories(*keys):
    known = set(keys)
    return ClassificationParser(plugin_instance=types.SimpleNamespace(
        plugin_config=types.SimpleNamespace(
            categories=list(keys),
            normalize_category_strict=lambda raw: raw if raw in known else None,
        )
    ))


def test_parser_reads_annotation_without_category():
    parser = _parser_with_categories("happy", "sigh")
    payload = """
    {"approved": true, "tags": ["熊猫头", "躺平"],
     "description": "熊猫头平躺闭眼一脸放弃", "overlay_text": "算了",
     "scenes": ["又被安排加班", "算了不想干了"]}
    """
    category, tags, desc, emotion, scenes, overlay, emotions = (
        parser._parse_classification_response(payload, "x.png")
    )
    assert category == ""
    assert emotions == []
    assert overlay == "算了"
    assert "算了" in scenes
    assert "熊猫头" in tags
    assert "放弃" in desc


def test_parser_keeps_existing_category_from_custom_prompt():
    parser = _parser_with_categories("happy", "sigh", "tired")
    category, *_rest, emotions = parser._parse_classification_response(
        '{"category": "sigh", "emotions": ["sigh", "tired"], "description": "叹气"}',
        "x.png",
    )
    assert category == "sigh"
    assert emotions == ["sigh", "tired"]


def test_parser_unknown_category_is_left_for_default_bucket():
    parser = _parser_with_categories("happy", "sad")
    category, *_rest, overlay, emotions = parser._parse_classification_response(
        '{"category": "whatever", "description": "文字梗", "overlay_text": "我不听"}',
        "x.png",
    )
    assert category == ""
    assert overlay == "我不听"
    assert emotions == []


def test_parser_moderation_rejection():
    parser = _parser_with_categories("happy")
    category, *_rest = parser._parse_classification_response(
        '{"approved": false, "reason": "审核不通过"}', "x.png"
    )
    assert category == parser.CATEGORY_FILTERED


def test_validate_response_requires_description_not_category():
    parser = _parser_with_categories("happy")
    parser.validate_response('{"approved": true, "description": "猫猫", "tags": []}')
    parser.validate_response('{"approved": false, "reason": "审核不通过"}')
    try:
        parser.validate_response('{"approved": true, "category": "happy"}')
    except ValueError:
        pass
    else:
        raise AssertionError("缺少 description 应触发重试")


def test_sanitize_scenes_keeps_overlay_and_short_phrases():
    parser = ClassificationParser(plugin_instance=None)
    long_scene = "这是一句明显超过长度限制的编造对话情境" * 3
    scenes = parser.sanitize_scenes(
        ["算了", long_scene],
        overlay_text="算了",
    )
    assert scenes[0] == "算了"
    assert len(scenes) == 1
    assert all(len(item) <= 40 for item in scenes)


def test_search_text_includes_manual_character():
    text = build_meme_search_text(
        {
            "category": "happy",
            "desc": "棕发小女孩唱歌",
            "character": "neurosama",
            "overlay_text": "heart",
        },
        character_info={"neurosama": {"name": "Neuro-sama"}},
    )
    assert "neurosama" in text
    assert "Neuro-sama" in text
    assert text.index("heart") < text.index("neurosama")


def test_search_text_puts_overlay_first():
    text = build_meme_search_text(
        {
            "category": "sigh",
            "desc": "熊猫头躺平",
            "overlay_text": "算了",
            "tags": ["摆烂"],
            "scenes": ["被安排加班"],
            "emotions": ["sigh", "tired"],
        },
        category_info={"sigh": {"name": "无奈", "desc": "叹气、摆烂"}},
    )
    assert text.startswith("算了")
    assert "被安排加班" in text
    assert "无奈" in text
    assert "tired" in text


def test_build_bm25_search_text_weights_overlay_and_keeps_category_weak():
    text = build_meme_search_text(
        {
            "category": "sigh",
            "desc": "熊猫头躺平",
            "overlay_text": "算了",
            "tags": ["摆烂"],
            "scenes": ["被安排加班"],
            "emotions": ["sigh", "tired"],
        },
        category_info={"sigh": {"name": "无奈", "desc": "叹气、摆烂"}},
        bm25=True,
    )
    assert text.count("算了") == 3
    assert "无奈" not in text
    assert "叹气" not in text
    assert "sigh" in text
    assert text.count("被安排加班") == 2


def test_embedding_service_uses_overlay_in_search_text():
    plugin = types.SimpleNamespace(plugin_config=types.SimpleNamespace(category_info={}))
    svc = EmbeddingService(plugin)
    text = svc._build_search_text(
        {"category": "dumb", "desc": "猫猫瞪眼", "overlay_text": "啊？", "tags": [], "scenes": []}
    )
    assert "啊？" in text
    assert "猫猫瞪眼" in text


def test_overlay_recall_matches_context_substring():
    svc = MemeSmartSelectService(
        None, None, None, types.SimpleNamespace(_is_entry_allowed_for_event=lambda data, event: True)
    )
    idx = {
        "/a.png": {"overlay_text": "算了", "category": "sigh"},
        "/b.png": {"overlay_text": "我超爱", "category": "love"},
    }
    hits = svc._overlay_recall_paths(idx, "又被安排加班，算了不想干了", event=None)
    assert "/a.png" in hits
    assert "/b.png" not in hits


def test_scene_recall_matches_context_substring():
    svc = MemeSmartSelectService(
        None, None, None, types.SimpleNamespace(_is_entry_allowed_for_event=lambda data, event: True)
    )
    idx = {
        "/a.png": {"scenes": ["又被安排加班"], "category": "sigh"},
        "/b.png": {"scenes": ["哈哈哈哈"], "category": "happy"},
    }
    hits = svc._scene_recall_paths(idx, "又被安排加班，不想干了", event=None)
    assert "/a.png" in hits
    assert "/b.png" not in hits


def test_bundled_prompts_merge_moderation_and_annotation():
    data = json.loads((ROOT / "prompts.json").read_text(encoding="utf-8"))
    vlm = data["EMOJI_CLASSIFICATION_PROMPT"]
    assert "approved" in vlm
    assert "{chat_context}" in vlm
    assert "{emotion_list}" not in vlm
    assert '"category"' not in vlm
    assert "EMOJI_CLASSIFICATION_WITH_FILTER_PROMPT" not in data
    hint = data["MEME_HINT_PROMPT"]
    for placeholder in ("{search_tool}", "{send_tool}", "{candidate_count}"):
        assert placeholder in hint
    assert "{description}" in data["MEME_QUERY_REWRITE_PROMPT"]


def test_schema_prompt_defaults_are_blank_so_bundled_prompts_apply():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["custom_meme_classification_prompt"]["default"] == ""
    assert schema["meme_hint_prompt"]["default"] == ""
    assert schema["meme_query_rewrite_prompt"]["default"] == ""
    assert "custom_meme_classification_with_filter_prompt" not in schema
    assert "emotion_analysis_prompt" not in schema


def test_embedding_search_defaults_to_enabled():
    from meme_stealer_core.core.config.config import PluginConfig

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["enable_embedding_search"]["default"] is True
    assert PluginConfig.model_fields["enable_embedding_search"].default is True
    assert EmbeddingService(types.SimpleNamespace())._embedding_enabled() is False


def test_get_prompts_falls_back_to_bundled_when_custom_blank():
    from meme_stealer_core.core.config.config import PluginConfig

    fake_cfg = types.SimpleNamespace(custom_meme_classification_prompt="")
    result = PluginConfig.get_prompts(fake_cfg, {"EMOJI_CLASSIFICATION_PROMPT": "PLUGIN A"})
    assert result == {"emoji_classification_prompt": "PLUGIN A"}


def test_get_prompts_prefers_custom_vlm_prompt():
    from meme_stealer_core.core.config.config import PluginConfig

    fake_cfg = types.SimpleNamespace(custom_meme_classification_prompt="CUSTOM A")
    result = PluginConfig.get_prompts(fake_cfg, {"EMOJI_CLASSIFICATION_PROMPT": "PLUGIN A"})
    assert result == {"emoji_classification_prompt": "CUSTOM A"}


def test_search_text_skips_default_bucket():
    text = build_meme_search_text(
        {"category": "uncategorized", "desc": "猫猫瞪眼", "overlay_text": "啊？"}
    )
    assert "uncategorized" not in text
    assert "猫猫瞪眼" in text
