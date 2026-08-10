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
    # 识图提示词: 发送给 VLM 的图片转述指令(WebUI 可编辑)
    vision_prompt: str = (
        "请将下方的图片转述为中文，描述时不要超过 100 字。图片转述时，可以参照下方的人物特征。\n"
        "\n"
        "洛天依\n"
        "--发型/头发：灰色头发（具体发型不固定），带有环状的麻花辫（\"8\"字形发髻），或紧紧并拢的双环呆毛\n"
        "--眼睛：绿色，但具体颜色不固定，黄绿色到青绿色都有可能\n"
        "乐正绫\n"
        "--发型：黑棕色（某些情况是黑色）头顶有呆毛，一般有麻花辫\n"
        "--眼睛：红瞳\n"
        "言和\n"
        "--发型：白色短发\n"
        "--眼睛：青蓝色\n"
        "乐正龙牙\n"
        "- 发型：半黑半白短发，额前碎发。背后黑白相间的麻花辫\n"
        "- 眼睛：深绿色\n"
        "徵羽摩柯\n"
        "- 发型：藏青色（深蓝色）短发，两侧有长鬓角\n"
        "- 眼睛：星空蓝（蓝色系）\n"
        "墨清弦\n"
        "- 发型：深紫色长发，披发造型，发尾蓬松\n"
        "- 眼睛：紫色（薰衣草紫）\n"
        "初音未来（Hatsune Miku）\n"
        "- 发型：蓝绿色双马尾，标志性的葱色发系\n"
        "- 眼睛：青绿色（也常描述为薄荷绿/湖蓝色）\n"
        "星尘\n"
        "- 发型：渐变淡紫色色长卷发，发梢呈星空蓝渐变，头顶有星型发饰\n"
        "- 眼睛：黄色/金黄色\n"
        "心华\n"
        "- 发型：紫色长散发\n"
        "- 眼睛：粉紫色（蔷薇紫/淡粉色）\n"
        "\n"
        "没有命中上面的特征人物正常描述图像的内容。如果出现人物且有其它物品/背景则都描述。"
        "如果只是猜测（部分特征相似）则注明。"
    )

    # 全局备用模型: beta 各 LLM 模块主模型遇到连接类错误(连接失败/超时)时
    # 自动换用此模型重试一次(仅连接类错误; 空 = 不回退)
    fallback_model: str = "DeepSeek-V4-Flash"

    # 可用模型列表(WebUI 预填, 供 beta 各 LLM 模块下拉选择; 可增删)
    models: list[str] = field(default_factory=lambda: [
        "DeepSeek-V4-Flash", "Qwen3-8B", "mimo-v2.5",
    ])


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
class DatabaseConfig:
    """数据库配置 — 与 Agent-LuoTianyi 共享同一个 SQLite 数据库文件。"""
    enabled: bool = True
    folder: str = "./data/database"
    file: str = "luotianyi.db"


@dataclass
class AgentConfig:
    """Agent 子系统全局配置(移植自 Agent-LuoTianyi,按 bot 隔离)。

    注意: 角色名/角色人设/说话风格不需要在此配置 —— 每个 bot 的
    persona 直接取自其私有配置(BotConfig.persona / nickname),
    "是否启用" 也在每个 bot 的私有配置里(agent_enabled)。
    """
    enabled: bool = True             # 全局总开关(各 bot 私有 agent_enabled 再单独控制)
    # beta 流水线 4 个 LLM 模块: 前两个(主回复/话题提取)默认主回复模型,
    # 后两个(记忆写入/用户画像)默认轻量模型; base_url/api_key 留空继承全局 llm。
    # 模型名在 WebUI 从可用模型列表(llm.models)下拉选择。
    llm_modules: dict = field(default_factory=lambda: {
        "main_chat": {"model": "DeepSeek-V4-Flash"},
        "topic_extractor": {"model": "DeepSeek-V4-Flash"},
        "memory_writer": {"model": "Qwen3-8B"},
        "user_profile_updater": {"model": "Qwen3-8B"},
    })
    memory: dict = field(default_factory=dict)         # vector_store / dedup 阈值等
    main_chat: dict = field(default_factory=dict)
    topic_planner: dict = field(default_factory=dict)  # listen_timer / unread_store
    topic_replier: dict = field(default_factory=dict)
    reflection_worker: dict = field(default_factory=dict)
    reflex: dict = field(default_factory=dict)
    music_knowledge: dict = field(default_factory=dict)  # 歌曲知识库(song_database/crawler/关键词文件)

    def to_config_dict(self) -> dict:
        """转成 agent 模块读取的嵌套 dict(供 runtime 使用)。

        注意: 必须包含 enabled,否则 save() 写入 yaml 后会丢开关。
        """
        return {
            "enabled": self.enabled,
            "llm_modules": self.llm_modules or {},
            "memory": self.memory or {},
            "main_chat": self.main_chat or {},
            "topic_planner": self.topic_planner or {},
            "topic_replier": self.topic_replier or {},
            "reflection_worker": self.reflection_worker or {},
            "reflex": self.reflex or {},
            "music_knowledge": self.music_knowledge or {},
        }


