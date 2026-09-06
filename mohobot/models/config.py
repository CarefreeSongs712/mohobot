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
    outbound_interval: float = 0.5
    outbound_maxsize: int = 100
    outbound_enqueue_timeout: float = 2.0


@dataclass
class LLMConfig:
    """LLM provider configuration."""
    # Chat model (primary)
    chat_model: str = "deepseek-chat"
    chat_base_url: str = "https://api.deepseek.com"
    chat_api_key: str = ""
    chat_max_tokens: int = 4096
    chat_temperature: float = 0.7

    # 上下文 AI 总结(压缩早期对话, 默认复用 chat 模型)
    summarize_temperature: float = 0.3
    summarize_max_tokens: int = 4096

    # 情感分析(二次 LLM, 留空则回退 chat 模型/密钥/地址)
    emotion_model: str = ""
    emotion_base_url: str = ""
    emotion_api_key: str = ""
    emotion_temperature: float = 0.3
    emotion_max_tokens: int = 512

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

    # 识图调用参数(推理型视觉模型思考会烧 token, 预算太小正文为空)
    vision_max_tokens: int = 2048
    vision_temperature: float = 0.3

    # 可用模型列表(WebUI 预填, 供识图模型等下拉选择; 可增删)
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
    password_hash: str = ""  # pbkdf2_sha256$salt$hex; MOHOBOT_WEB_PASSWORD may initialize it


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
    """数据库配置 — 会话持久化与面板备份/数据管理共用。"""
    enabled: bool = True
    folder: str = "./data/database"
    file: str = "luotianyi.db"


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


@dataclass
class EmotionConfig:
    """情感系统配置(移植自 astrbot-plugin-emotionai_pro 核心子集)。

    enabled 开关在启动时读取, 修改后需重启生效;
    其余阈值可通过 WebUI 保存热同步(EmotionManager.sync_config)。
    """
    enabled: bool = False            # 总开关(重启生效)
    smart_update: bool = True        # 智能按需调用情感分析 LLM(关闭则每轮都分析)
    force_update_interval: int = 10  # 每 N 轮强制触发一次情感分析
    significance_threshold: int = 5  # 情感变化达到该值才写入长期记忆
    favour_min: int = -100
    favour_max: int = 100
    intimacy_min: int = 0
    intimacy_max: int = 100


@dataclass
class TTSConfig:
    """TTS 语音合成配置(GPT-SoVITS api_v2)。

    GSV 相关全部全局: 所有 bot 共用同一套音色/模型/参考音频;
    每 bot 只有 tts_enabled 开关(BotConfig)。运行时不切权重,
    GSV 服务端启动时通过 tts_infer.yaml 自行加载模型。
    """
    enabled: bool = False
    base_url: str = "http://127.0.0.1:9880"
    # 单飞行队列: GSV 一次只能合成一条, 队列满时丢最新(新请求直接放弃)
    queue_maxsize: int = 16
    timeout: int = 60                # 单次合成超时(秒)
    media_type: str = "wav"          # wav / ogg / aac (ogg/aac 需 GSV 端 ffmpeg)
    text_lang: str = "zh"
    prompt_lang: str = "zh"
    # 参考音频为 GSV 服务器本机路径
    ref_audio_path: str = ""
    prompt_text: str = ""
    speed_factor: float = 1.0
    # LLM 自动朗读的系统提示词模板(开启 TTS 的 bot 注入)
    tts_prompt_template: str = (
        "\n\n语音标注规则：如果你想说一句适合朗读出来的话（例如问候、感叹、俏皮话），"
        "可以用 <tts></tts> 标签把它包起来，系统会把它转成语音发送。"
        "标注是可选的，多数时候可以不标；标注内容尽量不超过 20 字；"
        "标签内的文字仍会正常显示在聊天中。"
    )
    # 指令 TTS(/tts) 限制(管理员不受限)
    cmd_max_chars: int = 30
    cmd_cooldown: int = 120          # 非管理员冷却(秒)


# ── Global Config (旧 agent.beta 相关配置已在 dev 分支移除) ────


