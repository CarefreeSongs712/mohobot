"""新工作流：收录上下文、软上限、语义冗余淘汰、语义空间投影、提示词渲染。"""

import time
import types

import numpy as np
import pytest

from meme_stealer_core.core.events.chat_context_buffer import ChatContextBuffer
from meme_stealer_core.core.maintenance.retention import (
    redundancy_eviction_candidates,
    soft_limit_chance,
    usage_rate,
)
from meme_stealer_core.core.processing.prompt_manager import PromptManager
from meme_stealer_core.core.search.semantic_space import (
    auto_radius,
    distances_from,
    nearest_neighbor_distances,
    pca_2d,
    radius_neighbors,
)

DAY = 86400


class ChatEvent:
    def __init__(self, text, sender="u1", name="小明", group="g1", has_image=False):
        self._text = text
        self._sender = sender
        self._name = name
        self._group = group
        self._has_image = has_image
        self.unified_msg_origin = f"qq:GroupMessage:{group}" if group else f"qq:FriendMessage:{sender}"

    def get_message_str(self):
        return self._text

    def get_messages(self):
        image = type("Image", (), {})()
        return [image] if self._has_image else []

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._name

    def get_group_id(self):
        return self._group


# ── 收录上下文 ────────────────────────────────────────────


def test_context_excludes_messages_after_the_image():
    buf = ChatContextBuffer()
    buf.record_event(ChatEvent("今天又加班", sender="a", name="甲"))
    buf.record_event(ChatEvent("太惨了", sender="b", name="乙"))
    image_event = ChatEvent("", sender="a", name="甲", has_image=True)
    seq = buf.record_event(image_event)
    buf.record_event(ChatEvent("之后的消息", sender="b", name="乙"))

    text = buf.render_for_event(image_event, before_seq=seq, sender_limit=5, group_limit=15)
    assert "今天又加班" in text and "太惨了" in text
    assert "之后的消息" not in text
    assert "甲（图片发送者）" in text


def test_group_limit_and_sender_supplement_are_configurable():
    buf = ChatContextBuffer()
    for i in range(3):
        buf.record_event(ChatEvent(f"发送者旧消息{i}", sender="a", name="甲"))
    for i in range(10):
        buf.record_event(ChatEvent(f"群消息{i}", sender="b", name="乙"))
    image_event = ChatEvent("看图", sender="a", name="甲", has_image=True)
    seq = buf.record_event(image_event)

    text = buf.render_for_event(image_event, before_seq=seq, sender_limit=2, group_limit=4)
    assert "群聊最近 4 条" in text
    assert "群消息9" in text and "群消息5" not in text
    # 发送者的消息不在群窗口里，补充最近 2 条
    assert "发送者旧消息2" in text and "发送者旧消息1" in text
    assert "发送者旧消息0" not in text
    assert "看图 [图片]" in text

    assert buf.render_for_event(image_event, before_seq=seq, sender_limit=0, group_limit=0) == ""


def test_private_chat_uses_sender_messages_only_and_bot_replies_are_marked():
    buf = ChatContextBuffer()
    buf.record_event(ChatEvent("在吗", sender="a", name="甲", group=""))
    buf.record_bot_reply(ChatEvent("", sender="a", group=""), "在的")
    image_event = ChatEvent("", sender="a", name="甲", group="", has_image=True)
    seq = buf.record_event(image_event)
    text = buf.render_for_event(image_event, before_seq=seq, sender_limit=5, group_limit=15)
    assert "发送者最近的消息" in text
    assert "在吗" in text
    assert "群聊" not in text

    group_buf = ChatContextBuffer()
    group_buf.record_bot_reply(ChatEvent("", group="g1"), "我是机器人")
    ev = ChatEvent("", has_image=True)
    seq = group_buf.record_event(ev)
    assert "Bot（机器人）: 我是机器人" in group_buf.render_for_event(
        ev, before_seq=seq, sender_limit=5, group_limit=15
    )


def test_conversations_are_isolated():
    buf = ChatContextBuffer()
    buf.record_event(ChatEvent("别的群", group="g2"))
    ev = ChatEvent("", group="g1", has_image=True)
    seq = buf.record_event(ev)
    assert "别的群" not in buf.render_for_event(ev, before_seq=seq, sender_limit=5, group_limit=15)


# ── 提示词渲染 ────────────────────────────────────────────


