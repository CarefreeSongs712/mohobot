"""MiniMax 云端 TTS 客户端 — t2a_v2 同步合成(保留仅下线)。

当前默认后端为自建 HTTP 服务(services/tts.py, backend="http");
本模块仅保留 MinimaxTTSClient, 配置 tts.backend="minimax" 时由
TTSService 经 _build_client 装配切回(改 WebUI 配置即热切换)。

接口: POST /v1/t2a_v2, 响应 JSON 中 data.audio 为 hex 编码音频,
extra_info.usage_characters 为计费字符数。音色为 MiniMax 克隆音色
(voice_id), 全局默认 + 每 bot BotConfig.tts_voice_id 覆盖。
"""

from __future__ import annotations

import binascii
import os
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
