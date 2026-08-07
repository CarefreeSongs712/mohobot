"""Minimal plugin system — scans a plugins/ directory for Python files
with a 'Plugin' class and hooks into notice/meta events.

Each plugin file should define:
    class Plugin:
        async def on_notice(self, bot_id: str, event, raw_event) -> None: ...
        async def on_meta(self, bot_id: str, event, raw_event) -> None: ...
        async def on_message(self, bot_id: str, event, raw_event) -> tuple[bool, str | None]: ...
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from mohobot.interceptors.base import Interceptor
from mohobot.models.onebot import (
    Event,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
)


class PluginSystem(Interceptor):
    """Loads and manages plugins from a designated directory."""

    def __init__(self, plugins_dir: str = "./plugins"):
        self._plugins_dir = Path(plugins_dir)
        self._plugins: list[Any] = []  # Plugin instances

    async def load_plugins(self) -> int:
        """Scan plugins directory and load all plugins."""
        self._plugins.clear()

        if not self._plugins_dir.exists():
            self._plugins_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created plugins directory: {self._plugins_dir}")
            return 0

        # Add plugins dir to sys.path
        plugins_path = str(self._plugins_dir.absolute())
        if plugins_path not in sys.path:
            sys.path.insert(0, plugins_path)

        count = 0
        for entry in sorted(self._plugins_dir.iterdir()):
            if entry.suffix != ".py" or entry.name.startswith("_"):
                continue
            if entry.name == "__init__.py":
                continue

            try:
                module_name = entry.stem
                spec = importlib.util.spec_from_file_location(module_name, entry)
                if spec is None or spec.loader is None:
                    continue

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Find Plugin class in module
                for name, cls in inspect.getmembers(module, inspect.isclass):
                    if name == "Plugin":
                        plugin = cls()
                        self._plugins.append(plugin)
                        count += 1
                        logger.info(f"Loaded plugin: {module_name}.{name}")
                        break
            except Exception as e:
                logger.error(f"Failed to load plugin {entry.name}: {e}")

        logger.info(f"Plugin system loaded {count} plugin(s)")
        return count

    async def dispatch_notice(self, bot_id: str, event: NoticeEvent, raw: dict) -> None:
        """Dispatch a notice event to all plugins."""
        for plugin in self._plugins:
            try:
                handler = getattr(plugin, "on_notice", None)
                if handler:
                    await handler(bot_id, event, raw)
            except Exception as e:
                logger.error(f"Plugin notice handler error: {e}")

    async def dispatch_meta(self, bot_id: str, event: MetaEvent, raw: dict) -> None:
        """Dispatch a meta event to all plugins."""
        for plugin in self._plugins:
            try:
                handler = getattr(plugin, "on_meta", None)
                if handler:
                    await handler(bot_id, event, raw)
            except Exception as e:
                logger.error(f"Plugin meta handler error: {e}")

    async def intercept(
        self,
        bot_id: str,
        event: MessageEvent,
        raw_event: dict[str, Any],
    ) -> tuple[bool, str | list[dict[str, Any]] | None]:
        """Give plugins a chance to intercept messages."""
        for plugin in self._plugins:
            try:
                handler = getattr(plugin, "on_message", None)
                if handler:
                    handled, response = await handler(bot_id, event, raw_event)
                    if handled:
                        return (True, response)
            except Exception as e:
                logger.error(f"Plugin message handler error: {e}")
        return (False, None)