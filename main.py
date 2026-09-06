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
from typing import Any

from loguru import logger

from mohobot import __version__
from mohobot.bot_manager import BotManager
from mohobot.context_manager import ContextManager
from mohobot.db.database_manager import DatabaseManager
from mohobot.file_store import ensure_dir
from mohobot.image_cache import ImageCache
from mohobot.interceptors.command_handler import CommandHandler
from mohobot.interceptors.keyword_filter import KeywordFilter
from mohobot.interceptors.plugin_system import PluginSystem
from mohobot.llm_service import LLMService
from mohobot.services.usage import UsageRecorder
from mohobot.message_handler import MessageHandler
from mohobot.models.config import GlobalConfig
from mohobot.services.task_supervisor import TaskSupervisor
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
        self._database_manager: DatabaseManager | None = None
        self._usage_recorder: UsageRecorder | None = None
        self._song_matcher: Any | None = None
        self._song_info_service: Any | None = None
        self._tts_service: Any | None = None
        self._task_supervisor = TaskSupervisor()
        self._running = False
        self._shutting_down = False

    async def startup(self) -> None:
        """Initialize all components and start servers."""
        if self._task_supervisor.stopping:
            self._task_supervisor = TaskSupervisor()
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
        # 旧格式迁移: data/bots/{qq} (无 bot_id) → 自动编号 bot_id 目录
        self._bot_manager.migrate_legacy_bots()
        self._context_manager = ContextManager(data_dir=self._config.data_dir)
        self._usage_recorder = UsageRecorder(self._config.data_dir)

        # 全局歌曲知识库(识别 + LLM 前注入)。
        # music_knowledge 未启用/初始化失败 → 降级(正常聊天不受影响)。
        self._song_matcher: SongInfoMatcher | None = None
        music_cfg = (self._config.music_knowledge or {})
        if music_cfg.get("enabled", True):
            try:
                from mohobot.music_knowledge import SongInfoMatcher, SongInfoService
                song_db_cfg = music_cfg.get("song_database") or {}
                db_folder = song_db_cfg.get("db_folder", "./data/song_knowledge")
                db_file = song_db_cfg.get("db_file", "knowledge_db.db")
                self._song_matcher = SongInfoMatcher(
                    db_folder=db_folder, db_file=db_file,
                )
                self._song_info_service: SongInfoService | None = SongInfoService(music_cfg)
                if not self._song_matcher._index:
                    logger.warning("歌曲知识库为空, 歌曲识别将在 VCPedia 同步完成前不可用")
                logger.info("全局歌曲知识库已加载(SongInfoMatcher + SongInfoService)")
            except Exception as e:
                self._song_matcher = None
                self._song_info_service = None
                logger.warning(f"全局歌曲知识库初始化失败, 已降级: {e}")
        else:
            self._song_matcher = None
            self._song_info_service = None

        self._llm_service = LLMService(
            global_config=self._config,
            usage_recorder=self._usage_recorder,
            song_annotator=self._make_song_annotator(),
        )
        # 上下文 AI 总结压缩: 注入总结回调 + trim/时间压缩配置(WebUI 保存后可热同步)
        self._context_manager.set_summarizer(self._llm_service.summarize_context)
        self._context_manager.set_trim_config(
            enabled=self._config.context_summary_enabled,
            at_rounds=self._config.context_trim_at_rounds,
            remove_rounds=self._config.context_trim_remove_rounds,
            age_hours=self._config.context_summary_age_hours,
            sweep_enabled=self._config.context_summary_sweep_enabled,
            sweep_interval_minutes=self._config.context_summary_sweep_interval_minutes,
            min_interval_hours=self._config.context_summary_min_interval_hours,
        )
        # 全局情感系统(好感度/亲密度/关系阶段/长期记忆; 移植自 emotionai_pro)。
        # emotion.enabled 开关在启动时读取, 修改后重启生效; 失败降级(正常聊天不受影响)。
        self._emotion_manager = None
        if self._config.emotion.enabled:
            try:
                from mohobot.emotion import EmotionManager
                self._emotion_manager = EmotionManager(
                    data_dir=self._config.data_dir,
                    config=self._config.emotion,
                    llm_service=self._llm_service,
                    task_supervisor=self._task_supervisor,
                    admins=list(self._config.admins),
                    bot_name_provider=self._emotion_bot_name,
                )
                await self._emotion_manager.startup()
                logger.info("全局情感系统已加载(EmotionManager)")
            except Exception as e:
                self._emotion_manager = None
                logger.warning(f"情感系统初始化失败, 已降级: {e}")

        self._image_cache = ImageCache(cache_dir=f"{self._config.data_dir}/cache")

        # TTS 语音合成(GPT-SoVITS api_v2)。
        # tts.enabled 未开启/初始化失败 → 降级为 None(正常聊天不受影响)。
        self._tts_service = None
        if self._config.tts.enabled and self._config.tts.base_url:
            try:
                from mohobot.services.gsv_tts import TTSService
                self._tts_service = TTSService(
                    self._config.tts,
                    task_supervisor=self._task_supervisor,
                )
                logger.info(f"TTS 服务已创建: {self._config.tts.base_url}")
            except Exception as e:
                self._tts_service = None
                logger.warning(f"TTS 服务初始化失败, 已降级: {e}")

        self._plugin_system = PluginSystem(
            plugins_dir=self._config.plugins_dir,
            data_dir=self._config.data_dir,
        )
        self._plugin_system.set_task_supervisor(self._task_supervisor)
        self._plugin_system.set_song_matcher(self._song_matcher)

        # 4. Load plugins
        # 运行时引用由 PluginSystem 持有, 加载/热重载后自动注入
        self._plugin_system.set_runtime_refs(bot_manager=self._bot_manager)
        self._plugin_system.set_admin_ids(list(self._config.admins))
        # Anysearch 实时联网搜索客户端(供插件与流水线使用)
        from mohobot.anysearch import AnySearchClient
        self._anysearch_client: AnySearchClient | None = None
        if self._config.anysearch.enabled and self._config.anysearch.api_key:
            self._anysearch_client = AnySearchClient(
                api_key=self._config.anysearch.api_key,
                base_url=self._config.anysearch.base_url,
                timeout=self._config.anysearch.timeout,
            )
            self._plugin_system.set_runtime_refs(anysearch_client=self._anysearch_client)
        plugin_count = await self._plugin_system.load_plugins()
        logger.info(f"Loaded {plugin_count} plugin(s)")

        # 5. Database (会话持久化 + 面板备份/数据管理)
        if self._config.database.enabled:
            db_folder = self._config.database.folder
            if db_folder.startswith("./"):
                db_folder = str(Path(db_folder).resolve())
            self._database_manager = DatabaseManager(
                db_folder=db_folder,
                db_file=self._config.database.file,
            )

        # 6. Initialize message handler
        self._message_handler = MessageHandler(
            ws_server=None,  # Will be set after WS server creation
            context_manager=self._context_manager,
            llm_service=self._llm_service,
            plugin_system=self._plugin_system,
            data_dir=self._config.data_dir,
            reply_config=self._config.reply,
            database_manager=self._database_manager,
            image_cache=self._image_cache,
            global_config=self._config,
            song_matcher=self._song_matcher,
            emotion_manager=self._emotion_manager,
            tts_service=self._tts_service,
        )

        # 7. Set up interceptors (封禁过滤放最前 — 被禁用户一切消息静默丢弃)
        from mohobot.ban import BanInterceptor, BanStore
        self._ban_store = BanStore(data_dir=self._config.data_dir)
        ban_filter = BanInterceptor(
            data_dir=self._config.data_dir,
            enabled=self._config.ban.enabled,
            admins=self._config.admins,
            store=self._ban_store,
        )
        command_handler = CommandHandler(
            context_manager=self._context_manager,
            llm_service=self._llm_service,
            ws_server=None,  # Will be set after WS server creation
            plugin_system=self._plugin_system,
            emotion_manager=self._emotion_manager,
            tts_service=self._tts_service,
            admins=self._config.admins,
        )
        keyword_filter = KeywordFilter()
        # 拦截链: 封禁 → 插件 → 内置命令 → 关键词
        # 插件优先消费 / 命令(含别名如 /jrlp), 内置命令(/help /sess 等)兜底,
        # 未知指令节流不变; 关键词回复最后兜底普通消息。
        interceptors = [ban_filter, self._plugin_system, command_handler, keyword_filter]
        self._message_handler.set_interceptors(interceptors)

        # 8. Initialize WebSocket server
        self._ws_server = WSServer(
            bot_manager=self._bot_manager,
            host=self._config.server.host,
            port=self._config.server.port,
            max_size=self._config.server.max_size,
            task_supervisor=self._task_supervisor,
            outbound_interval=self._config.server.outbound_interval,
            outbound_maxsize=self._config.server.outbound_maxsize,
            outbound_enqueue_timeout=self._config.server.outbound_enqueue_timeout,
        )

        # Wire up circular references
        self._message_handler._ws = self._ws_server
        command_handler._ws = self._ws_server

        # TTS: 注入 ws_server 并启动合成队列 worker
        if self._tts_service is not None:
            self._tts_service.set_ws(self._ws_server)
            self._tts_service.start()

        # 注入 WS server 到插件(热重载后由 PluginSystem 自动重新注入)
        self._plugin_system.set_runtime_refs(ws_server=self._ws_server)
        self._plugin_system.apply_injections()

        # Set event callback from WS server to message handler
        self._ws_server.set_event_callback(self._message_handler.handle_event)

        # 9. Start servers
        await self._ws_server.start()

        # 10. Start web panel
        if self._config.web_panel.enabled:
            self._web_panel = WebPanel(
                host=self._config.web_panel.host,
                port=self._config.web_panel.port,
                username=self._config.web_panel.username,
                password_hash=self._config.web_panel.password_hash,
                data_dir=self._config.data_dir,
                config_path=self._config_path,
                bot_manager=self._bot_manager,
                context_manager=self._context_manager,
                llm_service=self._llm_service,
                plugin_system=self._plugin_system,
                ban_store=self._ban_store,
                ban_filter=ban_filter,
                restart_callback=self.restart,
                emotion_manager=self._emotion_manager,
            )
            # Start web panel in background
            self._web_panel_task = self._task_supervisor.create_task(
                self._run_web_panel(), name="web-panel", owner="web-panel"
            )

        # 11. 周期时间压缩(把超过 age_hours 的旧对话交给 AI 总结;
        # 间隔/开关配置热生效, 每周期读取 ContextManager 最新值)
        self._context_sweep_task = self._task_supervisor.create_task(
            self._context_sweep_loop(), name="context-sweep", owner="context-sweep"
        )

        self._running = True
        logger.info(
            f"Mohobot v{__version__} is running! "
            f"WS: ws://{self._config.server.host}:{self._config.server.port} | "
            f"Panel: http://{self._config.web_panel.host}:{self._config.web_panel.port}"
        )

        # 12. 审核面板(半独立): 未运行则拉起独立进程; mohobot 关闭不随机关闭它
        self._maybe_start_review_panel()

    def _maybe_start_review_panel(self) -> None:
        """拉起聊天记录审核面板(review/, 独立进程独立端口)。

        - review/config.yaml 不存在或 enabled=false 时不拉起
        - 端口已被监听(面板已在运行)时跳过
        - 脱离进程组启动, mohobot 退出不影响审核面板
        """
        import socket
        import subprocess

        review_dir = Path(__file__).resolve().parent / "review"
        entry = review_dir / "main.py"
        cfg_path = review_dir / "config.yaml"
        if not entry.exists():
            return

        port = 9091
        try:
            import yaml
            if not cfg_path.exists():
                logger.info("审核面板未配置(review/config.yaml 不存在), 跳过拉起")
                return
            rcfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if rcfg.get("enabled") is False:
                logger.info("审核面板 enabled=false, 跳过拉起")
                return
            port = int((rcfg.get("server") or {}).get("port", 9091))
        except Exception as e:
            logger.warning(f"审核面板配置读取失败, 跳过拉起: {e}")
            return

        probe = socket.socket()
        probe.settimeout(0.5)
        try:
            already_running = probe.connect_ex(("127.0.0.1", port)) == 0
        finally:
            probe.close()
        if already_running:
            logger.info(f"审核面板已在运行(端口 {port}), 跳过拉起")
            return

        kwargs: dict[str, Any] = dict(
            cwd=str(review_dir.parent),
            stdin=subprocess.DEVNULL,
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        try:
            log_fh = open(review_dir / "panel.log", "ab")
            kwargs["stdout"] = log_fh
            kwargs["stderr"] = log_fh
            subprocess.Popen([sys.executable, str(entry)], **kwargs)
            logger.info(f"审核面板已拉起: 端口 {port} (独立进程, 日志 review/panel.log)")
        except Exception as e:
            logger.warning(f"审核面板拉起失败: {e}")

    async def restart(self) -> None:
        """Restart the service in-process: shutdown then re-startup."""
        logger.info("Restarting Mohobot...")
        await self.shutdown()
        await self.startup()
        logger.info("Mohobot restarted successfully")

    def _make_song_annotator(self):
        """构造 legacy 路径用的歌曲注解回调(event → 注解文本)。"""
        matcher = self._song_matcher

        async def annotator(event) -> str:
            if matcher is None:
                return ""
            try:
                from mohobot.utils.cq_code import extract_plain_text
                text = (extract_plain_text(event.message) or "").strip()
                if not text:
                    return ""
                match = matcher.match(text)
                if match is None:
                    return ""
                return match.build_annotation()
            except Exception as e:
                logger.debug(f"歌曲注解失败: {e}")
                return ""

        return annotator

    def _emotion_bot_name(self, bot_id: str) -> str:
        """情感系统用的 bot 昵称(专家 prompt / 文本净化), 兜底 "AI"。"""
        if self._bot_manager:
            instance = self._bot_manager.get(bot_id)
            if instance is not None and instance.config:
                return (instance.config.nickname or "").strip() or "AI"
        return "AI"

    async def _run_web_panel(self) -> None:
        """Run the web panel (wraps uvicorn)."""
        try:
            await self._web_panel.start()
        except Exception as e:
            logger.error(f"Web panel failed: {e}")

    async def _context_sweep_loop(self) -> None:
        """周期时间压缩后台任务: 按配置间隔扫描全部会话, 压缩超龄旧对话。

        间隔与开关每周期从 ContextManager 读取(WebUI 保存后热生效);
        压缩本身不阻塞新消息路径(与 append 并发时靠文件锁+头部比对守卫)。
        """
        while True:
            cm = self._context_manager
            interval = getattr(cm, "sweep_interval_minutes", 30) if cm is not None else 30
            try:
                await asyncio.sleep(max(1, int(interval)) * 60)
            except asyncio.CancelledError:
                break
            if cm is None or not getattr(cm, "sweep_enabled", True):
                continue
            try:
                await cm.sweep_all_sessions()
            except Exception as e:
                logger.warning(f"周期时间压缩扫描失败: {e}")

    async def shutdown(self) -> None:
        """Gracefully shut down all components."""
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True
        logger.info("Shutting down Mohobot...")
        self._running = False

        # Stop web panel (await its server task so the port is freed
        # before a restart rebinds it — otherwise "Address already in use")
        if self._web_panel:
            await self._web_panel.stop()
        web_panel_task = getattr(self, "_web_panel_task", None)
        if web_panel_task is not None:
            try:
                await asyncio.wait_for(web_panel_task, timeout=5.0)
            except asyncio.TimeoutError:
                # uvicorn 卡住(如 lifespan 未响应) — 取消任务,防端口占用
                logger.warning("Web panel task did not exit in time, cancelling")
                web_panel_task.cancel()
                try:
                    await web_panel_task
                except (asyncio.CancelledError, Exception):
                    pass
            except (asyncio.CancelledError, Exception):
                pass
            self._web_panel_task = None
            self._web_panel = None

        # Stop plugin lifecycle before core transports are closed so hooks can release resources.
        if self._plugin_system:
            await self._plugin_system.shutdown_plugins()
        await self._task_supervisor.cancel_owner("plugins")

        # 停止上下文周期时间压缩任务
        await self._task_supervisor.cancel_owner("context-sweep")

        # 情感系统: 取消周期落盘任务并 flush 数据
        if self._emotion_manager:
            await self._emotion_manager.shutdown()

        # TTS: 停止合成队列 worker(丢弃排队任务)并关闭 HTTP 客户端
        if getattr(self, "_tts_service", None):
            await self._tts_service.stop()

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

        if self._usage_recorder:
            await self._usage_recorder.close()

        await self._task_supervisor.shutdown()
        self._shutting_down = False
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