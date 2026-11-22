"""TTS 服务(可切换后端) — 自建 HTTP 合成(默认) / MiniMax 云端(保留仅下线)。

队列语义(全局单飞行 FIFO, 满丢最新; LLM 来源失败静默, /tts 指令来源失败回
错误文本)与语音发送(record 段 base64 内嵌)与旧版一致。

http 后端: POST {base_url}/tts(Bearer 鉴权, {"text":...}) → wav 字节流;
本地 ffmpeg 转 mp3 后发送(NapCat 兼容性已在 MiniMax 时期验证), ffmpeg
不可用/转换失败自动降级发原始 wav。
minimax 后端: MinimaxTTSClient(services/minimax_tts.py, 保留仅下线)。

backend 可经 WebUI 改配置热切换(sync_config 检测 backend 变化重建客户端)。
"""

from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger


class HttpTTSClient:
    """自建 HTTP 合成服务客户端。

    接口: POST {base_url}/tts, Authorization: Bearer <token>,
    JSON body {"text": ...} → 200 时响应体即音频字节(实测 32kHz/16bit wav)。
    synthesize 返回 (音频字节, 计费字符数[=len(text), 供用量统计]);
    任何失败返回 (None, 0), 不抛异常。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout: float = 60.0,
        http_client_factory: Any = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or os.environ.get("MOHOBOT_TTS_API_KEY", "")
        self._timeout = timeout
        self._client_factory = http_client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """base_url 与 api_key 均已配置。"""
        return bool(self._base_url and self._api_key)

    def sync_config(self, cfg) -> None:
        """热同步 TTSConfig 字段(原位更新地址/token/超时, 立即生效)。"""
        new_timeout = float(cfg.timeout)
        if self._client is not None and new_timeout != self._timeout:
            # issue #131: httpx transport 的 timeout 在客户端创建时固定,
            # 只改字段不会影响已建连接池; 丢弃旧客户端, 下次调用用新超时创建。
            old, self._client = self._client, None
            try:
                asyncio.get_running_loop().create_task(old.aclose())
            except RuntimeError:
                pass
        self._base_url = (cfg.base_url or "").rstrip("/")
        if cfg.api_key:
            self._api_key = cfg.api_key
        self._timeout = new_timeout

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._client_factory(timeout=self._timeout)
        return self._client

    async def synthesize(self, text: str, voice_id: str | None = None) -> tuple[bytes | None, int]:
        """合成一段文本; voice_id 参数仅为与 MiniMax 客户端签名一致, 此处忽略。"""
        if not self._base_url:
            logger.warning("自建 TTS 未配置 base_url, 跳过合成")
            return None, 0
        if not self._api_key:
            logger.warning("自建 TTS 未配置 api_key(MOHOBOT_TTS_API_KEY 或 tts.api_key), 跳过合成")
            return None, 0
        try:
            client = await self._get_client()
            resp = await client.post(
                f"{self._base_url}/tts",
                json={"text": text},
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    f"自建 TTS 失败(http {resp.status_code}): {(resp.text or '')[:200]}"
                )
                return None, 0
            audio = resp.content
            if not audio:
                logger.warning("自建 TTS 返回空音频")
                return None, 0
            if audio.lstrip()[:1] == b"{":
                # 200 但返回的是 JSON(业务错误), 不当音频发
                logger.warning(f"自建 TTS 返回 JSON 错误体: {audio[:200]!r}")
                return None, 0
            return audio, len(text)
        except Exception as e:
            logger.warning(f"自建 TTS 异常: {e}")
            return None, 0

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


def _build_client(cfg):
    """按 cfg.backend 构造合成客户端。"""
    if cfg.backend == "minimax":
        from mohobot.services.minimax_tts import MinimaxTTSClient
        return MinimaxTTSClient(
            cfg.base_url,
            cfg.api_key,
            model=cfg.model,
            voice_id=cfg.voice_id,
            speed=cfg.speed,
            vol=cfg.vol,
            pitch=cfg.pitch,
            sample_rate=cfg.sample_rate,
            bitrate=cfg.bitrate,
            format=cfg.format,
            timeout=float(cfg.timeout),
        )
    return HttpTTSClient(cfg.base_url, cfg.api_key, timeout=float(cfg.timeout))


def _client_signature(cfg) -> tuple:
    """影响合成客户端构造的参数快照; 变化时必须重建客户端才真正生效。"""
    return (
        cfg.backend,
        float(cfg.timeout),
        cfg.base_url,
        cfg.api_key,
        cfg.model,
        cfg.voice_id,
        cfg.speed,
        cfg.vol,
        cfg.pitch,
        cfg.sample_rate,
        cfg.bitrate,
        cfg.format,
    )


@dataclass
class TTSJob:
    """一次语音合成任务。

    voice_id: 仅 minimax 后端生效(per-bot BotConfig.tts_voice_id 解析,
    留空用全局); http 后端音色由服务端固定, 客户端忽略该字段。

    generation: 入队时的配置代次。任务在队列中/合成中遇到配置热切换
    (尤其 enabled 关闭)后不得继续发送(issue #131)。
    """

    bot_id: str
    chat_type: str   # "group" | "private"
    chat_id: str
    text: str
    source: str = "llm"  # "llm"(LLM 自动朗读, 失败静默) | "command"(/tts 指令, 失败回文本)
    voice_id: str | None = None
    generation: int = 0


class TTSService:
    """全局单飞行 TTS 队列(可切换后端)。

    - submit: 队列满 → 丢最新(返回 False), 已排队照常
    - worker: 每次合成一条 → (http 后端)ffmpeg 转 mp3 → 用任务 bot_id 发 record 语音
    - LLM 来源失败静默(文本早已发出); 指令来源失败回错误文本
    - 计费: 每次成功合成把字符数记入用量统计(module="tts", 按 bot 维度)
    - backend 热切换: sync_config 检测 backend 变化自动重建客户端
    """

    def __init__(self, config, task_supervisor=None, usage_recorder=None):
        # config: TTSConfig(models/config.py) — 与 main 持有的 GlobalConfig.tts
        # 是同一对象, sync_config 原位更新字段即全框架热生效
        self.cfg = config
        self._client = _build_client(config)
        self._backend = config.backend
        # 影响客户端构造的参数快照 + 配置代次(issue #131):
        # 代次变化后, 旧代次入队的任务在 worker 中直接丢弃, 不再发送。
        self._client_signature = _client_signature(config)
        self._config_generation = 0
        # 退役客户端关闭任务: sync_config 可能在同一事件循环内连续调用,
        # stop() 会统一等待它们结束, 避免资源泄漏。
        self._retiring: set[asyncio.Task] = set()
        self._queue: asyncio.Queue[TTSJob] = asyncio.Queue(maxsize=max(1, int(config.queue_maxsize)))
        self._supervisor = task_supervisor
        self._ws = None
        self._worker: asyncio.Task | None = None
        # 用量统计(可选注入): 记录 TTS 字符消耗
        self._usage_recorder = usage_recorder
        # ffmpeg 可用性缓存(探测失败后本进程内不再反复尝试; sync_config 重置)
        self._ffmpeg_ok: bool | None = None
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
        backend/timeout 等构造参数变化时重建合成客户端(关旧开新, 立即切换);
        每次热同步都推进配置代次, 旧代次入队的语音任务不再发送(issue #131)。
        """
        import dataclasses
        for f in dataclasses.fields(cfg):
            setattr(self.cfg, f.name, getattr(cfg, f.name))
        self._config_generation += 1
        new_signature = _client_signature(self.cfg)
        if new_signature != self._client_signature:
            self._rebuild_client()
            self._client_signature = _client_signature(self.cfg)
        else:
            self._client.sync_config(self.cfg)
        if self.cfg.backend != "minimax":
            self._ffmpeg_ok = None  # 后端/转码配置变了, 重探 ffmpeg
        logger.info(
            f"TTS 配置已热同步(backend={self.cfg.backend}, "
            f"generation={self._config_generation}; 除 queue_maxsize 外立即生效)"
        )

    def _retire_client(self, old) -> None:
        """异步关闭被替换的客户端; 关闭任务登记到 stop() 统一等待。"""
        if old is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _close_old() -> None:
            try:
                await old.close()
            except Exception as e:  # noqa: BLE001 - 旧客户端关闭失败不影响新客户端
                logger.debug(f"TTS 旧客户端关闭失败: {e}")

        task = loop.create_task(_close_old(), name="tts-client-close")
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    def _rebuild_client(self) -> None:
        old = self._client
        try:
            new_client = _build_client(self.cfg)
        except Exception as e:
            logger.error(f"TTS 后端切换失败(保留原后端 {self._backend}): {e}")
            # 回滚内存配置中的 backend, 与实际客户端保持一致
            self.cfg.backend = self._backend
            return
        self._client = new_client
        self._backend = self.cfg.backend
        self._retire_client(old)
        logger.info(f"TTS 合成客户端已重建: backend={self.cfg.backend}")

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
        logger.info(f"TTS 合成队列已启动(单飞行, backend={self.cfg.backend})")

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
        # 等待被热切换替换掉的客户端真正关闭, 避免连接池泄漏
        if self._retiring:
            await asyncio.gather(*list(self._retiring), return_exceptions=True)
        await self._client.close()

    # ── 入队 ─────────────────────────────────────────────────

    def submit(self, job: TTSJob) -> bool:
        """入队一个合成任务; 队列满 → 丢最新, 返回 False。"""
        try:
            job.generation = self._config_generation
            self._queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            logger.warning(
                f"TTS 队列已满({self._queue.maxsize}), 丢弃最新请求: {job.text[:30]!r}"
            )
            return False

    def _job_is_stale(self, job: TTSJob) -> bool:
        """任务是否已失去发送资格: TTS 被关闭或配置代次已变化(issue #131)。"""
        if not bool(getattr(self.cfg, "enabled", False)):
            return True
        return int(getattr(job, "generation", 0) or 0) != self._config_generation

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def queue_full(self) -> bool:
        """队列是否已满(无标注概率朗读据此决定是否参与抽样)。"""
        return self._queue.full()

    # ── 状态(面板) ───────────────────────────────────────────

    async def service_status(self) -> dict[str, Any]:
        """聚合状态(供 /api/tts/status): 配置完整度 + 队列/计数。"""
        cur = self.current_job
        if self.cfg.backend == "minimax":
            configured = bool(self.cfg.api_key and self.cfg.voice_id)
            api_key_set = bool(self.cfg.api_key or os.environ.get("MOHOBOT_MINIMAX_API_KEY", ""))
            voice_id_set = bool(self.cfg.voice_id)
        else:
            configured = bool(self.cfg.base_url and self.cfg.api_key)
            api_key_set = bool(self.cfg.api_key or os.environ.get("MOHOBOT_TTS_API_KEY", ""))
            voice_id_set = None  # http 后端音色由服务端固定
        return {
            "tts_enabled": bool(self.cfg.enabled),
            "backend": self.cfg.backend,
            "configured": configured,
            "api_key_set": api_key_set,
            "voice_id_set": voice_id_set,
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

    # ── 面板试听(绕过队列直调后端) ───────────────────────────

    async def test_synthesize(self, text: str) -> dict[str, Any]:
        """面板「测试合成」: 直调后端合成 + 转码, 返回延迟与可播放音频。"""
        import time as _time
        started = _time.monotonic()
        audio, chars = await self._client.synthesize(text)
        latency_ms = int((_time.monotonic() - started) * 1000)
        if audio is None:
            return {"ok": False, "latency_ms": latency_ms, "error": "合成失败(见服务日志)"}
        fmt, audio_out = "wav", audio
        if self.cfg.convert_to_mp3:
            mp3 = await self._to_mp3(audio)
            if mp3 is not None:
                fmt, audio_out = "mp3", mp3
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "format": fmt,
            "size": len(audio_out),
            "chars": chars,
            "audio_b64": base64.b64encode(audio_out).decode(),
        }

    # ── ffmpeg wav → mp3 转码 ────────────────────────────────

    async def _to_mp3(self, wav: bytes) -> bytes | None:
        """wav → mp3(libmp3lame 128k); 失败/未安装返回 None(调用方降级发 wav)。"""
        if self._ffmpeg_ok is False or not self.cfg.convert_to_mp3:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0", "-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(wav), timeout=30)
            if proc.returncode == 0 and out:
                self._ffmpeg_ok = True
                return out
            logger.warning(f"ffmpeg 转码失败(rc={proc.returncode}), 按原始 wav 发送")
            return None
        except FileNotFoundError:
            self._ffmpeg_ok = False
            logger.warning("ffmpeg 未安装, 语音按原始 wav 发送(安装后自动恢复转码)")
            return None
        except Exception as e:
            logger.warning(f"ffmpeg 转码异常: {e}, 按原始 wav 发送")
            return None

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
        # issue #131 三时点校验: 任务可能是在 TTS 关闭/改配置前入队的,
        # worker 取到时配置已变则直接丢弃, 不合成、不发送。
        if self._job_is_stale(job):
            self.stats["dropped"] += 1
            logger.info(f"TTS 任务已过期(取到任务时), 丢弃: {job.text[:30]!r}")
            return
        audio, usage_chars = await self._client.synthesize(job.text, job.voice_id)
        # 合成期间被停用/配置代次变化: 不再发送, 也不计用量。
        if self._job_is_stale(job):
            self.stats["dropped"] += 1
            logger.info(f"TTS 任务已过期(合成期间), 丢弃: {job.text[:30]!r}")
            return
        if audio is None:
            self.stats["failed"] += 1
            logger.warning(f"TTS 合成失败({job.source}): {job.text[:30]!r}")
            if job.source == "command":
                await self._send_text(job, "语音合成失败，请稍后再试~")
            return
        self.stats["chars"] += usage_chars
        await self._record_usage(job, usage_chars)
        if self.cfg.backend != "minimax" and self.cfg.convert_to_mp3:
            mp3 = await self._to_mp3(audio)
            if mp3 is not None:
                audio = mp3
        if self._ws is None:
            logger.warning("TTS 语音发送跳过: ws_server 未注入")
            return
        # 转码耗时可能跨过一次配置热切换; 发送前最后确认(issue #131)。
        if self._job_is_stale(job):
            self.stats["dropped"] += 1
            logger.info(f"TTS 任务已过期(发送前), 丢弃: {job.text[:30]!r}")
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
            model = self.cfg.model if self.cfg.backend == "minimax" else "selfhost-http"
            await self._usage_recorder.record(
                None, model=model, bot_id=job.bot_id,
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
