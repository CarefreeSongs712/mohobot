"""Offline wiring smoke: temporary data, fake servers/LLM, no real bot or keys."""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from main import MohobotApplication
from mohobot.models.config import GlobalConfig


async def main():
    with tempfile.TemporaryDirectory(prefix="mohobot_smoke_") as directory:
        root = Path(directory)
        cfg = GlobalConfig()
        cfg.data_dir = str(root / "data")
        cfg.log_dir = str(root / "logs")
        cfg.plugins_dir = str(root / "plugins")
        cfg.database.folder = str(root / "database")
        cfg.interceptor.keyword_file = str(root / "keywords.json")
        cfg.music_knowledge = {"enabled": False}
        cfg.web_panel.enabled = False
        cfg.context_summary_sweep_enabled = False
        path = root / "config.yaml"
        cfg.save(path)
        app = MohobotApplication(config_path=str(path))
        with patch.object(MohobotApplication, "_maybe_start_review_panel"), patch("mohobot.llm_service.LLMService._provider_identities", return_value={"chat": None, "vision": None, "emotion": None}), patch("mohobot.ws_server.WSServer.start", new=AsyncMock()), patch("mohobot.ws_server.WSServer.stop", new=AsyncMock()):
            try:
                await app.startup()
                assert app._database_manager is not None
                assert app._message_handler._persona_ab is app._persona_ab_service
                assert app._persona_ab_service.config.enabled is False
                print("startup OK (offline, temporary data, no sockets)")
            finally:
                await app.shutdown()
                if app._database_manager is not None:
                    app._database_manager._engine.dispose()
        print("shutdown OK")


if __name__ == "__main__":
    asyncio.run(main())
