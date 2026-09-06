"""GPT-SoVITS TTS 服务 — api_v2 HTTP 客户端 + 全局单飞行队列。

GSV 服务端一次只能合成一条(单 GPU): 框架侧维护全局队列串行消费,
队列满时丢最新(新请求直接放弃, 已排队的照常合成)。
合成成功后由任务携带的 bot_id 发送 record 语音(base64 内嵌,
不依赖 NapCat 读取本地文件)。

模型权重不在运行时切换 — GSV 服务端启动时通过 tts_infer.yaml 自行加载,
本模块只调 /tts 推理接口。
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger


class GsvTTSClient:
    """GPT-SoVITS api_v2 /tts 客户端。

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
        timeout: float = 60.0,
        http_client_factory: Any = None,
    ):
        self._tts_url = base_url.rstrip("/") + "/tts"
        self._payload: dict[str, Any] = {
            "text_lang": text_lang,
            "prompt_lang": prompt_lang,
            "ref_audio_path": ref_audio_path,
            "prompt_text": prompt_text,
            "media_type": media_type,
            "speed_factor": speed_factor,
            "text_split_method": "cut5",
            "streaming_mode": False,
        }
        self._timeout = timeout
        self._client_factory = http_client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._client_factory(timeout=self._timeout)
        return self._client

    async def synthesize(self, text: str) -> bytes | None:
        """合成一段文本, 返回音频字节; 失败返回 None。"""
        try:
            client = await self._get_client()
            resp = await client.post(self._tts_url, json={**self._payload, "text": text})
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
    """全局单飞行 TTS 队列。

    - submit: 队列满 → 丢最新(返回 False), 已排队照常
    - worker: 每次合成一条 → 用该任务 bot_id 发 record 语音(base64)
    - LLM 来源失败静默(文本早已发出); 指令来源失败回错误文本
    """

    def __init__(self, config, task_supervisor=None):
        # config: TTSConfig(models/config.py)
        self.cfg = config
        self._client = GsvTTSClient(
            config.base_url,
            text_lang=config.text_lang,
            prompt_lang=config.prompt_lang,
            ref_audio_path=config.ref_audio_path,
            prompt_text=config.prompt_text,
            media_type=config.media_type,
            speed_factor=config.speed_factor,
            timeout=float(config.timeout),
        )
        self._queue: asyncio.Queue[TTSJob] = asyncio.Queue(maxsize=max(1, int(config.queue_maxsize)))
        self._supervisor = task_supervisor
        self._ws = None
        self._worker: asyncio.Task | None = None

    def set_ws(self, ws_server) -> None:
        """注入 WS server(main 装配时调用, 与其他组件同模式)。"""
        self._ws = ws_server

    # ── 生命周期 ─────────────────────────────────────────────

    def start(self) -> None:
        """启动 worker(幂等)。"""
        if self._worker is not None and not self._worker.done():
            return
        if self._supervisor is not None:
            self._worker = self._supervisor.create_task(
                self._run(), name="tts-worker", owner="tts"
            )
        else:
            self._worker = asyncio.create_task(self._run(), name="tts-worker")
        logger.info("TTS 服务已启动(单飞行队列)")

    async def stop(self) -> None:
        """停止 worker 并清空队列(文本已发出, 语音放弃), 关闭 HTTP 客户端。"""
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
            return True
        except asyncio.QueueFull:
            logger.warning(
                f"TTS 队列已满({self._queue.maxsize}), 丢弃最新请求: {job.text[:30]!r}"
            )
            return False

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    # ── worker ───────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"TTS 任务处理异常: {e}")
            finally:
                self._queue.task_done()

    async def _process(self, job: TTSJob) -> None:
        audio = await self._client.synthesize(job.text)
        if audio is None:
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
            logger.debug(f"TTS 语音已发送: {job.chat_type}:{job.chat_id} via {job.bot_id}")
        except Exception as e:
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
