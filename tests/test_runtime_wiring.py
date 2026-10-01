"""管理鉴权、配置热更新与主识图装配的隔离回归测试。"""

import asyncio
import base64
import io
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from PIL import Image

from main import MohobotApplication
from mohobot.bot_manager import BotManager
from mohobot.models.config import GlobalConfig
from mohobot.models.onebot import PrivateMessageEvent, Sender
from mohobot.web_panel.app import WebPanel


async def test_unbind_requires_auth_before_mutation():
    with tempfile.TemporaryDirectory() as td:
        cfg = GlobalConfig(data_dir=td)
        cfg.web_panel.password_hash = WebPanel._hash_password("test-password")
        path = Path(td) / "global.yaml"
        cfg.save(path)
        manager = BotManager(data_dir=td)
        bot = manager.create_bot(nickname="test", qq=1001)
        panel = WebPanel(config_path=str(path), data_dir=td,
                         password_hash=cfg.web_panel.password_hash, bot_manager=manager)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel._app),
                                         base_url="http://test") as client:
                url = f"/api/bots/{bot.bot_id}/unbind"
                panel._tokens["expired"] = time.time() - 1
                for headers in ({}, {"Authorization": "Bearer invalid"},
                                {"Authorization": "Bearer expired"}):
                    response = await client.post(url, headers=headers)
                    assert response.status_code == 401, response.text
                    assert manager.list_bot_configs()[0].qq == 1001
                panel._tokens["valid"] = time.time() + 60
                response = await client.post(url, headers={"Authorization": "Bearer valid"})
                assert response.status_code == 200, response.text
                assert manager.list_bot_configs()[0].qq == 0
        finally:
            await panel.stop()


class _FakeClients:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.closed = False

    async def close(self):
        self.closed = True


async def _start_isolated(td):
    cfg = GlobalConfig(data_dir=str(Path(td) / "data"), plugins_dir=str(Path(td) / "plugins"))
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    cfg.web_panel.enabled = False
    cfg.database.enabled = False
    cfg.music_knowledge = {"enabled": False}
    cfg.anysearch.enabled = False
    cfg.llm.chat_api_key = "test-key"
    cfg.llm.chat_base_url = "http://127.0.0.1:1/v1"
    cfg.llm.vision_base_url = cfg.llm.chat_base_url
    path = Path(td) / "global.yaml"
    cfg.save(path)
    app = MohobotApplication(str(path))
    with patch.object(MohobotApplication, "_maybe_start_review_panel", return_value=None), \
            patch("mohobot.llm_service.AsyncOpenAI", _FakeClients):
        await app.startup()
    return app, path


async def test_web_config_changes_live_components():
    with tempfile.TemporaryDirectory() as td:
        app, path = await _start_isolated(td)
        cfg = app._config
        cfg_refs = (app._message_handler._global_config, app._ws_server._global_config,
                    app._llm_service._cfg)
        reply_ref = cfg.reply
        panel = WebPanel(config_path=str(path), data_dir=cfg.data_dir,
                         password_hash=WebPanel._hash_password("test-password"),
                         config_update_callback=app.sync_config,
                         context_manager=app._context_manager,
                         llm_service=app._llm_service,
                         ban_filter=app._message_handler._interceptors[0],
                         plugin_system=app._plugin_system)
        panel._tokens["test"] = time.time() + 60
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel._app),
                                         base_url="http://test",
                                         headers={"Authorization": "Bearer test"}) as client:
                response = await client.put("/api/config", json={"data": {
                    "llm_excluded_groups": [1002], "ignore_auto_reply": False,
                    "history_dual_write": False, "group_recent_msgs_count": 0,
                    "admins": [3001], "touch_replies": ["new reply"],
                    "usage_excluded_models": ["excluded"],
                    "reply": {"stream": False, "segment_max_len": 100,
                              "reply_quote": False},
                    "context_trim_at_rounds": 50,
                }})
                assert response.status_code == 200, response.text
                assert all(reference is cfg for reference in cfg_refs)
                assert cfg.reply is reply_ref
                handler = app._message_handler
                assert handler._group_llm_excluded(1002)
                assert not handler._ignore_auto_reply_enabled()
                assert not cfg.history_dual_write
                assert handler._group_recent_count == 0
                assert handler._seg_max_len == 100 and not handler._stream
                assert not handler._reply_quote
                assert handler._command_handler._admins == {"3001"}
                assert cfg.touch_replies == ["new reply"]
                assert app._llm_service._cfg.usage_excluded_models == ["excluded"]
                response = await client.put("/api/models", json={"data": {
                    "chat": {"model": "new-model", "temperature": 0.2},
                    "vision": {"prompt": "new vision prompt"},
                }})
                assert response.status_code == 200, response.text
                assert app._llm_service._cfg.llm.chat_model == "new-model"
                assert app._llm_service._cfg.llm.chat_temperature == 0.2
                assert cfg.llm.vision_prompt == "new vision prompt"
                response = await client.put("/api/tts/config", json={"data": {
                    "tts_prompt_template": "new tts prompt", "enabled": False,
                }})
                assert response.status_code == 200, response.text
                assert cfg.tts.tts_prompt_template == "new tts prompt"
                disk = GlobalConfig.load(path)
                assert disk.llm_excluded_groups == [1002]
                assert disk.llm.chat_model == "new-model"
        finally:
            await panel.stop()
            await app.shutdown()


async def test_main_image_cache_shared_with_llm_and_context():
    with tempfile.TemporaryDirectory() as td:
        app, _ = await _start_isolated(td)
        try:
            assert app._llm_service._image_cache is app._image_cache
            assert app._message_handler._image_cache is app._image_cache
            buffer = io.BytesIO()
            Image.new("RGB", (16, 16), "red").save(buffer, format="PNG")
            uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
            calls = []

            async def describe(path, max_tokens=None):
                calls.append(path)
                await asyncio.sleep(0.01)
                return "一张红色图片"

            app._llm_service.describe_image_file = describe
            results = await asyncio.gather(*(
                app._llm_service._describe_image_for_text(uri) for _ in range(3)
            ))
            assert results == ["一张红色图片"] * 3
            assert len(calls) == 1
            assert await app._llm_service._describe_image_for_text(uri) == "一张红色图片"
            assert len(calls) == 1
            event = PrivateMessageEvent(
                time=1, self_id=1001, post_type="message", message_type="private",
                message_id=1, user_id=2001, sender=Sender(user_id=2001, nickname="test"),
                message=[{"type": "image", "data": {"url": uri}}],
            )
            content = await app._message_handler._build_user_store_content("bot_001", event, "")
            assert "红色图片" in content
        finally:
            await app.shutdown()


async def main():
    for name, target in sorted(globals().items()):
        if name.startswith("test_"):
            await target()
            print(f"PASS {name}")


if __name__ == "__main__":
    asyncio.run(main())
