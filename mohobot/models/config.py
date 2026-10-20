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
    # 已废弃: 出站队列满时现在立即抛 OutboundQueueFullError, 不再等待。
    # 字段保留仅为兼容既有配置文件, 修改它不会改变行为。
    outbound_enqueue_timeout: float = 2.0


@dataclass
class LLMConfig:
    """LLM provider configuration.

    支持**多提供商**：`providers` 里每项是一套独立的 base_url + api_key +
    默认模型（例如 snowluma / deepseek 两套 key 并存），全局用
    `chat_provider` / `vision_provider` / `emotion_provider` 指定默认用哪套；
    每个 bot 还可用 `chat_provider_override` 等单独指定（见 BotConfig）。
    未指定提供商时回退到旧的 `*_base_url` / `*_api_key` 字段，保持兼容。

    providers 项结构（字段都可选，缺省回退旧字段）：
        {id, name, base_url, api_key, default_model, models: [..]}
    """
    # Chat model (primary)
    chat_model: str = "deepseek-chat"
    chat_base_url: str = "https://api.deepseek.com"
    chat_api_key: str = ""
    chat_max_tokens: int = 4096
    chat_temperature: float = 0.7
    # 重复惩罚(OOC 治理): 抑制复读死循环与模板化套话。0 = 关闭, 推荐 0.1~0.4。
    # 部分网关/模型不支持这两个参数, 留 0 时不传参(保持旧请求形状)。
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0

    # 上下文 AI 总结(压缩早期对话, 默认复用 chat 模型)
    summarize_temperature: float = 0.3
    summarize_max_tokens: int = 4096

    # 情感分析(二次 LLM, 留空则回退 chat 模型/密钥/地址)
    emotion_model: str = ""
    emotion_base_url: str = ""
    emotion_api_key: str = ""
    emotion_temperature: float = 0.3
    emotion_max_tokens: int = 512

    # ── 多提供商(可选) ───────────────────────────────────────
    # 每项: {"id": 唯一标识, "name": 显示名, "base_url": ..., "api_key": ...,
    #        "default_model": ..., "models": [...]}
    providers: list[dict] = field(default_factory=list)
    chat_provider: str = ""       # 默认 chat 提供商 id; 空 = 用 chat_base_url/chat_api_key
    vision_provider: str = ""     # 默认 vision 提供商 id; 空 = 用 vision_base_url/...
    emotion_provider: str = ""    # 默认 emotion 提供商 id; 空 = 用 emotion_base_url/...

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

    # 识图模式:
    #   describe(默认) = 先调独立视觉模型转述成文本, 主模型只收文本(兼容纯文本主模型)
    #   direct         = 把图片原始内容随文本一起发给主模型(要求 chat 模型本身支持视觉,
    #                    可省一次视觉模型调用; 请求失败会自动回退 describe)
    vision_mode: str = "describe"

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
    # OOC 治理: 清理中文回复里突兀的英文话语标记与 AI 套话
    # (Well,/Okay so,/As an AI…)。只清理白名单句首词, 不影响正常英文内容。
    strip_english_fillers: bool = True


@dataclass
class DatabaseConfig:
    """数据库配置 — 会话持久化与面板备份/数据管理共用。"""
    enabled: bool = True
    folder: str = "./data/database"
    file: str = "luotianyi.db"