def _prompt_manager(template):
    plugin = types.SimpleNamespace(
        plugin_config=types.SimpleNamespace(categories=["happy"], category_info={}),
        EMOJI_CLASSIFICATION_PROMPT=template,
    )
    return PromptManager(plugin)


def test_chat_context_fills_placeholder_or_is_appended():
    ctx = "<chat_context>\nA: 早\n</chat_context>"
    with_slot = _prompt_manager("审核并标注\n{chat_context}\n只输出 JSON")
    assert with_slot.build_classification_prompt(chat_context=ctx).endswith(
        f"{ctx}\n只输出 JSON"
    )
    assert "（无聊天记录）" in with_slot.build_classification_prompt()

    without_slot = _prompt_manager("审核并标注")
    assert without_slot.build_classification_prompt(chat_context=ctx) == f"审核并标注\n\n{ctx}"
    assert without_slot.build_classification_prompt() == "审核并标注"


def test_legacy_emotion_list_placeholder_still_renders():
    pm = _prompt_manager("分类：{emotion_list}")
    assert pm.build_classification_prompt() == "分类：happy"


def test_legacy_default_prompt_is_dropped_but_custom_kept():
    import json
    import subprocess
    from pathlib import Path

    from meme_stealer_core.core.config.config import PluginConfig

    root = Path(__file__).resolve().parents[1]
    legacy = None
    try:
        raw = subprocess.run(
            ["git", "show", "fbbb8483:_conf_schema.json"],
            cwd=root, capture_output=True, check=True,
        ).stdout
        legacy = json.loads(raw.decode("utf-8"))["custom_meme_classification_prompt"]["default"]
    except Exception:
        pytest.skip("git history unavailable")

    import tempfile

    data_dir = tempfile.mkdtemp(prefix="meme_stealer_test_")
    assert PluginConfig({"custom_meme_classification_prompt": legacy}, data_dir).custom_meme_classification_prompt == ""
    custom = "我的提示词，包含 approved"
    assert PluginConfig({"custom_meme_classification_prompt": custom}, data_dir).custom_meme_classification_prompt == custom


def test_config_defaults_for_new_workflow(tmp_path):
    from meme_stealer_core.core.config.config import PluginConfig

    cfg = PluginConfig({}, tmp_path)
    assert cfg.audit_required is False
    assert cfg.enable_embedding_search is True
    assert cfg.vlm_context_sender_messages == 5
    assert cfg.vlm_context_group_messages == 15
    assert cfg.categories[0] == "uncategorized"
    assert cfg.closest_category("") == "uncategorized"
    assert cfg.closest_category("完全无关") == "uncategorized"
    assert cfg.closest_category("happy") == "happy"


# ── 软上限 ────────────────────────────────────────────────


def test_soft_limit_decays_but_never_reaches_zero():
    assert soft_limit_chance(0.3, 50, 100) == 0.3
    assert soft_limit_chance(0.3, 100, 100) == 0.3
    assert soft_limit_chance(0.4, 200, 100, exponent=2) == pytest.approx(0.1)
    assert soft_limit_chance(0.4, 10_000, 100, exponent=2, floor=0.02) == 0.02
    # 原概率低于下限时保持原概率
    assert soft_limit_chance(0.01, 10_000, 100, floor=0.02) == 0.01
    assert soft_limit_chance(1.0, 300, 100, exponent=1) == pytest.approx(1 / 3)


def test_event_handler_applies_soft_limit(monkeypatch):
    from meme_stealer_core.core.events import event_handler as module

    cfg = types.SimpleNamespace(
        steal_mode="probability", steal_chance=1.0, max_reg_num=10,
        steal_pool_capacity=10, soft_limit_exponent=2.0, soft_limit_min_chance=0.0,
    )
    handler = module.EventHandler.__new__(module.EventHandler)
    handler.plugin = types.SimpleNamespace(plugin_config=cfg)
    handler._library_count_cache = (0.0, 0)
    handler._current_store_count = lambda *, to_pending: 40
    monkeypatch.setattr(module.random, "random", lambda: 0.07)
    # 40 张、上限 10 → 采纳率 (10/40)^2 = 0.0625 < 0.07
    assert handler._should_process_image(to_pending=False) is False
    monkeypatch.setattr(module.random, "random", lambda: 0.05)
    assert handler._should_process_image(to_pending=False) is True


# ── 语义空间 ──────────────────────────────────────────────


