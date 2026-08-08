#!/usr/bin/env python3
"""Mohobot — Multi-Bot AI Framework (OneBot v11 Reverse WebSocket).

Entry point: initializes all services and starts the WebSocket server + web panel.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from loguru import logger

from mohobot import __version__
from mohobot.bot_manager import BotManager
from mohobot.context_manager import ContextManager
from mohobot.file_store import ensure_dir
from mohobot.image_cache import ImageCache
from mohobot.interceptors.command_handler import CommandHandler
from mohobot.interceptors.keyword_filter import KeywordFilter
from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.llm_service import LLMService
from mohobot.message_handler import MessageHandler
from mohobot.models.config import GlobalConfig
from mohobot.utils.logger import setup_logger
from mohobot.web_panel.app import WebPanel
from mohobot.ws_server import WSServer


class MohobotApplication:
    """Main application container — wires all components together."""

    def __init__(self, config_path: str = "./config/global.yaml"):
        self._config_path = config_path
        self._config: GlobalConfig | None = None
        self._bot_manager: BotManager | None = None
        self._ws_server: WSServer | None = None
        self._context_manager: ContextManager | None = None
        self._llm_service: LLMService | None = None
        self._image_cache: ImageCache | None = None
        self._message_handler: MessageHandler | None = None
        self._plugin_system: PluginSystem | None = None
        self._web_panel: WebPanel | None = None
        self._running = False

    async def startup(self) -> None:
        """Initialize all components and start servers."""
        logger.info(f"Mohobot v{__version__} starting up...")

        # 1. Load config
        self._config = GlobalConfig.load(self._config_path)

        # 2. Ensure data directories exist
        await ensure_dir(self._config.data_dir)
        await ensure_dir(f"{self._config.data_dir}/bots")
        await ensure_dir(f"{self._config.data_dir}/history")
        await ensure_dir(f"{self._config.data_dir}/contexts")
        await ensure_dir(f"{self._config.data_dir}/cache/images")
        await ensure_dir(self._config.plugins_dir)

        # 3. Initialize core services
        self._bot_manager = BotManager(data_dir=self._config.data_dir)
        self._context_manager = ContextManager(data_dir=self._config.data_dir)
        self._llm_service = LLMService(global_config=self._config)
        self._image_cache = ImageCache(cache_dir=f"{self._config.data_dir}/cache")
        self._plugin_system = PluginSystem(plugins_dir=self._config.plugins_dir)

        # 4. Load plugins
        plugin_count = await self._plugin_system.load_plugins()

        # Inject bot manager into plugins that need it (e.g. status plugin)
        from plugins.status import inject_bot_manager as inject_bm
        inject_bm(self._bot_manager)

        logger.info(f"Loaded {plugin_count} plugin(s)")

        # 5. Initialize message handler
        self._message_handler = MessageHandler(
            ws_server=None,  # Will be set after WS server creation
            context_manager=self._context_manager,
            llm_service=self._llm_service,
            plugin_system=self._plugin_system,
            data_dir=self._config.data_dir,
            context_max_rounds=self._config.context_max_rounds,
        )

        # 6. Set up interceptors
        command_handler = CommandHandler(
            context_manager=self._context_manager,
            llm_service=self._llm_service,
            ws_server=None,  # Will be set after WS server creation
        )
        keyword_filter = KeywordFilter()
        interceptors = [command_handler, keyword_filter, self._plugin_system]
        self._message_handler.set_interceptors(interceptors)

        # 7. Initialize WebSocket server
        self._ws_server = WSServer(
            bot_manager=self._bot_manager,
            host=self._config.server.host,
            port=self._config.server.port,
            max_size=self._config.server.max_size,
        )

        # Wire up circular references
        self._message_handler._ws = self._ws_server
        command_handler._ws = self._ws_server

        # Set event callback from WS server to message handler
        self._ws_server.set_event_callback(self._message_handler.handle_event)

        # 8. Start servers
        await self._ws_server.start()

        # 9. Start web panel
        if self._config.web_panel.enabled:
            self._web_panel = WebPanel(
                host=self._config.web_panel.host,
                port=self._config.web_panel.port,
                username=self._config.web_panel.username,
                password_hash=self._config.web_panel.password_hash,
                data_dir=self._config.data_dir,
            )
            # Start web panel in background
            asyncio.create_task(self._run_web_panel())

        self._running = True
        logger.info(
            f"Mohobot v{__version__} is running! "
            f"WS: ws://{self._config.server.host}:{self._config.server.port} | "
            f"Panel: http://{self._config.web_panel.host}:{self._config.web_panel.port}"
        )

    async def _run_web_panel(self) -> None:
        """Run the web panel (wraps uvicorn)."""
        try:
            await self._web_panel.start()
        except Exception as e:
            logger.error(f"Web panel failed: {e}")

    async def shutdown(self) -> None:
        """Gracefully shut down all components."""
        logger.info("Shutting down Mohobot...")
        self._running = False

        # Stop web panel
        if self._web_panel:
            await self._web_panel.stop()

        # Stop WS server
        if self._ws_server:
            await self._ws_server.stop()

        # Close LLM client
        if self._llm_service:
            await self._llm_service.close()

        # Close image cache HTTP client
        if self._image_cache:
            await self._image_cache.close()

        # Close file writers
        if self._message_handler:
            await self._message_handler.close()

        logger.info("Mohobot shutdown complete.")

    async def run_forever(self) -> None:
        """Run until a shutdown signal is received."""
        stop_event = asyncio.Event()

        def _signal_handler():
            logger.info("Received shutdown signal")
            stop_event.set()

        # Platform-specific signal handling
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _signal_handler)
                except (NotImplementedError, RuntimeError):
                    pass
        else:
            # Windows (Python 3.8+): SIGINT / SIGTERM / SIGBREAK supported
            sigs = [signal.SIGINT, signal.SIGTERM]
            if hasattr(signal, "SIGBREAK"):
                sigs.append(signal.SIGBREAK)
            for sig in sigs:
                try:
                    loop.add_signal_handler(sig, _signal_handler)
                except (NotImplementedError, RuntimeError):
                    pass

        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
            await self.shutdown()


def main():
    """Entry point."""
    config_path = os.environ.get("MOHOBOT_CONFIG", "./config/global.yaml")

    # Set up logging first
    setup_logger(log_dir="./logs")

    app = MohobotApplication(config_path=config_path)

    async def _run() -> None:
        # IMPORTANT: startup + run_forever must share ONE event loop.
        # Using two separate asyncio.run() calls kills all servers when
        # the first loop closes (the web panel task is cancelled mid-startup
        # and the WS server is bound to a dead loop).
        await app.startup()
        await app.run_forever()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()