@dataclass
class NewsConfig:
    """新闻/动态知识库(REQ-6) — 采集入库 → 管理员审核 → 检索与提示词注入。

    默认关闭(enabled=False), 开启后:
    - 只有**已审核通过**且未过期的条目会进入 LLM 上下文或工具结果;
    - 新条目(采集/LLM 提取/人工)一律以 pending 入库, 需 WebUI 管理员通过;
    - inject_* 控制被动注入, 工具 news_recent/news_search 由模型主动调用。
    db_path 留空 = <data_dir>/news/news.db。
    """
    enabled: bool = False
    db_path: str = ""
    inject_enabled: bool = True          # 是否在 system prompt 注入近期动态
    inject_limit: int = 3                # 注入条数
    inject_window_days: int = 7          # 只注入最近 N 天发布的
    inject_header: str = ""              # 留空用内置表头
    default_ttl_days: float = 14.0       # 条目默认有效期(天), <=0 表示不自动过期
    tool_enabled: bool = True            # 是否注册 news_recent/news_search 工具
    # ── B站采集器(默认关闭, 关闭时不发任何网络请求) ──
    collector_enabled: bool = False
    collector_uids: list[str] = field(default_factory=list)   # 关注的 B站 UID
    collector_interval_seconds: float = 1800.0                # 轮询间隔(秒), 最小 300
    collector_max_items_per_uid: int = 5                      # 单 UID 每轮最多提交条数
    collector_ttl_days: float = 14.0                          # 采集条目有效期(天)


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
    analysis_timeout_sec: int = 90   # 情感分析单次 LLM 调用超时(秒; 推理型慢模型/高负载时调大)
    # 调用频率控制(WebUI 可调, 热生效):
    keyword_threshold: int = 3       # 关键词门控触发阈值(用户消息累计情感强度; 越大越保守)
    analysis_round_cooldown: int = 2 # 距上次分析不足 N 轮则跳过本轮(0=不限)
    min_interval_sec: int = 180      # 同一用户两次情感分析的最小时间间隔(秒, 0=不限)
    # 并发上限(变更后重启生效; 信号量容量不可热重建):
    max_concurrent: int = 4          # 同时执行的情感分析 LLM 调用上限(1=退回串行)
    # 队列积压时改用快速模型 burst(把积压快速消化掉, 再回到正常模型):
    queue_burst_threshold: int = 5        # 排队任务超过该值时启用
    queue_burst_count: int = 3            # 快速模型连续调用次数
    burst_model: str = "DeepSeek-V4-Flash"  # 快速模型名(留空=不启用 burst)
    significance_threshold: int = 5  # 情感变化达到该值才写入长期记忆
    favour_min: int = -100
    favour_max: int = 100
    intimacy_min: int = 0
    intimacy_max: int = 100


@dataclass
class TTSConfig:
    """TTS 语音合成配置(可切换后端: 自建 HTTP 服务 / MiniMax 云端)。

    backend="http"(默认): 自建 HTTP 合成服务(POST {base_url}/tts,
    Bearer 鉴权, {"text":...} → wav 字节流), 本地 ffmpeg 转 mp3 后发送。
    backend="minimax": MiniMax t2a_v2 云端合成(保留仅下线, 改 backend 即切回),
    音色为 MiniMax 克隆音色, 全局 voice_id + 每 bot BotConfig.tts_voice_id 覆盖。

    队列/热同步语义: 除 queue_maxsize(重启生效)外, 其余字段经
    TTSService.sync_config 原位热同步(WebUI 保存即生效, 含后端切换)。
    """
    enabled: bool = False
    # http = 自建 HTTP 服务(默认); minimax = MiniMax 云端(保留仅下线)
    backend: str = "http"
    base_url: str = "http://127.0.0.1:9890"
    api_key: str = ""                          # Bearer token; MOHOBOT_TTS_API_KEY 环境变量兜底
    # 单飞行队列: 串行合成控费控速, 队列满时丢最新(新请求直接放弃)
    queue_maxsize: int = 16
    timeout: int = 60                # 单次合成超时(秒)
    # http 后端: 服务端返回 wav, 本地 ffmpeg 转 mp3 后发送(NapCat 兼容性已验证;
    # ffmpeg 不可用/转换失败自动降级发原始 wav)。仅 http 后端生效。
    convert_to_mp3: bool = True
    # ── 以下仅 backend=minimax 时生效 ─────────────────────────
    model: str = "speech-2.8-hd"               # speech-2.8-hd / speech-2.8-turbo
    voice_id: str = ""                         # 全局默认音色(如 ltyclone01)
    speed: float = 1.0
    vol: float = 1.0
    pitch: int = 0
    sample_rate: int = 32000
    bitrate: int = 128000
    format: str = "mp3"
    # LLM 自动朗读的系统提示词模板(开启 TTS 的 bot 注入)
    tts_prompt_template: str = (
        "\n\n语音标注规则：请尽量在每条回复里都用 <tts></tts> 标签标出想朗读给对方听的内容，"
        "基本做到每条都标；标注多少与长短完全由你决定——可以是一句话、几段话，"
        "甚至整条回复，不限字数。标签内的文字仍会正常显示在聊天中。"
    )
    # 指令 TTS(/tts) 限制(管理员不受限)
    cmd_max_chars: int = 30
    cmd_cooldown: int = 120          # 非管理员冷却(秒)


