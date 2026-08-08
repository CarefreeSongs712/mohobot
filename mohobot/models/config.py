"""Configuration models — global YAML config and per-bot JSON config."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ── Global Config ─────────────────────────────────────────────

@dataclass
class ServerConfig:
    """WebSocket server configuration."""
    host: str = "0.0.0.0"
    port: int = 8060
    max_size: int = 10 * 1024 * 1024  # 10 MB max message size


@dataclass
class LLMConfig:
    """LLM provider configuration."""
    # Chat model (primary)
    chat_model: str = "deepseek-chat"
    chat_base_url: str = "https://api.deepseek.com"
    chat_api_key: str = ""
    chat_max_tokens: int = 4096
    chat_temperature: float = 0.7

    # Vision model
    vision_model: str = "qwen-vl-plus"
    vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_api_key: str = ""


@dataclass
class WebPanelConfig:
    """Web admin panel configuration."""
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9090
    username: str = "admin"
    password_hash: str = ""  # bcrypt hash — set via CLI or initial setup


@dataclass
class InterceptorConfig:
    """Interceptor configuration."""
    keyword_file: str = "./data/keywords.json"


@dataclass
class ReplyConfig:
    """LLM reply behavior (streaming / segmentation / delays)."""
    stream: bool = True            # 是否使用流式请求
    segment_reply: bool = True     # 是否分段回复(标点+长度分隔法)
    segment_min_len: int = 12      # 分段最小长度(字符,低于此长度不切)
    segment_max_len: int = 60      # 分段硬上限(字符,超过强制切)
    segment_delay_min: float = 0.2  # 分段发送随机延迟下限(秒)
    segment_delay_max: float = 0.5  # 分段发送随机延迟上限(秒)
    reply_quote: bool = True       # 是否引用触发回复的那条消息


@dataclass
class GlobalConfig:
    """Top-level global configuration."""
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    web_panel: WebPanelConfig = field(default_factory=WebPanelConfig)
    interceptor: InterceptorConfig = field(default_factory=InterceptorConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    log_dir: str = "./logs"
    data_dir: str = "./data"
    plugins_dir: str = "./plugins"
    context_max_rounds: int = 30

    @classmethod
    def load(cls, path: str | Path = "./config/global.yaml") -> "GlobalConfig":
        """Load from YAML file, filling missing values with defaults."""
        path = Path(path)
        if not path.exists():
            # Return defaults
            return cls()

        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        server_raw = raw.get("server", {})
        llm_raw = raw.get("llm", {})
        panel_raw = raw.get("web_panel", {})
        interceptor_raw = raw.get("interceptor", {})
        reply_raw = raw.get("reply", {})

        return cls(
            server=ServerConfig(
                host=server_raw.get("host", "0.0.0.0"),
                port=server_raw.get("port", 8080),
                max_size=server_raw.get("max_size", 10 * 1024 * 1024),
            ),
            llm=LLMConfig(
                chat_model=llm_raw.get("chat_model", "deepseek-chat"),
                chat_base_url=llm_raw.get("chat_base_url", "https://api.deepseek.com"),
                chat_api_key=llm_raw.get("chat_api_key", ""),
                chat_max_tokens=llm_raw.get("chat_max_tokens", 4096),
                chat_temperature=llm_raw.get("chat_temperature", 0.7),
                vision_model=llm_raw.get("vision_model", "qwen-vl-plus"),
                vision_base_url=llm_raw.get("vision_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                vision_api_key=llm_raw.get("vision_api_key", ""),
            ),
            web_panel=WebPanelConfig(
                enabled=panel_raw.get("enabled", True),
                host=panel_raw.get("host", "127.0.0.1"),
                port=panel_raw.get("port", 9090),
                username=panel_raw.get("username", "admin"),
                password_hash=panel_raw.get("password_hash", ""),
            ),
            interceptor=InterceptorConfig(
                keyword_file=interceptor_raw.get("keyword_file", "./data/keywords.json"),
            ),
            reply=ReplyConfig(
                stream=reply_raw.get("stream", True),
                segment_reply=reply_raw.get("segment_reply", True),
                segment_min_len=reply_raw.get("segment_min_len", 12),
                segment_max_len=reply_raw.get("segment_max_len", 60),
                segment_delay_min=reply_raw.get("segment_delay_min", 0.2),
                segment_delay_max=reply_raw.get("segment_delay_max", 0.5),
                reply_quote=reply_raw.get("reply_quote", True),
            ),
            log_dir=raw.get("log_dir", "./logs"),
            data_dir=raw.get("data_dir", "./data"),
            plugins_dir=raw.get("plugins_dir", "./plugins"),
            context_max_rounds=raw.get("context_max_rounds", 30),
        )

    def save(self, path: str | Path = "./config/global.yaml") -> None:
        """Serialize to YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        raw = {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "max_size": self.server.max_size,
            },
            "llm": {
                "chat_model": self.llm.chat_model,
                "chat_base_url": self.llm.chat_base_url,
                "chat_api_key": self.llm.chat_api_key,
                "chat_max_tokens": self.llm.chat_max_tokens,
                "chat_temperature": self.llm.chat_temperature,
                "vision_model": self.llm.vision_model,
                "vision_base_url": self.llm.vision_base_url,
                "vision_api_key": self.llm.vision_api_key,
            },
            "web_panel": {
                "enabled": self.web_panel.enabled,
                "host": self.web_panel.host,
                "port": self.web_panel.port,
                "username": self.web_panel.username,
                "password_hash": self.web_panel.password_hash,
            },
            "interceptor": {
                "keyword_file": self.interceptor.keyword_file,
            },
            "reply": {
                "stream": self.reply.stream,
                "segment_reply": self.reply.segment_reply,
                "segment_min_len": self.reply.segment_min_len,
                "segment_max_len": self.reply.segment_max_len,
                "segment_delay_min": self.reply.segment_delay_min,
                "segment_delay_max": self.reply.segment_delay_max,
                "reply_quote": self.reply.reply_quote,
            },
            "log_dir": self.log_dir,
            "data_dir": self.data_dir,
            "plugins_dir": self.plugins_dir,
            "context_max_rounds": self.context_max_rounds,
        }

        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, default_flow_style=False, allow_unicode=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to nested dict (for web panel editing)."""
        return {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "max_size": self.server.max_size,
            },
            "llm": {
                "chat_model": self.llm.chat_model,
                "chat_base_url": self.llm.chat_base_url,
                "chat_api_key": self.llm.chat_api_key,
                "chat_max_tokens": self.llm.chat_max_tokens,
                "chat_temperature": self.llm.chat_temperature,
                "vision_model": self.llm.vision_model,
                "vision_base_url": self.llm.vision_base_url,
                "vision_api_key": self.llm.vision_api_key,
            },
            "web_panel": {
                "enabled": self.web_panel.enabled,
                "host": self.web_panel.host,
                "port": self.web_panel.port,
                "username": self.web_panel.username,
                "password_hash": self.web_panel.password_hash,
            },
            "interceptor": {
                "keyword_file": self.interceptor.keyword_file,
            },
            "reply": {
                "stream": self.reply.stream,
                "segment_reply": self.reply.segment_reply,
                "segment_min_len": self.reply.segment_min_len,
                "segment_max_len": self.reply.segment_max_len,
                "segment_delay_min": self.reply.segment_delay_min,
                "segment_delay_max": self.reply.segment_delay_max,
                "reply_quote": self.reply.reply_quote,
            },
            "log_dir": self.log_dir,
            "data_dir": self.data_dir,
            "plugins_dir": self.plugins_dir,
            "context_max_rounds": self.context_max_rounds,
        }


# ── Bot Config ────────────────────────────────────────────────

@dataclass
class BotConfig:
    """Per-bot configuration, stored in data/bots/{bot_id}/config.json."""
    qq: int = 0
    nickname: str = ""
    persona: str = "你是 Mohobot，一个有用的 AI 助手。"  # System prompt
    enabled: bool = True

    # Per-bot LLM overrides (optional)
    chat_model_override: str = ""
    vision_model_override: str = ""

    # Interceptor settings
    command_prefix: str = "/"
    keyword_replies: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "BotConfig":
        """Load from JSON file."""
        path = Path(path)
        if not path.exists():
            return cls()

        import json
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        return cls(
            qq=raw.get("qq", 0),
            nickname=raw.get("nickname", ""),
            persona=raw.get("persona", "你是 Mohobot，一个有用的 AI 助手。"),
            enabled=raw.get("enabled", True),
            chat_model_override=raw.get("chat_model_override", ""),
            vision_model_override=raw.get("vision_model_override", ""),
            command_prefix=raw.get("command_prefix", "/"),
            keyword_replies=raw.get("keyword_replies", {}),
        )

    def save(self, path: str | Path) -> None:
        """Save to JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict (for web panel editing)."""
        return {
            "qq": self.qq,
            "nickname": self.nickname,
            "persona": self.persona,
            "enabled": self.enabled,
            "chat_model_override": self.chat_model_override,
            "vision_model_override": self.vision_model_override,
            "command_prefix": self.command_prefix,
            "keyword_replies": self.keyword_replies,
        }