def test_radius_neighbors_and_nearest_distances():
    mat = np.array([[0, 0], [0.1, 0], [0, 0.15], [5, 5]], dtype=np.float32)
    neighbors = radius_neighbors(mat, 0.2)
    assert sorted(neighbors[0].tolist()) == [1, 2]
    assert neighbors[3].tolist() == []
    nn = nearest_neighbor_distances(mat)
    assert nn[0] == pytest.approx(0.1, abs=1e-5)
    assert auto_radius(mat) == pytest.approx(float(np.median(nn)))
    assert distances_from(mat, 3)[3] == 0


def test_pca_projection_never_expands_distances():
    rng = np.random.default_rng(1)
    mat = rng.normal(size=(60, 32)).astype(np.float32) * 3.0 + 7.0
    coords, ratio = pca_2d(mat)
    assert coords.shape == (60, 2)
    assert ratio[0] >= ratio[1] >= 0
    for i in (0, 17, 42):
        full = distances_from(mat, i)
        flat = np.linalg.norm(coords - coords[i], axis=1)
        assert np.all(flat <= full + 1e-3)
    # 不做归一化：投影尺度与原始向量尺度一致
    assert np.abs(coords).max() > 3.0


def test_project_2d_uses_umap_and_keeps_clusters_apart():
    pytest.importorskip("umap")
    from meme_stealer_core.core.search.semantic_space import project_2d

    rng = np.random.default_rng(3)
    a = rng.normal(size=(20, 24)) + 10
    b = rng.normal(size=(20, 24)) - 10
    coords, method = project_2d(np.vstack([a, b]).astype(np.float32))
    assert method == "umap"
    assert coords.shape == (40, 2)
    ca, cb = coords[:20].mean(axis=0), coords[20:].mean(axis=0)
    spread = max(coords[:20].std(axis=0).max(), coords[20:].std(axis=0).max())
    assert np.linalg.norm(ca - cb) > 2 * spread


def test_project_2d_falls_back_to_pca_for_tiny_sets():
    from meme_stealer_core.core.search.semantic_space import project_2d

    coords, method = project_2d(np.eye(3, dtype=np.float32))
    assert method == "pca"
    assert coords.shape == (3, 2)


# ── 语义冗余淘汰 ──────────────────────────────────────────


def _meta(uses, age_days, now, **extra):
    return {"use_count": uses, "created_at": int(now - age_days * DAY), **extra}


def test_redundancy_eviction_keeps_most_used_in_cluster():
    now = time.time()
    cluster = {f"/c{i}.png": np.array([0.0 + i * 0.01, 0.0], dtype=np.float32) for i in range(5)}
    loner = {"/lonely.png": np.array([9.0, 9.0], dtype=np.float32)}
    vectors = {**cluster, **loner}
    index = {
        "/c0.png": _meta(50, 30, now),
        "/c1.png": _meta(0, 30, now),
        "/c2.png": _meta(1, 30, now),
        "/c3.png": _meta(0, 30, now),
        "/c4.png": _meta(2, 30, now),
        "/lonely.png": _meta(0, 30, now),
    }
    removed, radius = redundancy_eviction_candidates(
        index, vectors, radius=0.1, min_neighbors=2, grace_days=7, now=now
    )
    removed_paths = [path for path, _ in removed]
    assert radius == pytest.approx(0.1)
    # 使用率中位数 = (0 + 1/30) / 2：只有从没被用过的两张冗余图会被删，
    # c2 / c4 的使用率高于中位数保留，用得最多的 c0 和不冗余的 lonely 也保留。
    assert sorted(removed_paths) == ["/c1.png", "/c3.png"]


def test_redundancy_eviction_respects_protection_and_grace():
    now = time.time()
    vectors = {f"/p{i}.png": np.array([0.0, i * 0.01], dtype=np.float32) for i in range(4)}
    index = {
        "/p0.png": _meta(0, 1, now),  # 新图保护期内
        "/p1.png": _meta(0, 30, now, is_favorite=True),  # 收藏不删
        "/p2.png": _meta(0, 30, now, character="neko"),  # 角色库不删
        "/p3.png": _meta(0, 30, now, retention_class="external"),  # 外部导入不删
    }
    removed, _ = redundancy_eviction_candidates(
        index, vectors, radius=0.5, min_neighbors=1, grace_days=7, now=now
    )
    assert removed == []


def test_usage_rate_is_per_day_with_minimum_one_day():
    now = time.time()
    assert usage_rate(_meta(10, 5, now), now) == pytest.approx(2.0, rel=1e-3)
    assert usage_rate(_meta(3, 0.1, now), now) == pytest.approx(3.0)
