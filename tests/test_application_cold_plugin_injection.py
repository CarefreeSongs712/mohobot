"""Cold plugin activation runs after host wiring but before event admission."""

import asyncio
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, Mock

from mohobot.bot_manager import BotManager
from mohobot.llm_service import LLMService
from mohobot.models.config import GlobalConfig
from mohobot.ws_server import WSServer


_MISSING = object()


@contextmanager
def _patched(*patches):
    """临时替换属性并在退出时还原。

    不用 pytest fixture: 项目自研 legacy runner 只支持无参或仅 ``tmp_path``
    的用例入口, 而 CI 在 push main 时仍会执行该 runner。

    还原时使用 ``vars(owner)[name]`` 的原始值: 类上可能有 staticmethod/
    classmethod 描述符, 用 getattr 还原会把描述符解析成普通函数, 影响
    后续用例。
    """
    undo = []
    try:
        for obj, name, value in patches:
            undo.append((obj, name, vars(obj).get(name, _MISSING)))
            setattr(obj, name, value)
        yield
    finally:
        for obj, name, old in reversed(undo):
            if old is _MISSING:
                delattr(obj, name)
            else:
                setattr(obj, name, old)


async def _check_activation(tmp_path: Path, startup_fails: bool) -> None:
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

    with _patched(
        (GlobalConfig, "load", Mock(return_value=cfg)),
        (BotManager, "migrate_legacy_bots", Mock()),
        (module.MohobotApplication, "_maybe_start_review_panel", Mock()),
        (LLMService, "_provider_identities", Mock(
            return_value=dict.fromkeys(("chat", "vision", "emotion")),
        )),
        (WSServer, "start", record_listen),
        (WSServer, "stop", AsyncMock()),
    ):
        app = module.MohobotApplication(config_path=str(tmp_path / "unused.yaml"))
        try:
            await app.startup()
            assert listened == [app._ws_server]
            if not startup_fails:
                assert await app._plugin_system.reload_plugin(name)
                assert_plugin_refs()
        finally:
            await app.shutdown()


def test_installed_plugin_activation_before_websocket_listens(tmp_path):
    for startup_fails in (False, True):
        case_dir = tmp_path / ("startup-fails" if startup_fails else "startup-ok")
        case_dir.mkdir()
        asyncio.run(_check_activation(case_dir, startup_fails))
