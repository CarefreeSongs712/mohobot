import json
import hashlib
import re
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from ...host import ConfigStore, StealerEvent, logger

from ..util.normalization import (
    normalize_category_key,
    normalize_character_key,
    normalize_label_list,
)


class PluginConfig(BaseModel):
    # === 基础功能 ===
    steal_meme: bool = False
    steal_mode: str = "probability"  # "probability" 或 "cooldown"
    steal_chance: float = 0.3  # 概率模式下的偷图概率
    auto_send_meme: bool = True
    meme_chance: float = 0.2
    send_meme_as_qq_sticker: bool = True
    send_meme_as_gif: bool = False
    meme_send_char_delay: float = 0.3
    # mohobot 在整条回复发送完毕后才派发排队的表情，这里只是额外停顿
    meme_send_delay: float = 1.0
    meme_send_delay_random: bool = False
    meme_send_delay_max: float = 8.0
    auto_meme_cancel_on_new_message: bool = True
    # 会话模型选图：通过门控的轮次在用户消息末尾注入提示（不写入历史，保护前缀缓存）
    meme_candidate_count: int = 5  # search_meme 每次返回的候选数 n
    meme_hint_prompt: str = ""  # 注入提示词；留空使用内置模板
    # search_meme 的描述是否先经小模型改写成入库描述的风格再检索
    meme_query_rewrite: bool = False
    meme_query_rewrite_model: str = ""  # 留空用 mohobot 全局 llm.chat_model
    meme_query_rewrite_base_url: str = ""
    meme_query_rewrite_api_key: str = ""
    meme_query_rewrite_prompt: str = ""

    # === 群聊过滤 ===
    steal_target_whitelist: list[str] = []
    steal_target_blacklist: list[str] = []
    send_target_whitelist: list[str] = []
    send_target_blacklist: list[str] = []
    steal_target_filter_mode: str = "whitelist_first"
    send_target_filter_mode: str = "whitelist_first"

    # === 模型配置（OpenAI 兼容接口；地址留空时回退 mohobot 全局 llm 配置）===
    vision_model: str = ""  # 留空用 mohobot 全局 llm.vision_model
    vision_base_url: str = ""
    vision_api_key: str = ""

    # === 内部常量/高级配置 ===
    # 通用库软上限：超过后偷图采纳率按 (上限/当前数量)^k 衰减，并启动冗余淘汰
    max_reg_num: int = 100
    soft_limit_exponent: float = 2.0  # 衰减指数 k
    soft_limit_min_chance: float = 0.02  # 采纳率下限，保证永远不会变回硬限制
    # 冗余淘汰：n 维向量空间中半径 r0 内的邻居数 >= K 且使用率不高于中位数
    eviction_radius: float = 0.0  # r0；0 表示自动取全库最近邻距离的中位数
    eviction_min_neighbors: int = 3  # K
    eviction_grace_days: int = 7  # 入库未满该天数的表情不参与淘汰
    storage_cleanup_strategy: str = "balanced"
    image_processing_cooldown: int = 30
    smart_meme_selection: bool = True  # 智能表情包选择

    # === VLM 标注上下文 ===
    # 标注时附带的聊天记录条数，仅用于帮助 VLM 理解图片含义，不写入描述
    vlm_context_sender_messages: int = 5
    vlm_context_group_messages: int = 15

    # === 待审核池 / 嵌入检索 ===
    steal_pool_capacity: int = 200  # 待审核池软上限，超过后同样衰减采纳率
    enable_embedding_search: bool = True  # 语义检索与冗余淘汰依赖向量；不可用时降级 BM25
    embedding_model: str = ""  # mohobot 没有全局嵌入模型，必须在这里指定
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_dimensions: int = 0  # 0 表示使用模型默认维度

    # === 外部表情包源（v3） ===
    external_sources_enabled: bool = True
    external_source_allow_http: bool = False
    external_source_default_review: bool = False
    external_source_max_items: int = 2000
    external_source_max_image_bytes: int = 32 * 1024 * 1024
    external_source_max_archive_bytes: int = 1024 * 1024 * 1024
    external_source_max_uncompressed_bytes: int = 4 * 1024 * 1024 * 1024
    external_source_max_pixels: int = 40_000_000

    # === 智能选择：文字距离融合权重（预设见 _conf_schema.json _smart_section）===
    sim_weight_preset: str = "balanced"  # balanced / keyword / semantic / strict
    sim_weight_ngram: float = 0.28  # 兼容旧配置保留，不再单独暴露
    sim_weight_cosine: float = 0.25
    sim_weight_substring: float = 0.12
    sim_weight_char: float = 0.08
    sim_weight_edit: float = 0.27
    sim_negation_penalty: float = 0.25

    # 文字距离融合权重预设（键名对齐 configure_similarity）
    SIM_WEIGHT_PRESETS: ClassVar[dict[str, dict[str, float]]] = {
        "balanced": {
            "ngram": 0.28,
            "cosine": 0.25,
            "substring": 0.12,
            "char": 0.08,
            "edit": 0.27,
            "negation": 0.25,
        },
        "keyword": {
            "ngram": 0.15,
            "cosine": 0.10,
            "substring": 0.35,
            "char": 0.15,
            "edit": 0.25,
            "negation": 0.20,
        },
        "semantic": {
            "ngram": 0.35,
            "cosine": 0.35,
            "substring": 0.05,
            "char": 0.05,
            "edit": 0.20,
            "negation": 0.30,
        },
        "strict": {
            "ngram": 0.20,
            "cosine": 0.10,
            "substring": 0.20,
            "char": 0.20,
            "edit": 0.30,
            "negation": 0.35,
        },
    }

    # === 自定义提示词（VLM 审核 + 标注） ===
    custom_meme_classification_prompt: str = ""

    # === 内化常量（不再暴露给用户） ===
    DO_REPLACE: ClassVar[bool] = True  # 达到上限始终替换旧表情
    ENABLE_RAW_CLEANUP: ClassVar[bool] = True  # raw 始终自动清理
    RAW_CLEANUP_INTERVAL: ClassVar[int] = 30  # 清理周期(分钟)，固定
    ENABLE_CAPACITY_CONTROL: ClassVar[bool] = True  # 始终启用容量控制
    CAPACITY_CONTROL_INTERVAL: ClassVar[int] = 60  # 容量检查周期(分钟)，固定
    RAW_RETENTION_MINUTES: ClassVar[int] = 60  # 原始图片保留时间(分钟)，固定

    # === 分类信息 ===
    categories: list[str] = []
    category_info: dict[str, dict[str, str]] = {}
    characters: list[str] = []
    character_info: dict[str, dict[str, str]] = {}

    # === 待审核池 ===
    # 自动偷取时是否进入待审核池等待人工通过。
    # False：VLM 审核通过即入库（默认）。
    # True：进入 pending，需在 WebUI 审核区通过后才入库。
    audit_required: bool = False

    # === WebUI（独立端口）===
    webui_enabled: bool = False
    webui_host: str = "127.0.0.1"
    webui_port: int = 9092
    webui_password: str = ""
    # WebUI 默认主题：auto/dark/light/minecraft/fallout。页面内切换后写入 KV，优先于该项。
    webui_theme: str = "auto"

    # === 内部状态 (不作为 Pydantic 字段) ===
    # 使用 PrivateAttr 或在 __init__ 中设置且不包含在 __annotations__ 中
    # 但 Pydantic v1/v2 处理方式不同。这里使用 __private_attributes__ 机制或直接忽略

    # 忽略额外字段（Pydantic v2 model_config）
    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    # === 常量 ===
    # 使用 ClassVar 标注，避免被 Pydantic 识别为字段
    # VLM 不再做情绪分类；新图统一进入该目录，分类只用于人工归档与浏览。
    DEFAULT_CATEGORY: ClassVar[str] = "uncategorized"
    DEFAULT_CATEGORY_NAME: ClassVar[str] = "未分类"

    DEFAULT_CATEGORIES: ClassVar[list[str]] = [
        "happy",
        "sad",
        "angry",
        "shy",
        "surprised",
        "troll",
        "cry",
        "confused",
        "embarrassed",
        "love",
        "disgust",
        "fear",
        "excitement",
        "tired",
        "sigh",
        "thank",
        "dumb",
    ]

    DEFAULT_CATEGORY_INFO: ClassVar[dict[str, dict[str, str]]] = {
        "happy": {"name": "开心", "desc": "快乐、愉悦、满足、好心情"},
        "sad": {"name": "难过", "desc": "悲伤、沮丧、失落、emo"},
        "angry": {"name": "生气", "desc": "愤怒、恼火、不满、暴躁"},
        "shy": {"name": "害羞", "desc": "羞涩、不好意思、腼腆"},
        "surprised": {"name": "惊讶", "desc": "意外、震惊、惊奇、啊？"},
        "troll": {"name": "整活", "desc": "调皮、搞怪、发癫、抽象"},
        "cry": {"name": "哭哭", "desc": "哭泣、流泪、委屈、破防"},
        "confused": {"name": "困惑", "desc": "迷茫、不解、疑惑、问号脸"},
        "embarrassed": {"name": "尴尬", "desc": "社死、窘迫、为难、脚趾抠地"},
        "love": {"name": "喜欢", "desc": "喜爱、爱慕、宠溺、心动"},
        "disgust": {"name": "嫌弃", "desc": "厌恶、反感、讨厌、yue"},
        "fear": {"name": "害怕", "desc": "恐惧、担心、紧张、怂"},
        "excitement": {"name": "兴奋", "desc": "激动、亢奋、嗨、上头"},
        "tired": {"name": "困倦", "desc": "疲惫、困、无力、想躺"},
        "sigh": {"name": "无奈", "desc": "叹气、摆烂、算了、心累"},
        "thank": {"name": "感谢", "desc": "道谢、感恩、收到、爱了"},
        "dumb": {"name": "无语", "desc": "呆住、傻眼、离谱、沉默"},
    }

    DEFAULT_CATEGORY_ALIASES: ClassVar[dict[str, str]] = {
        "开心": "happy",
        "高兴": "happy",
        "快乐": "happy",
        "哈哈": "happy",
        "笑": "happy",
        "难过": "sad",
        "伤心": "sad",
        "emo": "sad",
        "沮丧": "sad",
        "失落": "sad",
        "生气": "angry",
        "愤怒": "angry",
        "恼火": "angry",
        "暴躁": "angry",
        "害羞": "shy",
        "不好意思": "shy",
        "腼腆": "shy",
        "惊讶": "surprised",
        "震惊": "surprised",
        "意外": "surprised",
        "搞怪": "troll",
        "整活": "troll",
        "发癫": "troll",
        "抽象": "troll",
        "哭": "cry",
        "大哭": "cry",
        "哭哭": "cry",
        "委屈": "cry",
        "破防": "cry",
        "困惑": "confused",
        "疑惑": "confused",
        "迷茫": "confused",
        "问号": "confused",
        "尴尬": "embarrassed",
        "社死": "embarrassed",
        "为难": "embarrassed",
        "喜欢": "love",
        "喜爱": "love",
        "爱": "love",
        "心动": "love",
        "嫌弃": "disgust",
        "厌恶": "disgust",
        "反感": "disgust",
        "yue": "disgust",
        "害怕": "fear",
        "恐惧": "fear",
        "紧张": "fear",
        "怂": "fear",
        "兴奋": "excitement",
        "激动": "excitement",
        "嗨": "excitement",
        "上头": "excitement",
        "疲惫": "tired",
        "困": "tired",
        "困倦": "tired",
        "想睡": "tired",
        "无奈": "sigh",
        "叹气": "sigh",
        "摆烂": "sigh",
        "算了": "sigh",
        "感谢": "thank",
        "谢谢": "thank",
        "多谢": "thank",
        "感恩": "thank",
        "无语": "dumb",
        "傻眼": "dumb",
        "离谱": "dumb",
        "沉默": "dumb",
    }

    def __init__(self, config: ConfigStore | dict | None, data_dir: Path | str):
        # 1. 初始化 Pydantic 模型
        # config 是插件配置存档（ConfigStore，dict 子类）或 None
        initial_data = dict(config) if config else {}
        super().__init__(**initial_data)
        self._drop_legacy_default_prompt()

        # 2. 保存配置存档引用以便回写
        # 使用 object.__setattr__ 绕过 Pydantic 的 setattr 检查
        object.__setattr__(self, "_data", config)

        # 3. 初始化路径和目录
        data_dir = Path(data_dir).resolve()
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "categories_path", data_dir / "categories.json")
        object.__setattr__(self, "raw_dir", data_dir / "raw")
        object.__setattr__(self, "categories_dir", data_dir / "categories")
        object.__setattr__(self, "cache_dir", data_dir / "cache")
        object.__setattr__(self, "pending_dir", data_dir / "pending")
        object.__setattr__(self, "category_info_path", data_dir / "category_info.json")
        object.__setattr__(self, "characters_path", data_dir / "characters.json")
        object.__setattr__(self, "character_info_path", data_dir / "character_info.json")

        # 确保目录存在
        self.ensure_base_dirs()

        self._load_category_state()
        self._migrate_category_config()
        self._refresh_target_policy_cache()

    # 旧版 _conf_schema.json 把整段情绪分类提示词作为默认值写进了用户配置，
    # 这些原样保存的默认值不算用户自定义，否则会盖掉新的审核+标注提示词。
    LEGACY_DEFAULT_PROMPT_HASHES: ClassVar[frozenset[str]] = frozenset(
        {
            "5226741ed4541357c5f644df62597c861f7aed7ea543ea013d57fdc0baad70bf",
            "7c1da5f6f69203ac74ba798b12301d097e0b685a2c9a9d619e22b02a2c46d899",
            "4ef8a9087c85229377091e5c6273af2da032a8c38fd696515f8fec3390d5427d",
            "490e2fdfb52110920cf82daad3b2542a09049803b7459cbd3d8775b0bc13ee26",
        }
    )

    def _drop_legacy_default_prompt(self) -> None:
        prompt = str(self.custom_meme_classification_prompt or "").strip()
        if not prompt:
            return
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if digest in self.LEGACY_DEFAULT_PROMPT_HASHES:
            BaseModel.__setattr__(self, "custom_meme_classification_prompt", "")
            logger.info("[Config] 检测到旧版默认分类提示词，改用新的审核+标注提示词")
        elif "approved" not in prompt:
            logger.warning(
                "[Config] 自定义 VLM 提示词没有审核字段 approved，图片将不经内容审核直接标注"
            )

    def _read_json_file(self, path: Path):
        try:
            if not path.exists():
                return None
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"[Config] JSON 解析失败 {path}: {e}")
            return None
        except Exception as e:
            logger.debug(f"[Config] 读取文件失败 {path}: {e}")
            return None

    def _write_json_file(self, path: Path, data: Any) -> bool:
        """写入 JSON 文件。

        Args:
            path: 文件路径
            data: 要写入的数据

        Returns:
            bool: 是否写入成功
        """
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except PermissionError as e:
            logger.error(f"[Config] 权限不足，无法写入文件 {path}: {e}")
            return False
        except OSError as e:
            logger.error(f"[Config] 写入文件失败 {path}: {e}")
            return False
        except Exception as e:
            logger.error(f"[Config] 写入 JSON 文件时发生未知错误 {path}: {e}")
            return False

    def _load_category_state(self) -> None:
        stored_categories = self._read_json_file(self.categories_path)
        stored_info = self._read_json_file(self.category_info_path)

        config_categories = None
        config_info = None
        if isinstance(self._data, dict):
            if "categories" in self._data:
                config_categories = self._data.get("categories")
            if "category_info" in self._data:
                config_info = self._data.get("category_info")

        categories = (
            stored_categories
            if isinstance(stored_categories, list) and stored_categories
            else config_categories
            if isinstance(config_categories, list) and config_categories
            else list(self.DEFAULT_CATEGORIES)
        )
        info = (
            stored_info
            if isinstance(stored_info, dict)
            else config_info
            if isinstance(config_info, dict)
            else {}
        )

        categories, merged_info, migrations = self._normalize_legacy_category_state(
            categories, info
        )
        if self.DEFAULT_CATEGORY not in categories:
            categories.insert(0, self.DEFAULT_CATEGORY)
        merged_info.setdefault(
            self.DEFAULT_CATEGORY,
            {"name": self.DEFAULT_CATEGORY_NAME, "desc": "自动收录、尚未人工归档的表情包"},
        )
        object.__setattr__(self, "_legacy_category_key_map", migrations)

        # 使用 BaseModel.__setattr__ 绕过自定义 __setattr__ 中的写文件逻辑，
        # 避免初始化期间重复写文件（最后统一写一次即可）
        BaseModel.__setattr__(self, "categories", list(categories))
        BaseModel.__setattr__(self, "category_info", merged_info)
        self.save_categories()
        self.save_category_info()
        self._load_character_state()

    def _normalize_legacy_category_state(
        self,
        categories: list[Any],
        info: dict[str, Any],
    ) -> tuple[list[str], dict[str, dict[str, str]], dict[str, str]]:
        """将旧版中文/不安全分类 key 收敛为安全 key，保留中文显示信息。

        3.1.0 开始分类 key 用作目录名并执行便携路径校验。旧版本允许中文
        key，直接在启动时校验会阻止整个插件加载，因此这里先生成稳定迁移名。
        已知情绪名称优先复用现有英文 key；无法推断的自定义 key 使用内容哈希，
        避免引入拼音依赖或产生不稳定的 transliteration。
        """
        raw_keys: list[str] = []
        for value in [*(categories or []), *((info or {}).keys())]:
            key = str(value or "").strip()
            if key and key not in raw_keys:
                raw_keys.append(key)

        used: set[str] = set()
        migrations: dict[str, str] = {}

        def choose_key(raw_key: str) -> str:
            lowered = raw_key.lower()
            try:
                safe = normalize_category_key(lowered)
                if safe not in used:
                    used.add(safe)
                return safe
            except ValueError:
                pass

            raw_info = info.get(raw_key, {}) if isinstance(info, dict) else {}
            candidates = [raw_key]
            if isinstance(raw_info, dict):
                candidates.extend(
                    [str(raw_info.get("name") or ""), str(raw_info.get("desc") or "")]
                )
            for candidate in candidates:
                alias = self.DEFAULT_CATEGORY_ALIASES.get(candidate.strip())
                if alias:
                    try:
                        alias = normalize_category_key(alias)
                    except ValueError:
                        alias = ""
                    if alias:
                        used.add(alias)
                        return alias

            ascii_hint = re.sub(r"[^a-z0-9_-]+", "_", lowered).strip("_-")
            if ascii_hint and re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", ascii_hint):
                candidate = ascii_hint
            else:
                digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:12]
                candidate = f"legacy_{digest}"
            suffix = 2
            base = candidate[:48]
            candidate = base
            while candidate in used:
                tail = f"_{suffix}"
                candidate = f"{base[:48 - len(tail)]}{tail}"
                suffix += 1
            used.add(candidate)
            return candidate

        for raw_key in raw_keys:
            safe_key = choose_key(raw_key)
            if raw_key != safe_key:
                migrations[raw_key] = safe_key

        normalized_categories: list[str] = []
        for raw_key in categories or []:
            safe_key = migrations.get(str(raw_key), str(raw_key).strip().lower())
            if safe_key and safe_key not in normalized_categories:
                normalized_categories.append(safe_key)

        merged_info: dict[str, dict[str, str]] = {
            key: dict(value) for key, value in self.DEFAULT_CATEGORY_INFO.items()
        }
        for raw_key, raw_value in (info or {}).items():
            raw_key = str(raw_key)
            safe_key = migrations.get(raw_key, raw_key.strip().lower())
            existing = dict(merged_info.get(safe_key, {}))
            if isinstance(raw_value, dict):
                display_name = str(raw_value.get("name") or "").strip()
                description = str(raw_value.get("desc") or "").strip()
                if display_name:
                    existing["name"] = display_name
                elif raw_key != safe_key and "name" not in existing:
                    existing["name"] = raw_key
                if description:
                    existing["desc"] = description
            elif raw_key != safe_key and "name" not in existing:
                existing["name"] = raw_key
            merged_info[safe_key] = existing

        if migrations:
            logger.warning(
                "检测到旧版不安全分类 key，已自动迁移并保留中文显示名: "
                + ", ".join(f"{old}->{new}" for old, new in migrations.items())
            )
        return normalized_categories, merged_info, migrations

    def get_legacy_category_key_map(self) -> dict[str, str]:
        """返回本次启动发现的旧分类 key 映射。"""
        return dict(getattr(self, "_legacy_category_key_map", {}) or {})

    def get_categories(self) -> list[str]:
        """返回当前分类列表；为空时回退到 DEFAULT_CATEGORIES。

        各 service 读取分类时统一走这个方法，避免在多处重复
        `self.categories or DEFAULT_CATEGORIES` 模板。
        """
        cats = list(self.categories or [])
        if not cats:
            cats = list(self.DEFAULT_CATEGORIES)
        return cats

    def get_vlm_categories(self) -> list[str]:
        """给 VLM 的分类列表，不含 other。"""
        return [key for key in self.get_categories() if key != "other"]

    def closest_category(self, raw: str) -> str:
        """把分类名收到最接近的已有分类；无法识别时归入默认的未分类目录。"""
        known = self.get_vlm_categories()
        raw_l = str(raw or "").strip().lower()
        if not raw_l:
            return self.DEFAULT_CATEGORY
        strict = self.normalize_category_strict(raw_l)
        if strict and strict != "other" and strict in known:
            return strict
        info_map = self.category_info or self.DEFAULT_CATEGORY_INFO
        for key in known:
            info = info_map.get(key) or {}
            name = str(info.get("name") or "").strip().lower()
            desc = str(info.get("desc") or "").strip().lower()
            if name and (raw_l == name or raw_l in name or name in raw_l):
                return key
            if raw_l and raw_l in desc:
                return key
        return self.DEFAULT_CATEGORY

    def _migrate_category_config(self) -> None:
        if not isinstance(self._data, dict):
            return
        removed = False
        if "categories" in self._data:
            del self._data["categories"]
            removed = True
        if "category_info" in self._data:
            del self._data["category_info"]
            removed = True
        if removed and hasattr(self._data, "save_config"):
            self._data.save_config()

    def __setattr__(self, key: str, value: Any):
        super().__setattr__(key, value)

        if key in self._TARGET_POLICY_CONFIG_KEYS:
            self._refresh_target_policy_cache()

        if key in ("categories", "category_info"):
            if key == "categories":
                self.save_categories()
            else:
                self.save_category_info()
        if key in ("characters", "character_info"):
            if key == "characters":
                self.save_characters()
            else:
                self.save_character_info()

    def update_config(self, updates: dict) -> bool:
        """批量更新配置项。

        Args:
            updates: 配置更新字典

        Returns:
            bool: 是否更新成功
        """
        try:
            fields = getattr(type(self), "model_fields", None)
            if fields is None:
                fields = getattr(type(self), "__fields__", {})
            valid_updates: dict[str, Any] = {}
            for key, value in updates.items():
                if fields is not None and key not in fields:
                    logger.debug(f"[Config] 忽略已移除的配置键: {key}")
                    continue
                valid_updates[key] = value
                setattr(self, key, value)

            # 回写到配置存档
            if hasattr(self, "_data") and self._data is not None:
                if hasattr(self._data, "save_config"):
                    self._data.save_config(valid_updates)
                elif isinstance(self._data, dict):
                    self._data.update(valid_updates)
            return True
        except Exception as e:
            logger.error(f"更新配置失败: {e}")
            return False

    _TARGET_POLICY_CONFIG_KEYS: ClassVar[set[str]] = {
        "send_target_whitelist",
        "send_target_blacklist",
        "send_target_filter_mode",
        "steal_target_whitelist",
        "steal_target_blacklist",
        "steal_target_filter_mode",
    }

    def _normalize_target_collection(self, values: list[str] | None) -> frozenset[str]:
        normalized: set[str] = set()
        for value in values or []:
            target = self.normalize_target_entry(value)
            if target:
                normalized.add(target)
        return frozenset(normalized)

    def _refresh_target_policy_cache(self) -> None:
        object.__setattr__(
            self,
            "_target_policy_cache",
            {
                "send": {
                    "whitelist": self._normalize_target_collection(self.send_target_whitelist),
                    "blacklist": self._normalize_target_collection(self.send_target_blacklist),
                    "mode": self._normalize_filter_mode(self.send_target_filter_mode),
                },
                "steal": {
                    "whitelist": self._normalize_target_collection(self.steal_target_whitelist),
                    "blacklist": self._normalize_target_collection(self.steal_target_blacklist),
                    "mode": self._normalize_filter_mode(self.steal_target_filter_mode),
                },
            },
        )

    def save_categories(self) -> None:
        self._write_json_file(self.categories_path, self.categories)

    def save_category_info(self) -> None:
        self._write_json_file(self.category_info_path, self.category_info)

    def _load_character_state(self) -> None:
        stored_characters = self._read_json_file(self.characters_path)
        stored_info = self._read_json_file(self.character_info_path)
        characters = (
            list(stored_characters)
            if isinstance(stored_characters, list) and stored_characters
            else []
        )
        info = stored_info if isinstance(stored_info, dict) else {}
        BaseModel.__setattr__(
            self,
            "characters",
            normalize_label_list(characters, allow_duplicates=True),
        )
        BaseModel.__setattr__(self, "character_info", dict(info))
        self.save_characters()
        self.save_character_info()

    def save_characters(self) -> None:
        self._write_json_file(self.characters_path, self.characters)

    def save_character_info(self) -> None:
        self._write_json_file(self.character_info_path, self.character_info)

    @staticmethod
    def normalize_character_key(value: str) -> str:
        return normalize_character_key(value)

    def get_characters(self) -> list[str]:
        return [key for key in (self.characters or []) if key]

    def get_character_info_list(self) -> list[dict[str, str]]:
        info_map = self.character_info or {}
        result: list[dict[str, str]] = []
        for key in self.get_characters():
            info = info_map.get(key, {}) if isinstance(info_map, dict) else {}
            result.append(
                {
                    "key": key,
                    "name": str(info.get("name", "") or key),
                    "desc": str(info.get("desc", "") or ""),
                }
            )
        return result

    def ensure_category_dir(self, category: str) -> Path:
        safe_key = normalize_category_key(category)
        category_dir = (self.categories_dir / safe_key).resolve()
        if category_dir.parent != self.categories_dir.resolve():
            raise ValueError("分类目录超出允许范围")
        category_dir.mkdir(parents=True, exist_ok=True)
        return category_dir

    def ensure_category_dirs(self, categories: list[str] | None) -> None:
        if not categories:
            return
        for category in categories:
            self.ensure_category_dir(category)

    def ensure_raw_dir(self) -> Path:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        return self.raw_dir

    def ensure_cache_dir(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir

    def ensure_pending_dir(self) -> Path:
        """确保待审核池目录存在，并返回其路径。"""
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        return self.pending_dir

    def ensure_base_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.categories_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    def normalize_category_strict(self, category: str) -> str | None:
        """严格归一化情绪分类。"""
        if not category:
            return None

        category = category.lower().strip()

        legacy_alias = (getattr(self, "_legacy_category_key_map", {}) or {}).get(category)
        if legacy_alias:
            return legacy_alias

        # 1. 直接匹配当前配置的分类列表（包括用户自定义分类）
        if category in self.categories:
            return category

        # 2. 匹配默认分类（兜底）
        if category in self.DEFAULT_CATEGORIES:
            return category

        # 3. 别名查找
        return self.DEFAULT_CATEGORY_ALIASES.get(category)

    def get_keyword_map(self) -> dict[str, str]:
        """获取关键词映射表。"""
        return self.DEFAULT_CATEGORY_ALIASES

    def get_prompts(self, default_prompts: dict[str, str] | None = None) -> dict[str, str]:
        """获取 VLM 审核+标注提示词；用户自定义非空时优先，为空回退到插件自带 prompts.json。"""
        custom_prompt = getattr(self, "custom_meme_classification_prompt", "")
        default_prompts = default_prompts or {}

        result = {"emoji_classification_prompt": ""}
        if custom_prompt and str(custom_prompt).strip():
            result["emoji_classification_prompt"] = str(custom_prompt).strip()
        elif default_prompts:
            result["emoji_classification_prompt"] = str(
                default_prompts.get("EMOJI_CLASSIFICATION_PROMPT", "") or ""
            )
        return result

    def get_category_info(self) -> list[dict[str, str]]:
        categories = self.categories or list(self.DEFAULT_CATEGORIES)
        info_map = self.category_info or {}

        result: list[dict[str, str]] = []
        for key in categories:
            info = info_map.get(key, {}) if isinstance(info_map, dict) else {}
            name = str(info.get("name", "") or key)
            desc = str(info.get("desc", "") or "")
            result.append({"key": str(key), "name": name, "desc": desc})
        return result

    def get_group_id(self, event: StealerEvent) -> str:
        """获取群号。"""
        try:
            return event.get_group_id()
        except Exception:
            return ""

    def get_user_id(self, event: StealerEvent) -> str:
        try:
            user_id = event.get_sender_id()
            if user_id:
                return str(user_id).strip()
        except Exception:
            pass

        for attr in ("sender_id", "user_id"):
            try:
                value = getattr(event, attr, None)
            except Exception:
                value = None
            if value:
                return str(value).strip()

        try:
            message_obj = getattr(event, "message_obj", None)
            sender = getattr(message_obj, "sender", None) if message_obj else None
            user_id = getattr(sender, "user_id", None) if sender is not None else None
            if user_id:
                return str(user_id).strip()
        except Exception:
            pass

        return ""

    def get_event_target(self, event: StealerEvent) -> tuple[str, str]:
        group_id = self.get_group_id(event)
        if group_id:
            return "group", str(group_id).strip()

        user_id = self.get_user_id(event)
        if user_id:
            return "user", str(user_id).strip()

        return "", ""

    def get_event_targets(self, event: StealerEvent) -> list[str]:
        targets: list[str] = []
        seen: set[str] = set()

        group_id = self.get_group_id(event)
        if group_id:
            normalized = self.normalize_target_entry(group_id, "group")
            if normalized and normalized not in seen:
                seen.add(normalized)
                targets.append(normalized)

        user_id = self.get_user_id(event)
        if user_id:
            normalized = self.normalize_target_entry(user_id, "user")
            if normalized and normalized not in seen:
                seen.add(normalized)
                targets.append(normalized)

        return targets

    @staticmethod
    def normalize_target_entry(value: object, default_scope: str = "group") -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""

        lowered = raw.lower()
        for prefix, scope in (
            ("group:", "group"),
            ("g:", "group"),
            ("群:", "group"),
            ("user:", "user"),
            ("u:", "user"),
            ("qq:", "user"),
            ("好友:", "user"),
            ("私聊:", "user"),
        ):
            if lowered.startswith(prefix):
                target_id = raw[len(prefix) :].strip()
                return f"{scope}:{target_id}" if target_id else ""

        if ":" in raw:
            scope, target_id = raw.split(":", 1)
            scope = scope.strip().lower()
            target_id = target_id.strip()
            if scope in {"group", "user"} and target_id:
                return f"{scope}:{target_id}"

        return f"{default_scope}:{raw}" if raw else ""

    def _get_action_lists(self, action: str) -> tuple[list[str], list[str]]:
        policy = self._get_action_policy(action)
        return (sorted(policy["whitelist"]), sorted(policy["blacklist"]))

    @staticmethod
    def _normalize_filter_mode(value: object) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"blacklist_first", "blacklist", "bl", "black"}:
            return "blacklist_first"
        return "whitelist_first"

    def _get_action_filter_mode(self, action: str) -> str:
        return str(self._get_action_policy(action)["mode"])

    def _get_action_policy(self, action: str) -> dict[str, object]:
        cache = getattr(self, "_target_policy_cache", None)
        if not isinstance(cache, dict):
            self._refresh_target_policy_cache()
            cache = getattr(self, "_target_policy_cache", {})
        return cache.get(str(action or "").strip().lower(), {}) or {}

    def is_action_allowed(self, action: str, event: StealerEvent) -> bool:
        targets = self.get_event_targets(event)
        if not targets:
            return True
        return self._is_normalized_targets_allowed(action, targets)

    def is_targets_allowed(self, action: str, target_entries: list[str]) -> bool:
        normalized_targets: list[str] = []
        seen: set[str] = set()
        for entry in target_entries or []:
            normalized = self.normalize_target_entry(entry)
            if normalized and normalized not in seen:
                seen.add(normalized)
                normalized_targets.append(normalized)

        if not normalized_targets:
            return True

        return self._is_normalized_targets_allowed(action, normalized_targets)

    def _is_normalized_targets_allowed(self, action: str, normalized_targets: list[str]) -> bool:
        if not normalized_targets:
            return True

        policy = self._get_action_policy(action)
        whitelist = policy.get("whitelist", frozenset())
        blacklist = policy.get("blacklist", frozenset())
        filter_mode = str(policy.get("mode", "whitelist_first"))
        whitelist_hit = any(target in whitelist for target in normalized_targets)
        blacklist_hit = any(target in blacklist for target in normalized_targets)

        if filter_mode == "blacklist_first":
            if blacklist_hit:
                return False
            if whitelist:
                return whitelist_hit
            return True

        if whitelist_hit:
            return True
        if blacklist_hit:
            return False
        if whitelist:
            return False
        return True

    def is_target_allowed(self, action: str, target_entry: str) -> bool:
        return self.is_targets_allowed(action, [target_entry])

    def is_group_allowed(self, group_id: str) -> bool:
        """检查群组是否允许。"""
        if not group_id:
            return True

        return self.is_target_allowed("send", f"group:{group_id}")