@dataclass
class AnySearchConfig:
    """Anysearch 实时联网搜索配置。api_key 为空时搜索自动降级为空结果。"""
    enabled: bool = True
    api_key: str = ""
    base_url: str = "https://api.anysearch.com/mcp"
    timeout: int = 30


@dataclass
class BanConfig:
    """封禁系统配置(全局统一名单, 所有 bot 共享)。

    封禁语义: bot 静默忽略被禁用户的消息(不是 QQ 群管理封禁)。
    管理员从顶层 GlobalConfig.admins 获取(ban 段不再单独配置 admins)。
    """
    enabled: bool = True


# beta 流水线 4 个 LLM 模块的默认模型:
# 前两个(主回复/话题提取)用主回复模型, 后两个(记忆/画像)用轻量模型
_DEFAULT_BETA_MODELS = {
    "main_chat": "DeepSeek-V4-Flash",
    "topic_extractor": "DeepSeek-V4-Flash",
    "memory_writer": "Qwen3-8B",
    "user_profile_updater": "Qwen3-8B",
}


def _fill_agent_llm_defaults(llm_modules: dict) -> dict:
    """填充 beta 各 LLM 模块的默认模型(旧配置 model 为空时生效)。"""
    out = dict(llm_modules or {})
    for mod, model in _DEFAULT_BETA_MODELS.items():
        spec = out.setdefault(mod, {})
        if isinstance(spec, dict) and not (spec.get("model") or "").strip():
            spec["model"] = model
    return out