@dataclass
class KnowledgeConfig:
    """虚拟歌手知识库(RAG) — embedding 语义检索 + 可选 rerank。

    LLM 工具接入(knowledge_search/knowledge_lookup), 与歌曲知识
    (music_knowledge, 关键词/歌词确定性匹配)互补并存。
    enabled 开关启动时读取(修改后需重启); 其余字段经
    KnowledgeService.sync_config 热同步, embedding 模型变更自动重建索引。

    密钥回退链: embedding_api_key → MOHOBOT_EMBEDDING_API_KEY → chat key;
    rerank_api_key → MOHOBOT_RERANK_API_KEY → embedding key。
    rerank_model 留空 = 跳过重排(纯 embedding 余弦检索)。
    """
    enabled: bool = False
    source_dir: str = "./data/knowledge_db"      # 知识源(md + index.json)
    index_dir: str = "./data/knowledge_index"    # 向量索引 SQLite(可整目录删除重建)
    embedding_model: str = "BAAI/bge-m3"
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str = ""
    rerank_model: str = ""                       # 如 BAAI/bge-reranker-v2-m3
    rerank_base_url: str = "https://api.siliconflow.cn/v1"
    rerank_api_key: str = ""
    search_top_k: int = 20                       # 余弦召回数(rerank 候选池)
    search_top_n: int = 5                        # 最终返回给模型的块数
    max_chars_per_result: int = 800              # 单块注入正文的字符截断


# ── Global Config (旧 agent.beta 相关配置已在 dev 分支移除) ────


def _normalize_vision_mode(value) -> str:
    """识图模式归一化: 只接受 describe / direct, 其余(含空)回退 describe。"""
    mode = str(value or "").strip().lower()
    return mode if mode in ("describe", "direct") else "describe"