@dataclass
class GlobalConfig:
    """Top-level global configuration."""
    admins: list[int] = field(default_factory=list)  # 全局管理员 QQ 号(封禁/插件命令共用)
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    web_panel: WebPanelConfig = field(default_factory=WebPanelConfig)
    interceptor: InterceptorConfig = field(default_factory=InterceptorConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    anysearch: AnySearchConfig = field(default_factory=AnySearchConfig)
    ban: BanConfig = field(default_factory=BanConfig)
    # 情感系统(好感度/亲密度/关系阶段/长期记忆; emotion.enabled 开关)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    # TTS 语音合成(GPT-SoVITS api_v2; 每 bot 开关在 BotConfig.tts_enabled)
    tts: TTSConfig = field(default_factory=TTSConfig)
    # 歌曲知识库(识别 + LLM 前注入; song_database/crawler/关键词文件)
    music_knowledge: dict = field(default_factory=dict)
    # 戳一戳全局兜底文案(bot 私有 touch_replies 优先, 都为空时用内置默认)
    touch_replies: list[str] = field(default_factory=list)
    log_dir: str = "./logs"
    data_dir: str = "./data"
    plugins_dir: str = "./plugins"
    # 上下文压缩: 满 trim_at_rounds 轮时, 用 AI 总结最早的 trim_remove_rounds 轮,
    # 总结作为新的块插入对话最前(旧内容直接裁剪)。enabled=False 时仅裁剪不总结。
    context_summary_enabled: bool = True
    context_trim_at_rounds: int = 40
    context_trim_remove_rounds: int = 15
    # 时间压缩(满轮顺带 + 周期任务): 对话超过该时长即视为旧对话;
    # 周期任务按间隔扫描, 把旧对话交给 AI 总结(已压缩会话距上次压缩不足
    # min_interval_hours 则不重复压缩)。
    context_summary_age_hours: int = 3
    context_summary_sweep_enabled: bool = True
    context_summary_sweep_interval_minutes: int = 30
    context_summary_min_interval_hours: int = 24
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
        anysearch_raw = raw.get("anysearch", {})
        ban_raw = raw.get("ban", {})
        emotion_raw = raw.get("emotion", {})
        tts_raw = raw.get("tts", {})

        # 旧配置迁移: agent.music_knowledge / agent.reflex.touch_replies
        # 曾嵌在已删除的 agent: 段下, 读到旧键时搬到顶层并重写配置文件
        agent_raw = raw.get("agent", {}) or {}
        music_raw = raw.get("music_knowledge")
        migrated = False
        if music_raw is None:
            legacy_music = agent_raw.get("music_knowledge")
            if legacy_music:
                music_raw = legacy_music
                migrated = True
            else:
                music_raw = {}
        touch_raw = raw.get("touch_replies")
        if touch_raw is None:
            legacy_touch = (agent_raw.get("reflex") or {}).get("touch_replies")
            if legacy_touch:
                touch_raw = legacy_touch
                migrated = True
            else:
                touch_raw = []

        cfg = cls(
            # 顶层 admins 优先; 兼容旧配置 ban.admins(自动迁移)
            admins=[int(a) for a in (
                raw.get("admins") or ban_raw.get("admins") or []
            ) if str(a).isdigit()],
            server=ServerConfig(
                host=server_raw.get("host", "0.0.0.0"),
                port=server_raw.get("port", 8060),
                max_size=server_raw.get("max_size", 10 * 1024 * 1024),
                outbound_interval=server_raw.get("outbound_interval", 0.5),
                outbound_maxsize=server_raw.get("outbound_maxsize", 100),
                outbound_enqueue_timeout=server_raw.get("outbound_enqueue_timeout", 2.0),
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
                vision_max_tokens=int(llm_raw.get("vision_max_tokens", 2048)),
                vision_temperature=float(llm_raw.get("vision_temperature", 0.3)),
                summarize_temperature=float(llm_raw.get("summarize_temperature", 0.3)),
                summarize_max_tokens=int(llm_raw.get("summarize_max_tokens", 4096)),
                emotion_model=str(llm_raw.get("emotion_model", "") or ""),
                emotion_base_url=str(llm_raw.get("emotion_base_url", "") or ""),
                emotion_api_key=str(llm_raw.get("emotion_api_key", "") or ""),
                emotion_temperature=float(llm_raw.get("emotion_temperature", 0.3)),
                emotion_max_tokens=int(llm_raw.get("emotion_max_tokens", 512)),
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
            anysearch=AnySearchConfig(
                enabled=anysearch_raw.get("enabled", True),
                api_key=anysearch_raw.get("api_key", ""),
                base_url=anysearch_raw.get("base_url", "https://api.anysearch.com/mcp"),
                timeout=int(anysearch_raw.get("timeout", 30)),
            ),
            ban=BanConfig(
                enabled=ban_raw.get("enabled", True),
            ),
            emotion=EmotionConfig(
                enabled=bool(emotion_raw.get("enabled", False)),
                smart_update=bool(emotion_raw.get("smart_update", True)),
                force_update_interval=int(emotion_raw.get("force_update_interval", 10)),
                significance_threshold=int(emotion_raw.get("significance_threshold", 5)),
                favour_min=int(emotion_raw.get("favour_min", -100)),
                favour_max=int(emotion_raw.get("favour_max", 100)),
                intimacy_min=int(emotion_raw.get("intimacy_min", 0)),
                intimacy_max=int(emotion_raw.get("intimacy_max", 100)),
            ),
            tts=TTSConfig(
                enabled=bool(tts_raw.get("enabled", False)),
                base_url=str(tts_raw.get("base_url", "http://127.0.0.1:9880") or "http://127.0.0.1:9880"),
                queue_maxsize=max(1, int(tts_raw.get("queue_maxsize", 16))),
                timeout=max(5, int(tts_raw.get("timeout", 60))),
                media_type=str(tts_raw.get("media_type", "wav") or "wav"),
                text_lang=str(tts_raw.get("text_lang", "zh") or "zh"),
                prompt_lang=str(tts_raw.get("prompt_lang", "zh") or "zh"),
                ref_audio_path=str(tts_raw.get("ref_audio_path", "") or ""),
                prompt_text=str(tts_raw.get("prompt_text", "") or ""),
                speed_factor=float(tts_raw.get("speed_factor", 1.0)),
                tts_prompt_template=(
                    str(tts_raw.get("tts_prompt_template", "") or "").strip()
                    or TTSConfig().tts_prompt_template
                ),
                cmd_max_chars=max(1, int(tts_raw.get("cmd_max_chars", 30))),
                cmd_cooldown=max(0, int(tts_raw.get("cmd_cooldown", 120))),
            ),
            log_dir=raw.get("log_dir", "./logs"),
            data_dir=raw.get("data_dir", "./data"),
            plugins_dir=raw.get("plugins_dir", "./plugins"),
            context_summary_enabled=bool(raw.get("context_summary_enabled", True)),
            context_trim_at_rounds=int(raw.get("context_trim_at_rounds", 40)),
            context_trim_remove_rounds=int(raw.get("context_trim_remove_rounds", 15)),
            context_summary_age_hours=max(1, int(raw.get("context_summary_age_hours", 3))),
            context_summary_sweep_enabled=bool(raw.get("context_summary_sweep_enabled", True)),
            context_summary_sweep_interval_minutes=max(
                1, int(raw.get("context_summary_sweep_interval_minutes", 30))
            ),
            context_summary_min_interval_hours=max(
                1, int(raw.get("context_summary_min_interval_hours", 24))
            ),
            group_recent_msgs_count=int(raw.get("group_recent_msgs_count", 10)),
            music_knowledge=dict(music_raw or {}),
            touch_replies=[str(t) for t in (touch_raw or [])],
        )
        if migrated:
            cfg.save(path)
        return cfg

    def save(self, path: str | Path = "./config/global.yaml") -> None:
        """Serialize to YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        raw = {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "max_size": self.server.max_size,
                "outbound_interval": self.server.outbound_interval,
                "outbound_maxsize": self.server.outbound_maxsize,
                "outbound_enqueue_timeout": self.server.outbound_enqueue_timeout,
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
                "vision_max_tokens": self.llm.vision_max_tokens,
                "vision_temperature": self.llm.vision_temperature,
                "summarize_temperature": self.llm.summarize_temperature,
                "summarize_max_tokens": self.llm.summarize_max_tokens,
                "emotion_model": self.llm.emotion_model,
                "emotion_base_url": self.llm.emotion_base_url,
                "emotion_api_key": self.llm.emotion_api_key,
                "emotion_temperature": self.llm.emotion_temperature,
                "emotion_max_tokens": self.llm.emotion_max_tokens,
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
            "emotion": {
                "enabled": self.emotion.enabled,
                "smart_update": self.emotion.smart_update,
                "force_update_interval": self.emotion.force_update_interval,
                "significance_threshold": self.emotion.significance_threshold,
                "favour_min": self.emotion.favour_min,
                "favour_max": self.emotion.favour_max,
                "intimacy_min": self.emotion.intimacy_min,
                "intimacy_max": self.emotion.intimacy_max,
            },
            "tts": {
                "enabled": self.tts.enabled,
                "base_url": self.tts.base_url,
                "queue_maxsize": self.tts.queue_maxsize,
                "timeout": self.tts.timeout,
                "media_type": self.tts.media_type,
                "text_lang": self.tts.text_lang,
                "prompt_lang": self.tts.prompt_lang,
                "ref_audio_path": self.tts.ref_audio_path,
                "prompt_text": self.tts.prompt_text,
                "speed_factor": self.tts.speed_factor,
                "tts_prompt_template": self.tts.tts_prompt_template,
                "cmd_max_chars": self.tts.cmd_max_chars,
                "cmd_cooldown": self.tts.cmd_cooldown,
            },
            "music_knowledge": dict(self.music_knowledge or {}),
            "touch_replies": list(self.touch_replies),
            "log_dir": self.log_dir,
            "data_dir": self.data_dir,
            "plugins_dir": self.plugins_dir,
            "context_summary_enabled": self.context_summary_enabled,
            "context_trim_at_rounds": self.context_trim_at_rounds,
            "context_trim_remove_rounds": self.context_trim_remove_rounds,
            "context_summary_age_hours": self.context_summary_age_hours,
            "context_summary_sweep_enabled": self.context_summary_sweep_enabled,
            "context_summary_sweep_interval_minutes": self.context_summary_sweep_interval_minutes,
            "context_summary_min_interval_hours": self.context_summary_min_interval_hours,
            "group_recent_msgs_count": self.group_recent_msgs_count,
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
                "outbound_interval": self.server.outbound_interval,
                "outbound_maxsize": self.server.outbound_maxsize,
                "outbound_enqueue_timeout": self.server.outbound_enqueue_timeout,
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
                "vision_max_tokens": self.llm.vision_max_tokens,
                "vision_temperature": self.llm.vision_temperature,
                "summarize_temperature": self.llm.summarize_temperature,
                "summarize_max_tokens": self.llm.summarize_max_tokens,
                "emotion_model": self.llm.emotion_model,
                "emotion_base_url": self.llm.emotion_base_url,
                "emotion_api_key": self.llm.emotion_api_key,
                "emotion_temperature": self.llm.emotion_temperature,
                "emotion_max_tokens": self.llm.emotion_max_tokens,
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
            "emotion": {
                "enabled": self.emotion.enabled,
                "smart_update": self.emotion.smart_update,
                "force_update_interval": self.emotion.force_update_interval,
                "significance_threshold": self.emotion.significance_threshold,
                "favour_min": self.emotion.favour_min,
                "favour_max": self.emotion.favour_max,
                "intimacy_min": self.emotion.intimacy_min,
                "intimacy_max": self.emotion.intimacy_max,
            },
            "tts": {
                "enabled": self.tts.enabled,
                "base_url": self.tts.base_url,
                "queue_maxsize": self.tts.queue_maxsize,
                "timeout": self.tts.timeout,
                "media_type": self.tts.media_type,
                "text_lang": self.tts.text_lang,
                "prompt_lang": self.tts.prompt_lang,
                "ref_audio_path": self.tts.ref_audio_path,
                "prompt_text": self.tts.prompt_text,
                "speed_factor": self.tts.speed_factor,
                "tts_prompt_template": self.tts.tts_prompt_template,
                "cmd_max_chars": self.tts.cmd_max_chars,
                "cmd_cooldown": self.tts.cmd_cooldown,
            },
            "music_knowledge": dict(self.music_knowledge or {}),
            "touch_replies": list(self.touch_replies),
            "log_dir": self.log_dir,
            "data_dir": self.data_dir,
            "plugins_dir": self.plugins_dir,
            "context_summary_enabled": self.context_summary_enabled,
            "context_trim_at_rounds": self.context_trim_at_rounds,
            "context_trim_remove_rounds": self.context_trim_remove_rounds,
            "context_summary_age_hours": self.context_summary_age_hours,
            "context_summary_sweep_enabled": self.context_summary_sweep_enabled,
            "context_summary_sweep_interval_minutes": self.context_summary_sweep_interval_minutes,
            "context_summary_min_interval_hours": self.context_summary_min_interval_hours,
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
    touch_replies: list[str] = field(default_factory=list)  # 戳一戳固定回复(空=用全局/默认)

    # Per-bot LLM overrides (optional)
    chat_model_override: str = ""
    vision_model_override: str = ""

    # TTS 语音: 是否开启该 bot 的 LLM 自动朗读与 /tts 指令(GSV 全局配置共用)
    tts_enabled: bool = False

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
            touch_replies=list(raw.get("touch_replies", []) or []),
            chat_model_override=raw.get("chat_model_override", ""),
            vision_model_override=raw.get("vision_model_override", ""),
            tts_enabled=bool(raw.get("tts_enabled", False)),
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
            "touch_replies": list(self.touch_replies),
            "chat_model_override": self.chat_model_override,
            "vision_model_override": self.vision_model_override,
            "tts_enabled": self.tts_enabled,
            "command_prefix": self.command_prefix,
            "keyword_replies": self.keyword_replies,
        }