@dataclass
class GlobalConfig:
    """Top-level global configuration."""
    beta_mode: bool = True     # true = Agent 流水线(beta 模式); false = 旧版直接流式回复(数据库保留)
    admins: list[int] = field(default_factory=list)  # 全局管理员 QQ 号(封禁/插件命令共用)
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    web_panel: WebPanelConfig = field(default_factory=WebPanelConfig)
    interceptor: InterceptorConfig = field(default_factory=InterceptorConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    anysearch: AnySearchConfig = field(default_factory=AnySearchConfig)
    ban: BanConfig = field(default_factory=BanConfig)
    log_dir: str = "./logs"
    data_dir: str = "./data"
    plugins_dir: str = "./plugins"
    context_max_rounds: int = 30
    # 上下文压缩: 满 trim_at_rounds 轮时, 用 AI 总结最早的 trim_remove_rounds 轮,
    # 总结作为新的块插入对话最前(旧内容直接裁剪)。enabled=False 时仅裁剪不总结。
    context_summary_enabled: bool = True
    context_trim_at_rounds: int = 40
    context_trim_remove_rounds: int = 15
    # 群聊最近消息: 生成回复时把群内最近 N 条消息临时注入 prompt(不写入 context,
    # 不参与 AI 总结压缩), 用于感知群聊氛围。0 = 关闭。
    group_recent_msgs_count: int = 10

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
        db_raw = raw.get("database", {})
        agent_raw = raw.get("agent", {})
        anysearch_raw = raw.get("anysearch", {})
        ban_raw = raw.get("ban", {})

        return cls(
            beta_mode=raw.get("beta_mode", True),
            # 顶层 admins 优先; 兼容旧配置 ban.admins(自动迁移)
            admins=[int(a) for a in (
                raw.get("admins") or ban_raw.get("admins") or []
            ) if str(a).isdigit()],
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
                # 空/缺失时保留默认人物特征提示词
                vision_prompt=(llm_raw.get("vision_prompt") or "").strip() or LLMConfig().vision_prompt,
                fallback_model=llm_raw.get("fallback_model", "DeepSeek-V4-Flash"),
                models=[str(m) for m in (llm_raw.get("models") or [
                    "DeepSeek-V4-Flash", "Qwen3-8B", "mimo-v2.5",
                ])],
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
            database=DatabaseConfig(
                enabled=db_raw.get("enabled", True),
                folder=db_raw.get("folder", "./data/database"),
                file=db_raw.get("file", "luotianyi.db"),
            ),
            agent=AgentConfig(
                enabled=agent_raw.get("enabled", True),
                llm_modules=_fill_agent_llm_defaults(agent_raw.get("llm_modules", {}) or {}),
                memory=agent_raw.get("memory", {}) or {},
                main_chat=agent_raw.get("main_chat", {}) or {},
                topic_planner=agent_raw.get("topic_planner", {}) or {},
                topic_replier=agent_raw.get("topic_replier", {}) or {},
                reflection_worker=agent_raw.get("reflection_worker", {}) or {},
                reflex=agent_raw.get("reflex", {}) or {},
                music_knowledge=agent_raw.get("music_knowledge", {}) or {},
            ),
            anysearch=AnySearchConfig(
                enabled=anysearch_raw.get("enabled", True),
                api_key=anysearch_raw.get("api_key", ""),
                base_url=anysearch_raw.get("base_url", "https://api.anysearch.com/mcp"),
                timeout=int(anysearch_raw.get("timeout", 30)),
            ),
            ban=BanConfig(
                enabled=ban_raw.get("enabled", True),
            ),
            log_dir=raw.get("log_dir", "./logs"),
            data_dir=raw.get("data_dir", "./data"),
            plugins_dir=raw.get("plugins_dir", "./plugins"),
            context_max_rounds=raw.get("context_max_rounds", 30),
            context_summary_enabled=bool(raw.get("context_summary_enabled", True)),
            context_trim_at_rounds=int(raw.get("context_trim_at_rounds", 40)),
            context_trim_remove_rounds=int(raw.get("context_trim_remove_rounds", 15)),
            group_recent_msgs_count=int(raw.get("group_recent_msgs_count", 10)),
        )

    def save(self, path: str | Path = "./config/global.yaml") -> None:
        """Serialize to YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        raw = {
            "beta_mode": self.beta_mode,
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
                "vision_prompt": self.llm.vision_prompt,
                "fallback_model": self.llm.fallback_model,
                "models": list(self.llm.models),
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
            "database": {
                "enabled": self.database.enabled,
                "folder": self.database.folder,
                "file": self.database.file,
            },
            "agent": self.agent.to_config_dict(),
            "anysearch": {
                "enabled": self.anysearch.enabled,
                "api_key": self.anysearch.api_key,
                "base_url": self.anysearch.base_url,
                "timeout": self.anysearch.timeout,
            },
            "admins": list(self.admins),
            "ban": {
                "enabled": self.ban.enabled,
            },
            "log_dir": self.log_dir,
            "data_dir": self.data_dir,
            "plugins_dir": self.plugins_dir,
            "context_max_rounds": self.context_max_rounds,
            "context_summary_enabled": self.context_summary_enabled,
            "context_trim_at_rounds": self.context_trim_at_rounds,
            "context_trim_remove_rounds": self.context_trim_remove_rounds,
            "group_recent_msgs_count": self.group_recent_msgs_count,
        }

        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, default_flow_style=False, allow_unicode=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to nested dict (for web panel editing)."""
        return {
            "beta_mode": self.beta_mode,
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
                "vision_prompt": self.llm.vision_prompt,
                "fallback_model": self.llm.fallback_model,
                "models": list(self.llm.models),
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
            "database": {
                "enabled": self.database.enabled,
                "folder": self.database.folder,
                "file": self.database.file,
            },
            "agent": self.agent.to_config_dict(),
            "anysearch": {
                "enabled": self.anysearch.enabled,
                "api_key": self.anysearch.api_key,
                "base_url": self.anysearch.base_url,
                "timeout": self.anysearch.timeout,
            },
            "admins": list(self.admins),
            "ban": {
                "enabled": self.ban.enabled,
            },
            "log_dir": self.log_dir,
            "data_dir": self.data_dir,
            "plugins_dir": self.plugins_dir,
            "context_max_rounds": self.context_max_rounds,
            "context_summary_enabled": self.context_summary_enabled,
            "context_trim_at_rounds": self.context_trim_at_rounds,
            "context_trim_remove_rounds": self.context_trim_remove_rounds,
            "group_recent_msgs_count": self.group_recent_msgs_count,
        }


# ── Bot Config ────────────────────────────────────────────────

@dataclass
class BotConfig:
    """Per-bot configuration, stored in data/bots/{bot_id}/config.json.

    bot_id 与 QQ 分离: bot_id 为内部标识(自动编号 bot_001...),
    qq 为绑定的 QQ 号(0 = 未绑定, 一个 bot 只能绑定一个 QQ,
    QQ 唯一绑定 —— 一个 QQ 只能被一个 bot 绑定)。
    """
    bot_id: str = ""  # 内部标识(自动编号), 决定数据目录名
    qq: int = 0       # 绑定的 QQ 号 (0 = 未绑定)
    nickname: str = ""
    persona: str = "你是 Mohobot，一个有用的 AI 助手。"  # System prompt
    enabled: bool = True
    agent_enabled: bool = True   # 本 bot 是否启用 Agent 子系统(beta 模式下的流水线)
    touch_replies: list[str] = field(default_factory=list)  # 戳一戳固定回复(空=用全局/默认)

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
            bot_id=raw.get("bot_id", ""),
            qq=raw.get("qq", 0),
            nickname=raw.get("nickname", ""),
            persona=raw.get("persona", "你是 Mohobot，一个有用的 AI 助手。"),
            enabled=raw.get("enabled", True),
            agent_enabled=raw.get("agent_enabled", True),
            touch_replies=list(raw.get("touch_replies", []) or []),
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
            "bot_id": self.bot_id,
            "qq": self.qq,
            "nickname": self.nickname,
            "persona": self.persona,
            "enabled": self.enabled,
            "agent_enabled": self.agent_enabled,
            "touch_replies": list(self.touch_replies),
            "chat_model_override": self.chat_model_override,
            "vision_model_override": self.vision_model_override,
            "command_prefix": self.command_prefix,
            "keyword_replies": self.keyword_replies,
        }