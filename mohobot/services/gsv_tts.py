"""GPT-SoVITS TTS 服务 — api_v2 HTTP 客户端 + 全局单飞行队列 + 进程管理。

GSV 服务端一次只能合成一条(单 GPU/CPU): 框架侧维护全局队列串行消费,
队列满时丢最新(新请求直接放弃, 已排队的照常合成)。
合成成功后由任务携带的 bot_id 发送 record 语音(base64 内嵌,
不依赖 NapCat 读取本地文件)。

GSV 后台进程(api_v2.py)由 WebUI 手动启停(start_service/stop_service/
restart_service), **不随 mohobot 生命周期**——mohobot 启动不拉起它,
mohobot 关闭也不停它。停止流程: /control exit 优雅退出 → 等待
stop_wait_seconds → 仍在监听则 kill 监听该端口的进程兜底。

模型权重不在运行时切换 — GSV 服务端启动时通过 tts_infer.yaml 自行加载,
本模块只调 /tts 推理与 /control 控制。
"""

from __future__ import annotations

import asyncio
import base64
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger


class GsvTTSClient:
    """GPT-SoVITS api_v2 /tts 与 /control 客户端。

    http 客户端工厂可注入(测试 mock, 仿 AnySearchClient)。
    synthesize 返回音频字节; 任何失败返回 None(调用方降级, 不抛异常)。
    """

    def __init__(
        self,
        base_url: str,
        *,
        text_lang: str = "zh",
        prompt_lang: str = "zh",
        ref_audio_path: str = "",
        prompt_text: str = "",
        media_type: str = "wav",
        speed_factor: float = 1.0,
        top_k: int = 15,
        top_p: float = 1.0,
        temperature: float = 1.0,
        fragment_interval: float = 0.3,
        text_split_method: str = "cut5",
        timeout: float = 300.0,
        http_client_factory: Any = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._payload: dict[str, Any] = {
            "text_lang": text_lang,
            "prompt_lang": prompt_lang,
            "ref_audio_path": ref_audio_path,
            "prompt_text": prompt_text,
            "media_type": media_type,
            "speed_factor": speed_factor,
            "top_k": top_k,
            "top_p": top_p,
            "temperature": temperature,
            "fragment_interval": fragment_interval,
            "text_split_method": text_split_method,
            "streaming_mode": False,
        }
        self._timeout = timeout
        self._client_factory = http_client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None

    @property
    def tts_url(self) -> str:
        return self._base_url + "/tts"

    def sync_config(self, cfg) -> None:
        """热同步 TTSConfig 字段(原位更新 payload/超时/地址, 立即生效)。"""
        self._base_url = (cfg.base_url or "http://127.0.0.1:9880").rstrip("/")
        p = self._payload
        p["text_lang"] = cfg.text_lang
        p["prompt_lang"] = cfg.prompt_lang
        p["ref_audio_path"] = cfg.ref_audio_path
        p["prompt_text"] = cfg.prompt_text
        p["media_type"] = cfg.media_type
        p["speed_factor"] = cfg.speed_factor
        p["top_k"] = cfg.top_k
        p["top_p"] = cfg.top_p
        p["temperature"] = cfg.temperature
        p["fragment_interval"] = cfg.fragment_interval
        p["text_split_method"] = cfg.text_split_method
        self._timeout = float(cfg.timeout)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._client_factory(timeout=self._timeout)
        return self._client

    async def synthesize(self, text: str) -> bytes | None:
        """合成一段文本, 返回音频字节; 失败返回 None。"""
        try:
            client = await self._get_client()
            resp = await client.post(self.tts_url, json={**self._payload, "text": text})
            if resp.status_code != 200:
                logger.warning(
                    f"GSV TTS 失败(http {resp.status_code}): {(resp.text or '')[:200]}"
                )
                return None
            audio = resp.content
            if not audio:
                logger.warning("GSV TTS 返回空音频")
                return None
            return audio
        except Exception as e:
            logger.warning(f"GSV TTS 异常: {e}")
            return None

    async def control(self, command: str) -> None:
        """调 /control (restart|exit); 连接中断/超时视为已发出, 不抛异常。"""
        try:
            client = await self._get_client()
            await client.get(
                f"{self._base_url}/control", params={"command": command},
            )
        except Exception as e:
            logger.debug(f"GSV control({command}) 请求返回: {e}")

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


@dataclass
class TTSJob:
    """一次语音合成任务。"""

    bot_id: str
    chat_type: str   # "group" | "private"
    chat_id: str
    text: str
    source: str = "llm"  # "llm"(LLM 自动朗读, 失败静默) | "command"(/tts 指令, 失败回文本)


class TTSService:
    """全局单飞行 TTS 队列 + GSV 后台进程管理。

    - submit: 队列满 → 丢最新(返回 False), 已排队照常
    - worker: 每次合成一条 → 用该任务 bot_id 发 record 语音(base64)
    - LLM 来源失败静默(文本早已发出); 指令来源失败回错误文本
    - GSV 进程: WebUI 手动启停, 不随 mohobot 生命周期
    """

    def __init__(self, config, task_supervisor=None):
        # config: TTSConfig(models/config.py) — 与 main 持有的 GlobalConfig.tts
        # 是同一对象, sync_config 原位更新字段即全框架热生效
        self.cfg = config
        self._client = GsvTTSClient(
            config.base_url,
            text_lang=config.text_lang,
            prompt_lang=config.prompt_lang,
            ref_audio_path=config.ref_audio_path,
            prompt_text=config.prompt_text,
            media_type=config.media_type,
            speed_factor=config.speed_factor,
            top_k=config.top_k,
            top_p=config.top_p,
            temperature=config.temperature,
            fragment_interval=config.fragment_interval,
            text_split_method=config.text_split_method,
            timeout=float(config.timeout),
        )
        self._queue: asyncio.Queue[TTSJob] = asyncio.Queue(maxsize=max(1, int(config.queue_maxsize)))
        self._supervisor = task_supervisor
        self._ws = None
        self._worker: asyncio.Task | None = None
        # 运行状态(面板查看): 当前合成任务 + 计数
        self.current_job: TTSJob | None = None
        self.stats: dict[str, int] = {"done": 0, "failed": 0, "dropped": 0}
        # 端口探测器(可注入替换, 测试用): async () -> bool
        self.port_prober = self._tcp_probe

    def set_ws(self, ws_server) -> None:
        """注入 WS server(main 装配时调用, 与其他组件同模式)。"""
        self._ws = ws_server

    # ── 配置热同步 ───────────────────────────────────────────

    def sync_config(self, cfg) -> None:
        """把磁盘最新 TTSConfig 字段原位拷进运行中对象(WebUI 保存后调用)。

        self.cfg 与 main 的 GlobalConfig.tts 为同一对象, 原位 setattr 后
        message_handler._tts_active / CommandHandler._cmd_tts 读到的即新值;
        仅 queue_maxsize(队列构造固定)不生效, 需重启。
        """
        import dataclasses
        for f in dataclasses.fields(cfg):
            setattr(self.cfg, f.name, getattr(cfg, f.name))
        self._client.sync_config(self.cfg)
        logger.info("TTS 配置已热同步(除 queue_maxsize 外立即生效)")

    # ── 生命周期(mohobot 侧 worker) ──────────────────────────

    def start(self) -> None:
        """启动合成队列 worker(幂等)。注意: 不拉起 GSV 进程。"""
        if self._worker is not None and not self._worker.done():
            return
        if self._supervisor is not None:
            self._worker = self._supervisor.create_task(
                self._run(), name="tts-worker", owner="tts"
            )
        else:
            self._worker = asyncio.create_task(self._run(), name="tts-worker")
        logger.info("TTS 合成队列已启动(单飞行; GSV 进程由面板手动启停)")

    async def stop(self) -> None:
        """停止 worker 并清空队列(文本已发出, 语音放弃), 关闭 HTTP 客户端。

        注意: 不停止 GSV 进程(独立生命周期, 由面板管理)。
        """
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        self._worker = None
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self._client.close()

    # ── 入队 ─────────────────────────────────────────────────

    def submit(self, job: TTSJob) -> bool:
        """入队一个合成任务; 队列满 → 丢最新, 返回 False。"""
        try:
            self._queue.put_nowait(job)
            self.stats["total_submitted"] = self.stats.get("total_submitted", 0) + 1
            return True
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            logger.warning(
                f"TTS 队列已满({self._queue.maxsize}), 丢弃最新请求: {job.text[:30]!r}"
            )
            return False

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    # ── 状态(面板) ───────────────────────────────────────────

    async def service_status(self) -> dict[str, Any]:
        """聚合状态: GSV 进程是否运行 + 队列/计数(供 /api/tts/status)。"""
        running = False
        try:
            running = await self.port_prober()
        except Exception as e:
            logger.debug(f"GSV 端口探测失败: {e}")
        cur = self.current_job
        return {
            "tts_enabled": bool(self.cfg.enabled),
            "running": running,
            "service_configured": bool(self.cfg.service_command),
            "base_url": self.cfg.base_url,
            "current": (
                {
                    "bot_id": cur.bot_id, "chat_type": cur.chat_type,
                    "chat_id": cur.chat_id, "source": cur.source,
                    "text": cur.text[:60],
                } if cur is not None else None
            ),
            "queued": self._queue.qsize(),
            "queue_maxsize": self._queue.maxsize,
            "stats": dict(self.stats),
        }

    # ── GSV 进程管理(WebUI 手动启停, 独立于 mohobot 生命周期) ──

    async def _tcp_probe(self) -> bool:
        """TCP 探测 base_url 的 host:port 是否可连(GSV 监听即视为运行)。"""
        parts = urlsplit(self.cfg.base_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.5,
            )
            writer.close()
            return True
        except Exception:
            return False

    async def start_service(self) -> tuple[bool, str]:
        """拉起 GSV 后台进程(detached, 日志重定向到 service_log_path)。"""
        if await self.port_prober():
            return False, "GSV 已在运行"
        cfg = self.cfg
        if not cfg.service_command:
            return False, "未配置启动命令(tts.service_command), 无法拉起"
        argv = shlex.split(cfg.service_command)
        if not argv:
            return False, "启动命令为空"
        try:
            if cfg.service_log_path:
                # 子进程继承 fd, 父进程句柄用完即关
                with open(cfg.service_log_path, "ab") as log_fh:
                    subprocess.Popen(
                        argv,
                        cwd=cfg.service_cwd or None,
                        stdin=subprocess.DEVNULL,
                        stdout=log_fh,
                        stderr=subprocess.STDOUT,
                        start_new_session=(sys.platform != "win32"),
                    )
            else:
                subprocess.Popen(
                    argv,
                    cwd=cfg.service_cwd or None,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=(sys.platform != "win32"),
                )
        except Exception as e:
            logger.warning(f"GSV 进程拉起失败: {e}")
            return False, f"启动失败: {e}"
        logger.info(f"GSV 进程已拉起: {cfg.service_command[:80]}")
        return True, "进程已拉起, 模型加载约需 1~2 分钟(期间状态显示未运行)"

    async def stop_service(self) -> tuple[bool, str]:
        """停止 GSV: /control exit 优雅退出 → 等待 stop_wait_seconds →
        仍监听则 kill 监听该端口的进程(SIGTERM→3s→SIGKILL)兜底。"""
        if not await self.port_prober():
            return False, "GSV 未在运行"
        # 1) 优雅退出(请求可能因进程退出而中断, 忽略异常)
        await self._client.control("exit")
        # 2) 等待端口释放
        deadline = time.monotonic() + max(3, int(self.cfg.stop_wait_seconds))
        while time.monotonic() < deadline:
            if not await self.port_prober():
                logger.info("GSV 进程已停止(control exit)")
                return True, "GSV 已停止"
            await asyncio.sleep(1)
        # 3) kill 兜底: 只杀监听目标端口的进程(不按命令名 pgrep, 防误杀)
        port = urlsplit(self.cfg.base_url).port or 80
        pids = await self._listening_pids(port)
        if not pids:
            return False, f"退出等待超时且未找到监听端口 {port} 的进程, 请手动处理"
        import os as _os
        for pid in pids:
            try:
                _os.kill(pid, signal.SIGTERM)
            except Exception as e:
                logger.warning(f"kill -TERM {pid} 失败: {e}")
        await asyncio.sleep(3)
        if await self.port_prober():
            for pid in pids:
                try:
                    _os.kill(pid, signal.SIGKILL)
                except Exception as e:
                    logger.warning(f"kill -9 {pid} 失败: {e}")
            await asyncio.sleep(2)
        ok = not await self.port_prober()
        msg = "GSV 已停止(kill 兜底)" if ok else "GSV 未能停止, 请手动处理"
        logger.warning(f"GSV stop: {msg} (pids={pids})")
        return ok, msg

    async def restart_service(self) -> tuple[bool, str]:
        """重启 GSV(停止 → 拉起)。模型加载约 1~2 分钟。"""
        if await self.port_prober():
            ok, msg = await self.stop_service()
            if not ok:
                return False, f"停止失败, 未重启: {msg}"
            await asyncio.sleep(1)
        return await self.start_service()

    @staticmethod
    async def _listening_pids(port: int) -> list[int]:
        """解析 ss -ltnp, 返回监听指定端口的进程 pid(Linux)。"""
        if sys.platform == "win32":
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                "ss", "-ltnp",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception as e:
            logger.warning(f"ss -ltnp 执行失败: {e}")
            return []
        import re
        pids: list[int] = []
        for line in out.decode("utf-8", "ignore").splitlines():
            if f":{port} " not in line:
                continue
            for m in re.finditer(r"pid=(\d+)", line):
                pid = int(m.group(1))
                if pid not in pids:
                    pids.append(pid)
        return pids

    # ── worker ───────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            self.current_job = job
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"TTS 任务处理异常: {e}")
                self.stats["failed"] += 1
            finally:
                self.current_job = None
                self._queue.task_done()

    async def _process(self, job: TTSJob) -> None:
        audio = await self._client.synthesize(job.text)
        if audio is None:
            self.stats["failed"] += 1
            logger.warning(f"TTS 合成失败({job.source}): {job.text[:30]!r}")
            if job.source == "command":
                await self._send_text(job, "语音合成失败，请稍后再试~")
            return
        if self._ws is None:
            logger.warning("TTS 语音发送跳过: ws_server 未注入")
            return
        b64 = base64.b64encode(audio).decode()
        segment = [{"type": "record", "data": {"file": f"base64://{b64}"}}]
        try:
            if job.chat_type == "group":
                await self._ws.send_group_msg(job.bot_id, job.chat_id, segment)
            else:
                await self._ws.send_private_msg(job.bot_id, job.chat_id, segment)
            self.stats["done"] += 1
            logger.debug(f"TTS 语音已发送: {job.chat_type}:{job.chat_id} via {job.bot_id}")
        except Exception as e:
            self.stats["failed"] += 1
            logger.warning(f"TTS 语音发送失败: {e}")
            if job.source == "command":
                await self._send_text(job, "语音发送失败，请稍后再试~")

    async def _send_text(self, job: TTSJob, text: str) -> None:
        """指令来源的失败提示(纯文本)。"""
        if self._ws is None:
            return
        try:
            if job.chat_type == "group":
                await self._ws.send_group_msg(job.bot_id, job.chat_id, text)
            else:
                await self._ws.send_private_msg(job.bot_id, job.chat_id, text)
        except Exception as e:
            logger.warning(f"TTS 错误提示发送失败: {e}")
