"""MiniMax 云端 TTS 服务 — t2a_v2 合成客户端 + 全局单飞行队列。

替代原本地 GPT-SoVITS 方案: 合成走 MiniMax /v1/t2a_v2 同步接口
(音色为 MiniMax 克隆音色 voice_id, 克隆动作在 MiniMax 侧一次性完成),
返回 JSON 中 data.audio 为 **hex 编码**的音频字节, 解码后经 base64
内嵌 record 段发送(不依赖 NapCat 读取本地文件)。

队列语义同前: 全局单飞行 FIFO 串行消费(控费控速), 队列满丢最新;
LLM 来源失败静默(文本早已发出), /tts 指令来源失败回错误文本。
每次合成把 usage_characters 记入用量统计(module="tts")。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger


class MinimaxTTSClient:
    """MiniMax t2a_v2 合成客户端。

    - 音色: voice_id(MiniMax 克隆音色或系统音色), 请求级可覆盖(per-bot)
    - 响应: base_resp.status_code != 0 → 业务错误; data.audio 为 hex 字符串
    - synthesize 返回 (音频字节, 计费字符数); 任何失败返回 (None, 0), 不抛异常
    - http 客户端工厂可注入(测试 mock, 仿 AnySearchClient)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        model: str = "speech-2.8-hd",
        voice_id: str = "",
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
        sample_rate: int = 32000,
        bitrate: int = 128000,
        format: str = "mp3",
        timeout: float = 60.0,
        http_client_factory: Any = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or os.environ.get("MOHOBOT_MINIMAX_API_KEY", "")
        self._voice_setting: dict[str, Any] = {
            "speed": speed,
            "vol": vol,
            "pitch": pitch,
        }
        self._audio_setting: dict[str, Any] = {
            "sample_rate": sample_rate,
            "bitrate": bitrate,
            "format": format,
            "channel": 1,
        }
        self._model = model
        self._voice_id = voice_id
        self._timeout = timeout
        self._client_factory = http_client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """api_key 与全局 voice_id 均已配置。"""
        return bool(self._api_key and self._voice_id)

    def sync_config(self, cfg) -> None:
        """热同步 TTSConfig 字段(原位更新请求参数, 立即生效)。"""
        self._base_url = (cfg.base_url or "https://api.minimax.cn").rstrip("/")
        if cfg.api_key:
            self._api_key = cfg.api_key
        v = self._voice_setting
        v["speed"] = cfg.speed
        v["vol"] = cfg.vol
        v["pitch"] = cfg.pitch
        a = self._audio_setting
        a["sample_rate"] = cfg.sample_rate
        a["bitrate"] = cfg.bitrate
        a["format"] = cfg.format
        self._model = cfg.model
        self._voice_id = cfg.voice_id
        self._timeout = float(cfg.timeout)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._client_factory(timeout=self._timeout)
        return self._client

    async def synthesize(self, text: str, voice_id: str | None = None) -> tuple[bytes | None, int]:
        """合成一段文本, 返回 (音频字节, 计费字符数); 失败返回 (None, 0)。

        voice_id 传 None 时用全局默认(per-bot 覆盖由调用方解析后传入)。
        """
        if not self._api_key:
            logger.warning("MiniMax TTS 未配置 api_key, 跳过合成")
            return None, 0
        vid = voice_id or self._voice_id
        if not vid:
            logger.warning("MiniMax TTS 未配置 voice_id, 跳过合成")
            return None, 0
        payload = {
            "model": self._model,
            "text": text,
            "stream": False,
            "voice_setting": {"voice_id": vid, **self._voice_setting},
            "audio_setting": dict(self._audio_setting),
        }
        try:
            client = await self._get_client()
            resp = await client.post(
                f"{self._base_url}/v1/t2a_v2",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                # 不打印含 key 的请求头; 响应体可能含业务错误信息
                logger.warning(
                    f"MiniMax TTS 失败(http {resp.status_code}): {(resp.text or '')[:200]}"
                )
                return None, 0
            result = resp.json()
            base_resp = result.get("base_resp") or {}
            if base_resp.get("status_code", 0) != 0:
                logger.warning(
                    f"MiniMax TTS 业务错误({base_resp.get('status_code')}): "
                    f"{base_resp.get('status_msg', '')[:200]}"
                )
                return None, 0
            audio_hex = str((result.get("data") or {}).get("audio", "") or "")
            if not audio_hex:
                logger.warning("MiniMax TTS 返回空音频")
                return None, 0
            try:
                audio = binascii.unhexlify(audio_hex)
            except (ValueError, binascii.Error) as e:
                logger.warning(f"MiniMax TTS 音频 hex 解码失败: {e}")
                return None, 0
            usage = int(((result.get("extra_info") or {}).get("usage_characters")) or 0)
            return audio, usage
        except Exception as e:
            logger.warning(f"MiniMax TTS 异常: {e}")
            return None, 0

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


@dataclass
class TTSJob:
    """一次语音合成任务。

    voice_id: 该 bot 使用的 MiniMax 音色(提交时按 BotConfig.tts_voice_id
    解析好; None/空 = 用全局默认)。
    """

    bot_id: str
    chat_type: str   # "group" | "private"
    chat_id: str
    text: str
    source: str = "llm"  # "llm"(LLM 自动朗读, 失败静默) | "command"(/tts 指令, 失败回文本)
    voice_id: str | None = None


class TTSService:
    """全局单飞行 TTS 队列(MiniMax 云端合成)。

    - submit: 队列满 → 丢最新(返回 False), 已排队照常
    - worker: 每次合成一条 → 用该任务 bot_id 发 record 语音(base64)
    - LLM 来源失败静默(文本早已发出); 指令来源失败回错误文本
    - 计费: 每次成功合成的 usage_characters 记入用量统计(module="tts")
    """

    def __init__(self, config, task_supervisor=None, usage_recorder=None):
        # config: TTSConfig(models/config.py) — 与 main 持有的 GlobalConfig.tts
        # 是同一对象, sync_config 原位更新字段即全框架热生效
        self.cfg = config
        self._client = MinimaxTTSClient(
            config.base_url,
            config.api_key,
            model=config.model,
            voice_id=config.voice_id,
            speed=config.speed,
            vol=config.vol,
            pitch=config.pitch,
            sample_rate=config.sample_rate,
            bitrate=config.bitrate,
            format=config.format,
            timeout=float(config.timeout),
        )
        self._queue: asyncio.Queue[TTSJob] = asyncio.Queue(maxsize=max(1, int(config.queue_maxsize)))
        self._supervisor = task_supervisor
        self._ws = None
        self._worker: asyncio.Task | None = None
        # 用量统计(可选注入): 记录 TTS 字符消耗
        self._usage_recorder = usage_recorder
        # 运行状态(面板查看): 当前合成任务 + 计数
        self.current_job: TTSJob | None = None
        self.stats: dict[str, int] = {"done": 0, "failed": 0, "dropped": 0, "chars": 0}

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
        """启动合成队列 worker(幂等)。"""
        if self._worker is not None and not self._worker.done():
            return
        if self._supervisor is not None:
            self._worker = self._supervisor.create_task(
                self._run(), name="tts-worker", owner="tts"
            )
        else:
            self._worker = asyncio.create_task(self._run(), name="tts-worker")
        logger.info("TTS 合成队列已启动(单飞行, MiniMax 云端合成)")

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
        """聚合状态(供 /api/tts/status): 配置完整度 + 队列/计数。"""
        cur = self.current_job
        return {
            "tts_enabled": bool(self.cfg.enabled),
            "configured": self._client.configured,
            "api_key_set": bool(self._client._api_key),
            "voice_id_set": bool(self._client._voice_id),
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
        audio, usage_chars = await self._client.synthesize(job.text, job.voice_id)
        if audio is None:
            self.stats["failed"] += 1
            logger.warning(f"TTS 合成失败({job.source}): {job.text[:30]!r}")
            if job.source == "command":
                await self._send_text(job, "语音合成失败，请稍后再试~")
            return
        self.stats["chars"] += usage_chars
        await self._record_usage(job, usage_chars)
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

    async def _record_usage(self, job: TTSJob, chars: int) -> None:
        """把 TTS 字符消耗记入用量统计(module="tts", 按bot维度)。"""
        if self._usage_recorder is None or chars <= 0:
            return
        try:
            await self._usage_recorder.record(
                None, model=self.cfg.model, bot_id=job.bot_id,
                module="tts", kind="tts", chat_type=job.chat_type,
                chat_id=str(job.chat_id), chars=chars,
            )
        except Exception as e:
            logger.debug(f"TTS 用量记录失败: {e}")

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
