"""Cold plugin activation runs after host wiring but before event admission."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, Mock

import pytest

from mohobot.bot_manager import BotManager
from mohobot.llm_service import LLMService
from mohobot.models.config import GlobalConfig
from mohobot.ws_server import WSServer


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_fails", [False, True])
async def test_installed_plugin_activation_before_websocket_listens(tmp_path, monkeypatch, startup_fails):
    entry = Path(__file__).resolve().parents[1] / "main.py"
    spec = spec_from_file_location("_cold_plugin_application_test", entry)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    cfg = GlobalConfig()
    cfg.data_dir = str(tmp_path / "data")
    cfg.plugins_dir = str(tmp_path / "builtin")
    cfg.music_knowledge = {"enabled": False}
    cfg.context_summary_sweep_enabled = False
    for section in (cfg.web_panel, cfg.database, cfg.knowledge, cfg.news, cfg.emotion, cfg.tts, cfg.anysearch):
        section.enabled = False

    name = "cold_runtime_injection_probe"
    plugin_dir = Path(cfg.data_dir) / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "metadata.yaml").write_text(dedent(f"""\
        name: {name}
        author: Test
        display_name: Cold runtime injection probe
        version: 1.0.0
        description: Offline application assembly fixture
        repo: https://github.com/TestFixture/offline-plugin
        license: MIT
        api_version: 1
    """), encoding="utf-8")
    (plugin_dir / "main.py").write_text(dedent(f"""\
        class Plugin:
            ws_injections = []
            handler_injections = []

            @classmethod
            def inject_ws_server(cls, value):
                cls.ws_injections.append(value)

            @classmethod
            def inject_message_handler(cls, value):
                cls.handler_injections.append(value)

            async def on_startup(self):
                if not self.ws_injections or not self.handler_injections:
                    raise RuntimeError("runtime references unavailable in startup")
                self.startup_refs = (tuple(self.ws_injections), tuple(self.handler_injections))
                if {startup_fails!r}:
                    raise RuntimeError("fixture startup failure after injection")
    """), encoding="utf-8")

    monkeypatch.setattr(GlobalConfig, "load", Mock(return_value=cfg))
    monkeypatch.setattr(BotManager, "migrate_legacy_bots", Mock())
    monkeypatch.setattr(module.MohobotApplication, "_maybe_start_review_panel", Mock())
    monkeypatch.setattr(LLMService, "_provider_identities", Mock(return_value=dict.fromkeys(("chat", "vision", "emotion"))))
    monkeypatch.setattr(WSServer, "stop", AsyncMock())
    app = module.MohobotApplication(config_path=str(tmp_path / "unused.yaml"))
    listened = []

    async def record_listen(server):
        assert server is app._ws_server
        listened.append(server)
        meta = next(item for item in app._plugin_system.list_plugins() if item["name"] == name)
        # loaded 语义随生命周期修复变化（#149/#130）：失败候选可能保持实例但未就绪。
        if startup_fails:
            assert meta["error"] == "fixture startup failure after injection"
        else:
            assert meta["loaded"] is True
            assert meta["error"] is None
            assert_plugin_refs()

    def assert_plugin_refs():
        meta = next(item for item in app._plugin_system._plugins if item["name"] == name)
        instance = meta["instance"]
        assert instance.startup_refs == ((app._ws_server,), (app._message_handler,))
        assert instance.ws_injections == [app._ws_server]
        assert instance.handler_injections == [app._message_handler]

    monkeypatch.setattr(WSServer, "start", record_listen)
    try:
        await app.startup()
        assert listened == [app._ws_server]
        if not startup_fails:
            assert await app._plugin_system.reload_plugin(name)
            assert_plugin_refs()
    finally:
        await app.shutdown()
