"""Qzone 统一 bot 人设隔离测试, FakeLLM + 临时 data, 不联网。"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _REPO / "plugins" / "qzone"
sys.path.insert(0, str(_REPO))
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from qzone_core.model import Post


def _load_plugin_class():
    # 与其它 qzone 测试使用不同 module, 防止类级注入互相污染。
    spec = importlib.util.spec_from_file_location("mohobot_plugin_qzone_persona_test", _PLUGIN_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Plugin


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.resolved_configs = []
        self.vision_calls = []
        self.presets = {"persona_001": "BOT_DEFAULT_A", "persona_002": "BOT_DEFAULT_B"}
        self.private_binding = {("bot_b", "2002", "sess_001"): "DO_NOT_USE_PRIVATE_PERSONA"}
        self.response = "自然生成的回复"

    def resolve_bot_persona(self, config):
        self.resolved_configs.append(config)
        return {"id": config.persona_id, "name": "预设", "content": self.presets[config.persona_id], "source": "bot"}

    async def complete_text(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return self.response

    async def describe_image(self, url):
        self.vision_calls.append(url)
        return "客观图片描述"


class QzonePersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = _load_plugin_class()()
        self.plugin._data_dir = self.temp.name
        self.plugin._emotion_manager = None
        self.plugin._admin_ids = []
        self.config_a = SimpleNamespace(bot_id="bot_a", nickname="甲", persona_id="persona_001", persona="LEGACY_A")
        self.config_b = SimpleNamespace(bot_id="bot_b", nickname="乙", persona_id="persona_002", persona="LEGACY_B")
        self.bots = {
            "bot_a": SimpleNamespace(config=self.config_a, qq=11),
            "bot_b": SimpleNamespace(config=self.config_b, qq=22),
        }
        self.plugin._ws_server = SimpleNamespace(_bot_manager=SimpleNamespace(get=self.bots.get))
        self.llm = FakeLLM()
        self.plugin._llm_service = self.llm

    async def test_auto_reply_and_publish_use_bot_default_not_private_override(self):
        post = Post(uin=2002, tid="t1", name="好友", text="日常内容")
        await self.plugin._generate_auto_text("bot_b", "好友", "你好", post)
        await self.plugin._generate_publish_text("bot_b", "猫:跑酷")
        self.assertEqual(len(self.llm.calls), 2)
        self.assertTrue(all(config is self.config_b for config in self.llm.resolved_configs))
        for prompt, kwargs in self.llm.calls:
            self.assertIn("BOT_DEFAULT_B", kwargs["system_prompt"])
            self.assertNotIn("BOT_DEFAULT_A", kwargs["system_prompt"])
            self.assertNotIn("LEGACY_B", kwargs["system_prompt"])
            self.assertNotIn("DO_NOT_USE_PRIVATE_PERSONA", kwargs["system_prompt"])
            self.assertEqual(kwargs["module"], "qzone")

    async def test_default_selection_and_preset_content_hot_updates(self):
        self.assertEqual(self.plugin._bot_persona("bot_b"), "BOT_DEFAULT_B")
        self.config_b.persona_id = "persona_001"
        self.assertEqual(self.plugin._bot_persona("bot_b"), "BOT_DEFAULT_A")
        self.llm.presets["persona_001"] = "HOT_UPDATED_PRESET"
        self.assertEqual(self.plugin._bot_persona("bot_b"), "HOT_UPDATED_PRESET")
        await self.plugin._generate_auto_text("bot_b", "好友", "你好", Post(uin=1, tid="t"))
        await self.plugin._generate_publish_text("bot_b", None)
        for _, kwargs in self.llm.calls:
            self.assertIn("HOT_UPDATED_PRESET", kwargs["system_prompt"])
        self.assertEqual(len(self.llm.resolved_configs), 5)

    async def test_empty_resolved_persona_does_not_fall_back_to_legacy(self):
        self.llm.presets["persona_002"] = ""
        self.assertEqual(self.plugin._bot_persona("bot_b"), "")
        await self.plugin._generate_publish_text("bot_b", None)
        self.assertNotIn("LEGACY_B", self.llm.calls[0][1]["system_prompt"])

    async def test_legacy_fake_without_resolver_can_read_inline_persona(self):
        class OldFakeLLM:
            def __init__(self):
                self.calls = []

            async def complete_text(self, prompt, **kwargs):
                self.calls.append(kwargs)
                return "独立测试生成内容"

        fake = OldFakeLLM()
        self.plugin._llm_service = fake
        self.config_b.persona = "  LEGACY_B  "
        self.assertEqual(self.plugin._bot_persona("bot_b"), "LEGACY_B")
        await self.plugin._generate_auto_text("bot_b", "好友", "你好", Post(uin=1, tid="t"))
        await self.plugin._generate_publish_text("bot_b", None)
        self.assertTrue(all("LEGACY_B" in kw["system_prompt"] for kw in fake.calls))
        self.plugin._llm_service = None
        self.assertEqual(self.plugin._bot_persona("bot_b"), "LEGACY_B")

    async def test_missing_bot_or_injection_is_safe(self):
        self.assertEqual(self.plugin._bot_persona("missing"), "")
        self.assertEqual(self.llm.resolved_configs, [])
        self.plugin._ws_server = None
        self.assertEqual(self.plugin._bot_persona("bot_b"), "")
        self.plugin._ws_server = SimpleNamespace()
        self.assertEqual(self.plugin._bot_persona("bot_b"), "")

    async def test_topic_extraction_and_vision_remain_objective(self):
        self.plugin._emotion_manager = SimpleNamespace(
            recent_interactions=lambda bot_id, limit: [{"user_msg": "聊猫", "ai_response": "猫跑酷"}])
        self.llm.response = '["猫:跑酷"]'
        topics = await self.plugin._extract_topics("bot_b")
        self.assertEqual(topics, ["猫:跑酷"])
        self.assertEqual(self.llm.calls[0][1]["system_prompt"], "你是话题提炼助手, 只输出 JSON 数组。")
        self.assertEqual(self.llm.resolved_configs, [])
        self.plugin.plugin_config["analyze_images_on_view_feed"] = True
        post = Post(uin=2002, tid="t1", images=["fake://image"])
        await self.plugin._analyze_post_images(post)
        self.assertEqual(post.extra_text, "客观图片描述")
        self.assertEqual(self.llm.vision_calls, ["fake://image"])
        self.assertEqual(self.llm.resolved_configs, [])


def test_qzone_persona_suite():
    import threading
    results = []
    def run():
        results.append(unittest.TextTestRunner(verbosity=0).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(QzonePersonaTests)))
    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert results and results[0].wasSuccessful(), "QQ空间人设回归失败"


if __name__ == "__main__":
    unittest.main()
