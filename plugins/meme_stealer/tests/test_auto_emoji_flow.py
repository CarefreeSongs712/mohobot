import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from meme_stealer_core.app import StealerApp as Main
from meme_stealer_core.core.events.event_handler import EventHandler
from meme_stealer_core.core.events.meme_sender_engine import MemeSenderEngine
from meme_stealer_core.host import Image as MessageImage


class DummyEvent:
    def __init__(self, text: str = "hello"):
        self._extras = {}
        self.sent = []
        self._text = text

    def get_extra(self, key=None, default=None):
        if key is None:
            return dict(self._extras)
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_session_id(self):
        return "session-1"

    def get_message_str(self):
        return self._text

    def get_messages(self):
        return []

    async def send(self, message):
        self.sent.append(message)


def _build_main(chance: float) -> Main:
    main = Main.__new__(Main)
    main.plugin_config = types.SimpleNamespace(
        steal_meme=True,
        auto_send_meme=True,
        meme_chance=chance,
        meme_candidate_count=3,
        meme_hint_prompt="",
        meme_query_rewrite=False,
        meme_send_delay=0.0,
        meme_send_char_delay=0.0,
    )

    async def _async_empty_dict():
        return {}

    async def _async_none(_idx=None):
        pass

    main.index_manager = types.SimpleNamespace(
        load_index=_async_empty_dict,
        save_index=_async_none,
        rebuild_index_from_files=_async_empty_dict,
    )
    main.db_service = types.SimpleNamespace(count_total=lambda: 3)
    main.update_config = lambda updates: None
    main.is_send_enabled_for_event = lambda event: True
    main._tools_registered = None
    main._emoji_sender_engine = MemeSenderEngine(main)
    return main


class FakeSelector:
    def __init__(self, results=None):
        self.results = results or []
        self.queries = []
        self.sent = []
        self.recorded = []

    async def semantic_candidates(self, description, *, limit, event=None):
        self.queries.append((description, limit))
        return self.results[:limit]

    def _recently_sent_paths(self):
        return set()

    def is_path_allowed_for_event(self, path, event):
        return True

    async def send_emoji_message(self, event, path):
        self.sent.append(path)
        return "base64_image"

    async def record_emoji_usage(self, path, trigger="auto"):
        self.recorded.append((path, trigger))

    def mark_recently_sent(self, path, data=None):
        pass


class MemeHintTests(unittest.IsolatedAsyncioTestCase):
    async def test_hint_mentions_tools_and_candidate_count(self):
        main = _build_main(1.0)
        event = DummyEvent()

        hint = await main.build_meme_hint(event)

        self.assertIn("search_meme", hint)
        self.assertIn("send_meme", hint)
        self.assertIn("3", hint)
        self.assertTrue(main._emoji_sender_engine.emoji_turn_state(event).is_hint_injected())

    async def test_hint_skipped_when_probability_gate_rejects(self):
        main = _build_main(0.0)
        event = DummyEvent()

        self.assertEqual(await main.build_meme_hint(event), "")
        self.assertEqual(event.get_extra("stealer_auto_emoji_turn_reason"), "chance_zero")

    async def test_hint_skipped_without_meme_tools(self):
        main = _build_main(1.0)
        main._tools_registered = lambda: False
        self.assertEqual(await main.build_meme_hint(DummyEvent()), "")

    async def test_hint_skipped_when_library_empty(self):
        main = _build_main(1.0)
        main.db_service = types.SimpleNamespace(count_total=lambda: 0)
        self.assertEqual(await main.build_meme_hint(DummyEvent()), "")

    async def test_custom_hint_prompt_placeholders(self):
        main = _build_main(1.0)
        main.plugin_config.meme_hint_prompt = "用 {search_tool} 找 {candidate_count} 张，{send_tool} 发"
        self.assertEqual(
            await main.build_meme_hint(DummyEvent()), "用 search_meme 找 3 张，send_meme 发"
        )

    async def test_turn_permission_respects_cooldown(self):
        main = _build_main(1.0)
        event = DummyEvent()
        await main._emoji_sender_engine.mark_auto_emoji_sent(event)
        allowed = await main._emoji_sender_engine.resolve_auto_emoji_turn_permission(event)
        self.assertFalse(allowed)
        self.assertEqual(event.get_extra("stealer_auto_emoji_turn_reason"), "cooldown")

    async def test_cooldown_is_per_bot_in_the_same_group(self):
        main = _build_main(1.0)
        bot_a, bot_b = DummyEvent(), DummyEvent()
        bot_a.bot_id, bot_b.bot_id = "bot_001", "bot_002"
        await main._emoji_sender_engine.mark_auto_emoji_sent(bot_a)
        engine = main._emoji_sender_engine
        self.assertFalse(await engine.is_auto_emoji_cooldown_ready(bot_a))
        self.assertTrue(await engine.is_auto_emoji_cooldown_ready(bot_b))


class MemeToolFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_returns_candidates_from_semantic_recall(self):
        main = _build_main(0.0)
        meta = {"desc": "猫猫瞪眼", "overlay_text": "啊？", "scenes": ["你在说什么"]}
        main.meme_selector = FakeSelector([(__file__, meta, 0.9)] * 5)
        event = DummyEvent()

        text = await main.search_meme(event, "一只惊讶的猫")

        self.assertEqual(main.meme_selector.queries, [("一只惊讶的猫", 3)])
        self.assertIn("啊？", text)
        self.assertIn("send_meme(emoji_id=", text)
        self.assertEqual(len(main._emoji_sender_engine.emoji_turn_state(event).get_candidates()), 3)

    async def test_send_meme_queues_until_reply_then_sends(self):
        main = _build_main(1.0)
        main.meme_selector = FakeSelector([(__file__, {"desc": "x"}, 0.9)])
        main.event_handler = types.SimpleNamespace(
            chat_context=types.SimpleNamespace(record_bot_reply=lambda *_a, **_k: 1)
        )
        created = []

        def _safe_create_task(coro, *, name=""):
            task = asyncio.ensure_future(coro)
            created.append((name, task))
            return task

        main._safe_create_task = _safe_create_task
        event = DummyEvent()
        await main.search_meme(event, "惊讶")
        reply = await main.send_meme(event, 1)

        self.assertIn("文字回复后自动发出", reply)
        self.assertTrue(main.has_queued_meme(event))
        self.assertEqual(main.meme_selector.sent, [])

        dispatched = main.after_reply(event, "好的")
        self.assertTrue(dispatched)
        await created[0][1]
        self.assertEqual(main.meme_selector.sent, [__file__])
        self.assertEqual(main.meme_selector.recorded, [(__file__, "llm_tool")])
        self.assertTrue(main._emoji_sender_engine.emoji_turn_state(event).is_active_sent())

    async def test_response_without_queued_meme_does_nothing(self):
        main = _build_main(1.0)
        main.event_handler = None
        self.assertFalse(main.after_reply(DummyEvent(), "hi"))

    async def test_send_meme_rejects_invalid_ids(self):
        main = _build_main(1.0)
        main.meme_selector = FakeSelector([(__file__, {"desc": "x"}, 0.9)])
        event = DummyEvent()
        self.assertIn("candidate_expired", await main.send_meme(event, 1))
        await main.search_meme(event, "x")
        self.assertIn("invalid_id", await main.send_meme(event, 9))
        self.assertIn("invalid_id", await main.send_meme(event, "abc"))


class FakeStealEventHandler:
    _get_media_ref = staticmethod(EventHandler._get_media_ref)
    _resolve_media_ref = EventHandler._resolve_media_ref

    def _extract_store_emoji_urls(self, _event):
        return []

    def build_chat_context(self, _event):
        return "<chat_context>\nA: 看这个\n</chat_context>"


class StealToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_steal_tool_resolves_current_message_image_when_ref_empty(self):
        main = _build_main(1.0)

        class TestImage(MessageImage):
            pass

        image = TestImage()
        image.url = "https://example.test/a.png"

        class ImageEvent(DummyEvent):
            def get_messages(self):
                return [image]

        image_ref, source = await main._resolve_steal_image_ref(
            ImageEvent(), "", FakeStealEventHandler()
        )
        self.assertEqual(image_ref, "https://example.test/a.png")
        self.assertEqual(source, "llm_tool")

    async def test_steal_tool_resolves_relative_path_to_message_image(self):
        """issue #88: LLM 传 ./image.png 相对路径时应当映射回当前消息中的真实 URL。"""
        main = _build_main(1.0)

        class TestImage(MessageImage):
            pass

        image = TestImage()
        image.url = "https://example.test/abc-123.png"

        class ImageEvent(DummyEvent):
            def get_messages(self):
                return [image]

        for ref in (
            "./abc-123.png",
            "abc-123.png",
            ".\\abc-123.png",
            "https://example.test/abc-123.png",
        ):
            resolved, source = await main._resolve_steal_image_ref(
                ImageEvent(), ref, FakeStealEventHandler()
            )
            if ref.startswith("http"):
                self.assertEqual(resolved, ref)
            else:
                self.assertEqual(resolved, "https://example.test/abc-123.png")
            self.assertEqual(source, "llm_tool")

    async def test_steal_tool_resolves_absolute_path_via_convert_to_file_path(self):
        main = _build_main(1.0)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fp:
            local_path = fp.name
            fp.write(b"fake png")

        try:

            class TestImage(MessageImage):
                async def convert_to_file_path(self_inner):
                    return local_path

            image = TestImage()
            image.url = "https://example.test/x.png"
            image.file = local_path
            image.path = local_path

            class ImageEvent(DummyEvent):
                def get_messages(self):
                    return [image]

            resolved, source = await main._resolve_steal_image_ref(
                ImageEvent(), Path(local_path).name, FakeStealEventHandler()
            )
            self.assertEqual(resolved, local_path)
            self.assertEqual(source, "llm_tool")
        finally:
            Path(local_path).unlink(missing_ok=True)

    async def test_steal_tool_keeps_relative_path_when_no_image_in_message(self):
        main = _build_main(1.0)
        resolved, source = await main._resolve_steal_image_ref(
            DummyEvent(), "./orphan.png", FakeStealEventHandler()
        )
        self.assertEqual(resolved, "./orphan.png")
        self.assertEqual(source, "llm_tool")

    async def test_steal_tool_passes_origin_metadata_and_context_to_processor(self):
        main = _build_main(1.0)
        main.is_steal_enabled_for_event = lambda event: True
        main.get_event_target = lambda event: ("group", "123")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fp:
            temp_path = fp.name

        class DownloadingHandler(FakeStealEventHandler):
            async def _download_to_temp(self, _url, *, log_download=False):
                return temp_path, False

        captured = {}
        saved = []
        main._get_event_handler = lambda **kwargs: DownloadingHandler()
        main._precheck_image_file = lambda path: (True, "")

        async def _load_index():
            return {}

        async def _save_index(idx):
            saved.append(idx)

        async def _process_image(event, file_path, is_temp=False, extra_meta=None, **kwargs):
            captured["extra_meta"] = dict(extra_meta or {})
            captured["chat_context"] = kwargs.get("chat_context")
            return True, {
                file_path: {
                    "category": "uncategorized",
                    "overlay_text": "啊？",
                    "tags": ["tag"],
                    "desc": "desc",
                    "scenes": ["scene"],
                }
            }

        main.index_manager.load_index = _load_index
        main.index_manager.save_index = _save_index
        main._process_image = _process_image

        try:
            result = await main.steal_meme(DummyEvent(), "https://example.test/a.png")
        finally:
            Path(temp_path).unlink(missing_ok=True)

        self.assertIn("偷取成功", result)
        self.assertIn("啊？", result)
        self.assertEqual(captured["extra_meta"]["origin_target"], "group:123")
        self.assertEqual(captured["extra_meta"]["origin_url"], "https://example.test/a.png")
        self.assertEqual(captured["extra_meta"]["source"], "llm_tool")
        self.assertIn("看这个", captured["chat_context"])
        self.assertTrue(saved)