def _normalize_providers(value) -> list[dict]:
    """归一化 llm.providers: 只保留有 id 的项, 字段转字符串并去重。

    宽容处理: 接受 list[dict]; 非法项直接跳过而不是让整个配置加载失败
    (配置错误不应导致服务起不来)。
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        models = item.get("models")
        if isinstance(models, str):
            models = [m.strip() for m in models.replace("，", ",").split(",") if m.strip()]
        elif isinstance(models, (list, tuple)):
            models = [str(m).strip() for m in models if str(m).strip()]
        else:
            models = []
        out.append({
            "id": pid,
            "name": str(item.get("name") or pid).strip() or pid,
            "base_url": str(item.get("base_url") or "").strip(),
            "api_key": str(item.get("api_key") or "").strip(),
            "default_model": str(item.get("default_model") or "").strip(),
            "models": models,
        })
    return out


def _int_list(value) -> list[int]:
    """宽松解析整数列表: 接受 list[str|int] 逗号分隔字符串; 忽略非整数字符串。"""
    if isinstance(value, str):
        value = [p for p in value.split(",")]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        s = str(item).strip()
        if s.isdigit():
            out.append(int(s))
    return out


def is_external_service_group(group_id, configured_groups=()) -> bool:
    """Return whether a group is explicitly reserved for an external service."""
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        return False
    return group_id in _int_list(configured_groups)


@dataclass
class PersonaABConfig:
    """Private, single-turn preference evaluation; bot allowlist is not configurable."""
    enabled: bool = False
    baseline_id: str = ""
    test_id: str = ""
    probability: float = 0.05
    cooldown_seconds: int = 900
    pending_seconds: int = 86400
    max_pages: int = 8

    @classmethod
    def from_dict(cls, raw):
        from dataclasses import fields
        import math
        if not isinstance(raw, dict):
            raise ValueError("persona_ab 必须是对象")
        if set(raw) - {f.name for f in fields(cls)}:
            raise ValueError("persona_ab 包含未知配置")
        result = cls(**raw)
        if type(result.enabled) is not bool:
            raise ValueError("enabled 必须为布尔值")
        for key in ("baseline_id", "test_id"):
            if not isinstance(getattr(result, key), str):
                raise ValueError("人设 ID 必须为字符串")
        if type(result.probability) not in (float, int) or not math.isfinite(result.probability) or not 0 <= result.probability <= 1:
            raise ValueError("抽样概率必须为 0 到 1")
        for key, minimum, maximum in (("cooldown_seconds", 0, 31536000), ("pending_seconds", 1, 31536000), ("max_pages", 1, 20)):
            value = getattr(result, key)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{key} 超出范围")
        return result

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


@dataclass
class GlobalConfig:
    """Top-level global configuration."""
    admins: list[int] = field(default_factory=list)  # 全局管理员 QQ 号(封禁/插件命令共用)
    # 用量统计排除的模型: 这些模型的调用不计入 WebUI 用量统计(记录仍落盘, 重启生效)
    usage_excluded_models: list[str] = field(default_factory=list)
    # LLM 排除群: 这些群号内 bot 不做 LLM 聊天回复(@/引用/ping 均静默), 插件与
    # 命令不受影响(WebPanel 保存 setattr 同一实例, 天然热生效)
    llm_excluded_groups: list[int] = field(default_factory=list)
    # 由外部服务(如 Koishi)独占处理的群; 框架收到消息后仍先归档, 随后完全静默。
    external_service_groups: list[int] = field(default_factory=list)
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    web_panel: WebPanelConfig = field(default_factory=WebPanelConfig)
    interceptor: InterceptorConfig = field(default_factory=InterceptorConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    persona_ab: PersonaABConfig = field(default_factory=PersonaABConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    anysearch: AnySearchConfig = field(default_factory=AnySearchConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    ban: BanConfig = field(default_factory=BanConfig)
    # 情感系统(好感度/亲密度/关系阶段/长期记忆; emotion.enabled 开关)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    # TTS 语音合成(MiniMax t2a_v2 云端 API; 每 bot 开关在 BotConfig.tts_enabled)
    tts: TTSConfig = field(default_factory=TTSConfig)
    # 虚拟歌手知识库(RAG): embedding 语义检索 + 可选 rerank, LLM 工具接入
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
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
    # 告状工具(snitch): LLM 判定遭骚扰/违规时可向该管理群发告状消息,
    # 消息由触发会话的 bot 署名发送。0 = 关闭。热生效(WebUI 保存同步同实例)。
    snitch_admin_group: int = 1070473353
    # 私聊自动回复过滤: 命中即静默丢弃(不回复/不写上下文/不入库, history 归档保留)。
    # 判定规则写死: ① 文本以「[自动回复]」开头; ② 同一用户连续 3 条相同文本且
    # 相邻间隔 < 5 分钟(QQ 自动回复无统一标记, 见 docs/DEVELOPMENT.md)。
    ignore_auto_reply: bool = True
    # history 双写过渡: 群聊消息除合并存储(data/history/group/{群号}.jsonl)外,
    # 仍按旧布局 data/history/{bot_id}/group/{群号}.jsonl 归档一份(回滚保险)。
    # 确认新布局稳定后改 false 停止旧写入(热生效)。
    history_dual_write: bool = True

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
        news_raw = raw.get("news", {})
        ban_raw = raw.get("ban", {})
        emotion_raw = raw.get("emotion", {})
        tts_raw = raw.get("tts", {})
        knowledge_raw = raw.get("knowledge", {})

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
            usage_excluded_models=[
                str(m).strip() for m in (raw.get("usage_excluded_models") or [])
                if str(m).strip()
            ],
            llm_excluded_groups=_int_list(raw.get("llm_excluded_groups")),
            external_service_groups=_int_list(raw.get("external_service_groups")),
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
                providers=_normalize_providers(llm_raw.get("providers")),
                chat_provider=str(llm_raw.get("chat_provider", "") or "").strip(),
                vision_provider=str(llm_raw.get("vision_provider", "") or "").strip(),
                emotion_provider=str(llm_raw.get("emotion_provider", "") or "").strip(),
                chat_temperature=llm_raw.get("chat_temperature", 0.7),
                frequency_penalty=float(llm_raw.get("frequency_penalty", 0.0) or 0.0),
                presence_penalty=float(llm_raw.get("presence_penalty", 0.0) or 0.0),
                vision_model=llm_raw.get("vision_model", "qwen-vl-plus"),
                vision_base_url=llm_raw.get("vision_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                vision_api_key=llm_raw.get("vision_api_key", ""),
                # 空/缺失时保留默认人物特征提示词
                vision_prompt=(llm_raw.get("vision_prompt") or "").strip() or LLMConfig().vision_prompt,
                vision_max_tokens=int(llm_raw.get("vision_max_tokens", 2048)),
                vision_temperature=float(llm_raw.get("vision_temperature", 0.3)),
                vision_mode=_normalize_vision_mode(llm_raw.get("vision_mode")),
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
            persona_ab=PersonaABConfig.from_dict(raw.get("persona_ab", {})),
            reply=ReplyConfig(
                stream=reply_raw.get("stream", True),
                segment_reply=reply_raw.get("segment_reply", True),
                segment_min_len=reply_raw.get("segment_min_len", 12),
                segment_max_len=reply_raw.get("segment_max_len", 60),
                segment_delay_min=reply_raw.get("segment_delay_min", 0.2),
                segment_delay_max=reply_raw.get("segment_delay_max", 0.5),
                reply_quote=reply_raw.get("reply_quote", True),
                strip_english_fillers=bool(reply_raw.get("strip_english_fillers", True)),
            ),
            database=DatabaseConfig(
                enabled=db_raw.get("enabled", True),
                folder=db_raw.get("folder", "./data/database"),
                file=db_raw.get("file", "luotianyi.db"),
            ),
            news=NewsConfig(
                enabled=bool(news_raw.get("enabled", False)),
                db_path=str(news_raw.get("db_path", "") or ""),
                inject_enabled=bool(news_raw.get("inject_enabled", True)),
                inject_limit=int(news_raw.get("inject_limit", 3)),
                inject_window_days=int(news_raw.get("inject_window_days", 7)),
                inject_header=str(news_raw.get("inject_header", "") or ""),
                default_ttl_days=float(news_raw.get("default_ttl_days", 14.0)),
                tool_enabled=bool(news_raw.get("tool_enabled", True)),
                collector_enabled=bool(news_raw.get("collector_enabled", False)),
                collector_uids=[
                    str(u).strip()
                    for u in (news_raw.get("collector_uids") or [])
                    if str(u).strip()
                ],
                collector_interval_seconds=float(
                    news_raw.get("collector_interval_seconds", 1800) or 1800
                ),
                collector_max_items_per_uid=int(
                    news_raw.get("collector_max_items_per_uid", 5) or 5
                ),
                collector_ttl_days=float(news_raw.get("collector_ttl_days", 14) or 14),
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
                analysis_timeout_sec=int(emotion_raw.get("analysis_timeout_sec", 90)),
                keyword_threshold=int(emotion_raw.get("keyword_threshold", 3)),
                analysis_round_cooldown=int(emotion_raw.get("analysis_round_cooldown", 2)),
                min_interval_sec=int(emotion_raw.get("min_interval_sec", 180)),
                max_concurrent=max(1, int(emotion_raw.get("max_concurrent", 4) or 1)),
                queue_burst_threshold=int(emotion_raw.get("queue_burst_threshold", 5)),
                queue_burst_count=int(emotion_raw.get("queue_burst_count", 3)),
                burst_model=str(emotion_raw.get("burst_model", "DeepSeek-V4-Flash") or ""),
                significance_threshold=int(emotion_raw.get("significance_threshold", 5)),
                favour_min=int(emotion_raw.get("favour_min", -100)),
                favour_max=int(emotion_raw.get("favour_max", 100)),
                intimacy_min=int(emotion_raw.get("intimacy_min", 0)),
                intimacy_max=int(emotion_raw.get("intimacy_max", 100)),
            ),
            tts=TTSConfig(
                enabled=bool(tts_raw.get("enabled", False)),
                backend=(str(tts_raw.get("backend", "http") or "http").strip().lower()
                         if str(tts_raw.get("backend", "http") or "http").strip().lower() in ("http", "minimax")
                         else "http"),
                base_url=str(tts_raw.get("base_url", "http://127.0.0.1:9890") or "http://127.0.0.1:9890"),
                api_key=str(tts_raw.get("api_key", "") or ""),
                queue_maxsize=max(1, int(tts_raw.get("queue_maxsize", 16))),
                timeout=max(5, int(tts_raw.get("timeout", 60))),
                convert_to_mp3=bool(tts_raw.get("convert_to_mp3", True)),
                model=str(tts_raw.get("model", "speech-2.8-hd") or "speech-2.8-hd"),
                voice_id=str(tts_raw.get("voice_id", "") or ""),
                speed=float(tts_raw.get("speed", 1.0)),
                vol=float(tts_raw.get("vol", 1.0)),
                pitch=int(tts_raw.get("pitch", 0)),
                sample_rate=int(tts_raw.get("sample_rate", 32000)),
                bitrate=int(tts_raw.get("bitrate", 128000)),
                format=str(tts_raw.get("format", "mp3") or "mp3"),
                tts_prompt_template=(
                    str(tts_raw.get("tts_prompt_template", "") or "").strip()
                    or TTSConfig().tts_prompt_template
                ),
                cmd_max_chars=max(1, int(tts_raw.get("cmd_max_chars", 30))),
                cmd_cooldown=max(0, int(tts_raw.get("cmd_cooldown", 120))),
            ),
            knowledge=KnowledgeConfig(
                enabled=bool(knowledge_raw.get("enabled", False)),
                source_dir=str(knowledge_raw.get("source_dir", "./data/knowledge_db") or "./data/knowledge_db"),
                index_dir=str(knowledge_raw.get("index_dir", "./data/knowledge_index") or "./data/knowledge_index"),
                embedding_model=str(knowledge_raw.get("embedding_model", "BAAI/bge-m3") or "BAAI/bge-m3"),
                embedding_base_url=str(knowledge_raw.get("embedding_base_url", "https://api.siliconflow.cn/v1") or ""),
                embedding_api_key=str(knowledge_raw.get("embedding_api_key", "") or ""),
                rerank_model=str(knowledge_raw.get("rerank_model", "") or ""),
                rerank_base_url=str(knowledge_raw.get("rerank_base_url", "https://api.siliconflow.cn/v1") or ""),
                rerank_api_key=str(knowledge_raw.get("rerank_api_key", "") or ""),
                search_top_k=max(1, int(knowledge_raw.get("search_top_k", 20))),
                search_top_n=max(1, int(knowledge_raw.get("search_top_n", 5))),
                max_chars_per_result=max(100, int(knowledge_raw.get("max_chars_per_result", 800))),
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
            snitch_admin_group=int(raw.get("snitch_admin_group", 1070473353)),
            ignore_auto_reply=bool(raw.get("ignore_auto_reply", True)),
            history_dual_write=bool(raw.get("history_dual_write", True)),
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
                "providers": [dict(p) for p in (self.llm.providers or [])],
                "chat_provider": self.llm.chat_provider,
                "vision_provider": self.llm.vision_provider,
                "emotion_provider": self.llm.emotion_provider,
                "chat_temperature": self.llm.chat_temperature,
                "frequency_penalty": self.llm.frequency_penalty,
                "presence_penalty": self.llm.presence_penalty,
                "vision_model": self.llm.vision_model,
                "vision_base_url": self.llm.vision_base_url,
                "vision_api_key": self.llm.vision_api_key,
                "vision_prompt": self.llm.vision_prompt,
                "vision_max_tokens": self.llm.vision_max_tokens,
                "vision_temperature": self.llm.vision_temperature,
                "vision_mode": self.llm.vision_mode,
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
            "persona_ab": self.persona_ab.to_dict(),
            "reply": {
                "stream": self.reply.stream,
                "segment_reply": self.reply.segment_reply,
                "segment_min_len": self.reply.segment_min_len,
                "segment_max_len": self.reply.segment_max_len,
                "segment_delay_min": self.reply.segment_delay_min,
                "segment_delay_max": self.reply.segment_delay_max,
                "reply_quote": self.reply.reply_quote,
                "strip_english_fillers": self.reply.strip_english_fillers,
            },
            "database": {
                "enabled": self.database.enabled,
                "folder": self.database.folder,
                "file": self.database.file,
            },
            "news": {
                "enabled": self.news.enabled,
                "db_path": self.news.db_path,
                "inject_enabled": self.news.inject_enabled,
                "inject_limit": self.news.inject_limit,
                "inject_window_days": self.news.inject_window_days,
                "inject_header": self.news.inject_header,
                "default_ttl_days": self.news.default_ttl_days,
                "tool_enabled": self.news.tool_enabled,
                "collector_enabled": self.news.collector_enabled,
                "collector_uids": list(self.news.collector_uids),
                "collector_interval_seconds": self.news.collector_interval_seconds,
                "collector_max_items_per_uid": self.news.collector_max_items_per_uid,
                "collector_ttl_days": self.news.collector_ttl_days,
            },
            "anysearch": {
                "enabled": self.anysearch.enabled,
                "api_key": self.anysearch.api_key,
                "base_url": self.anysearch.base_url,
                "timeout": self.anysearch.timeout,
            },
            "admins": list(self.admins),
            "usage_excluded_models": list(self.usage_excluded_models),
            "llm_excluded_groups": list(self.llm_excluded_groups),
            "external_service_groups": list(self.external_service_groups),
            "ban": {
                "enabled": self.ban.enabled,
            },
            "emotion": {
                "enabled": self.emotion.enabled,
                "smart_update": self.emotion.smart_update,
                "force_update_interval": self.emotion.force_update_interval,
                "analysis_timeout_sec": self.emotion.analysis_timeout_sec,
                "keyword_threshold": self.emotion.keyword_threshold,
                "analysis_round_cooldown": self.emotion.analysis_round_cooldown,
                "min_interval_sec": self.emotion.min_interval_sec,
                "max_concurrent": self.emotion.max_concurrent,
                "queue_burst_threshold": self.emotion.queue_burst_threshold,
                "queue_burst_count": self.emotion.queue_burst_count,
                "burst_model": self.emotion.burst_model,
                "significance_threshold": self.emotion.significance_threshold,
                "favour_min": self.emotion.favour_min,
                "favour_max": self.emotion.favour_max,
                "intimacy_min": self.emotion.intimacy_min,
                "intimacy_max": self.emotion.intimacy_max,
            },
            "tts": {
                "enabled": self.tts.enabled,
                "backend": self.tts.backend,
                "base_url": self.tts.base_url,
                "api_key": self.tts.api_key,
                "queue_maxsize": self.tts.queue_maxsize,
                "timeout": self.tts.timeout,
                "convert_to_mp3": self.tts.convert_to_mp3,
                "model": self.tts.model,
                "voice_id": self.tts.voice_id,
                "speed": self.tts.speed,
                "vol": self.tts.vol,
                "pitch": self.tts.pitch,
                "sample_rate": self.tts.sample_rate,
                "bitrate": self.tts.bitrate,
                "format": self.tts.format,
                "tts_prompt_template": self.tts.tts_prompt_template,
                "cmd_max_chars": self.tts.cmd_max_chars,
                "cmd_cooldown": self.tts.cmd_cooldown,
            },
            "knowledge": {
                "enabled": self.knowledge.enabled,
                "source_dir": self.knowledge.source_dir,
                "index_dir": self.knowledge.index_dir,
                "embedding_model": self.knowledge.embedding_model,
                "embedding_base_url": self.knowledge.embedding_base_url,
                "embedding_api_key": self.knowledge.embedding_api_key,
                "rerank_model": self.knowledge.rerank_model,
                "rerank_base_url": self.knowledge.rerank_base_url,
                "rerank_api_key": self.knowledge.rerank_api_key,
                "search_top_k": self.knowledge.search_top_k,
                "search_top_n": self.knowledge.search_top_n,
                "max_chars_per_result": self.knowledge.max_chars_per_result,
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
            "snitch_admin_group": self.snitch_admin_group,
            "ignore_auto_reply": self.ignore_auto_reply,
            "history_dual_write": self.history_dual_write,
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
                "providers": [dict(p) for p in (self.llm.providers or [])],
                "chat_provider": self.llm.chat_provider,
                "vision_provider": self.llm.vision_provider,
                "emotion_provider": self.llm.emotion_provider,
                "chat_temperature": self.llm.chat_temperature,
                "frequency_penalty": self.llm.frequency_penalty,
                "presence_penalty": self.llm.presence_penalty,
                "vision_model": self.llm.vision_model,
                "vision_base_url": self.llm.vision_base_url,
                "vision_api_key": self.llm.vision_api_key,
                "vision_prompt": self.llm.vision_prompt,
                "vision_max_tokens": self.llm.vision_max_tokens,
                "vision_temperature": self.llm.vision_temperature,
                "vision_mode": self.llm.vision_mode,
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
            "persona_ab": self.persona_ab.to_dict(),
            "reply": {
                "stream": self.reply.stream,
                "segment_reply": self.reply.segment_reply,
                "segment_min_len": self.reply.segment_min_len,
                "segment_max_len": self.reply.segment_max_len,
                "segment_delay_min": self.reply.segment_delay_min,
                "segment_delay_max": self.reply.segment_delay_max,
                "reply_quote": self.reply.reply_quote,
                "strip_english_fillers": self.reply.strip_english_fillers,
            },
            "database": {
                "enabled": self.database.enabled,
                "folder": self.database.folder,
                "file": self.database.file,
            },
            "news": {
                "enabled": self.news.enabled,
                "db_path": self.news.db_path,
                "inject_enabled": self.news.inject_enabled,
                "inject_limit": self.news.inject_limit,
                "inject_window_days": self.news.inject_window_days,
                "inject_header": self.news.inject_header,
                "default_ttl_days": self.news.default_ttl_days,
                "tool_enabled": self.news.tool_enabled,
                "collector_enabled": self.news.collector_enabled,
                "collector_uids": list(self.news.collector_uids),
                "collector_interval_seconds": self.news.collector_interval_seconds,
                "collector_max_items_per_uid": self.news.collector_max_items_per_uid,
                "collector_ttl_days": self.news.collector_ttl_days,
            },
            "anysearch": {
                "enabled": self.anysearch.enabled,
                "api_key": self.anysearch.api_key,
                "base_url": self.anysearch.base_url,
                "timeout": self.anysearch.timeout,
            },
            "admins": list(self.admins),
            "usage_excluded_models": list(self.usage_excluded_models),
            "llm_excluded_groups": list(self.llm_excluded_groups),
            "external_service_groups": list(self.external_service_groups),
            "ban": {
                "enabled": self.ban.enabled,
            },
            "emotion": {
                "enabled": self.emotion.enabled,
                "smart_update": self.emotion.smart_update,
                "force_update_interval": self.emotion.force_update_interval,
                "analysis_timeout_sec": self.emotion.analysis_timeout_sec,
                "keyword_threshold": self.emotion.keyword_threshold,
                "analysis_round_cooldown": self.emotion.analysis_round_cooldown,
                "min_interval_sec": self.emotion.min_interval_sec,
                "max_concurrent": self.emotion.max_concurrent,
                "queue_burst_threshold": self.emotion.queue_burst_threshold,
                "queue_burst_count": self.emotion.queue_burst_count,
                "burst_model": self.emotion.burst_model,
                "significance_threshold": self.emotion.significance_threshold,
                "favour_min": self.emotion.favour_min,
                "favour_max": self.emotion.favour_max,
                "intimacy_min": self.emotion.intimacy_min,
                "intimacy_max": self.emotion.intimacy_max,
            },
            "tts": {
                "enabled": self.tts.enabled,
                "backend": self.tts.backend,
                "base_url": self.tts.base_url,
                "api_key": self.tts.api_key,
                "queue_maxsize": self.tts.queue_maxsize,
                "timeout": self.tts.timeout,
                "convert_to_mp3": self.tts.convert_to_mp3,
                "model": self.tts.model,
                "voice_id": self.tts.voice_id,
                "speed": self.tts.speed,
                "vol": self.tts.vol,
                "pitch": self.tts.pitch,
                "sample_rate": self.tts.sample_rate,
                "bitrate": self.tts.bitrate,
                "format": self.tts.format,
                "tts_prompt_template": self.tts.tts_prompt_template,
                "cmd_max_chars": self.tts.cmd_max_chars,
                "cmd_cooldown": self.tts.cmd_cooldown,
            },
            "knowledge": {
                "enabled": self.knowledge.enabled,
                "source_dir": self.knowledge.source_dir,
                "index_dir": self.knowledge.index_dir,
                "embedding_model": self.knowledge.embedding_model,
                "embedding_base_url": self.knowledge.embedding_base_url,
                "embedding_api_key": self.knowledge.embedding_api_key,
                "rerank_model": self.knowledge.rerank_model,
                "rerank_base_url": self.knowledge.rerank_base_url,
                "rerank_api_key": self.knowledge.rerank_api_key,
                "search_top_k": self.knowledge.search_top_k,
                "search_top_n": self.knowledge.search_top_n,
                "max_chars_per_result": self.knowledge.max_chars_per_result,
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
            "snitch_admin_group": self.snitch_admin_group,
            "ignore_auto_reply": self.ignore_auto_reply,
            "history_dual_write": self.history_dual_write,
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
    persona: str = ""  # Legacy text, read only for preset migration
    persona_id: str = "persona_001"  # Stable shared preset reference
    enabled: bool = True
    touch_replies: list[str] = field(default_factory=list)  # 戳一戳固定回复(空=用全局/默认)

    # Per-bot LLM overrides (optional)
    chat_model_override: str = ""
    vision_model_override: str = ""
    # 每 bot 指定用哪个提供商(留空 = 用全局 llm.chat_provider / vision_provider)。
    # 多套 key(如 snowluma + deepseek)时可让不同 bot 走不同提供商。
    chat_provider_override: str = ""
    vision_provider_override: str = ""

    # TTS 语音: 是否开启该 bot 的 LLM 自动朗读与 /tts 指令(MiniMax 全局配置共用)
    tts_enabled: bool = False
    # MiniMax 克隆音色 ID(留空用全局 TTSConfig.voice_id)
    tts_voice_id: str = ""

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
        if not isinstance(raw, dict):
            raise ValueError("bot配置必须是JSON对象")
        if not isinstance(raw.get("persona", ""), str) or not isinstance(raw.get("persona_id", ""), str):
            raise ValueError("bot persona/persona_id必须是字符串")

        return cls(
            bot_id=raw.get("bot_id", ""),
            qq=raw.get("qq", 0),
            nickname=raw.get("nickname", ""),
            persona=raw.get("persona", ""),
            persona_id=raw.get("persona_id", ""),
            enabled=raw.get("enabled", True),
            touch_replies=list(raw.get("touch_replies", []) or []),
            chat_model_override=raw.get("chat_model_override", ""),
            vision_model_override=raw.get("vision_model_override", ""),
            chat_provider_override=str(raw.get("chat_provider_override", "") or ""),
            vision_provider_override=str(raw.get("vision_provider_override", "") or ""),
            tts_enabled=bool(raw.get("tts_enabled", False)),
            tts_voice_id=str(raw.get("tts_voice_id", "") or ""),
            command_prefix=raw.get("command_prefix", "/"),
            keyword_replies=raw.get("keyword_replies", {}),
        )

    def save(self, path: str | Path) -> None:
        """Save to JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        import json
        import os
        import tempfile
        # Synchronous callers cannot await file_store locks; replace atomically.
        fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict (for web panel editing)."""
        return {
            "bot_id": self.bot_id,
            "qq": self.qq,
            "nickname": self.nickname,
            "persona": self.persona,
            "persona_id": self.persona_id,
            "enabled": self.enabled,
            "touch_replies": list(self.touch_replies),
            "chat_model_override": self.chat_model_override,
            "vision_model_override": self.vision_model_override,
            "chat_provider_override": self.chat_provider_override,
            "vision_provider_override": self.vision_provider_override,
            "tts_enabled": self.tts_enabled,
            "tts_voice_id": self.tts_voice_id,
            "command_prefix": self.command_prefix,
            "keyword_replies": self.keyword_replies,
        }