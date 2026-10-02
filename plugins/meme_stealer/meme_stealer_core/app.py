"""表情包小偷核心：原 AstrBot 插件的 Main 去掉框架装饰器后的服务容器。

mohobot 入口（``main.py`` 的 ``Plugin``）负责把消息钩子、``/meme`` 指令和
LLM 工具调用转发到这里；本模块不依赖 mohobot。
"""

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from PIL import Image

from .host import ConfigStore, MessageEventResult, ModelGateway, StealerEvent, logger

from .core.commands.command_handler import CommandHandler
from .core.commands.image_mgmt_command import ImageManagementCommand
from .core.commands.index_rebuild_command import IndexRebuildCommand
from .core.commands.target_filter_command import TargetFilterCommand
from .core.config.config import PluginConfig
from .core.db.database_service import DatabaseService
from .core.search.meme_selector import MemeSelector
from .core.events.event_handler import EventHandler
from .core.events.meme_sender_engine import MemeSenderEngine
from .core.db.index_manager import IndexManager
from .core.processing.image_processor_service import ImageProcessorService
from .core.processing.image_render_service import ImageRenderService
from .core.maintenance.service import MaintenanceService
from .core.sources.source_service import SourceService
from .task_scheduler import TaskScheduler
from .plugin_api import PluginAPI
from .core.tools.meme_search_tool import MemeSearchToolWorkflow
from .core.tools.meme_steal_tool import MemeStealToolWorkflow


class StealerApp(MemeSearchToolWorkflow, MemeStealToolWorkflow):
    """表情包偷取与发送。

    功能：
    - 监听消息中的表情包，由视觉模型一次完成审核与语义标注后入库
    - 入库文本做嵌入向量，会话模型用 search_meme / send_meme 按语义选图
    - 提供 /meme 管理指令与独立端口的 WebUI
    """

    # 常量定义
    BACKEND_TAG = "emoji_stealer"
    SEARCH_MEME_TOOL_NAME = "search_meme"
    SEND_MEME_TOOL_NAME = "send_meme"
    STEAL_MEME_TOOL_NAME = "steal_meme"

    # 时间间隔常量（单位：秒）
    RAW_CLEANUP_INTERVAL_SECONDS = 30 * 60  # 30分钟
    CAPACITY_CONTROL_INTERVAL_SECONDS = 60 * 60  # 60分钟

    # 超时和处理常量
    IMAGE_PROCESSING_TIMEOUT_SECONDS = 120  # 图片处理超时时间（GIF动图处理需要更长时间）
    AUTO_EMOJI_COOLDOWN_SECONDS = 20  # 同一会话自动发表情的最短间隔

    # 嵌入相关配置变化时需要重新初始化向量库
    EMBEDDING_CONFIG_KEYS = frozenset(
        {
            "enable_embedding_search",
            "embedding_model",
            "embedding_base_url",
            "embedding_api_key",
            "embedding_dimensions",
        }
    )

    def __init__(
        self,
        config: ConfigStore | dict | None,
        data_dir: Path | str,
        *,
        plugin_dir: Path | str,
        llm_service_getter: Callable[[], Any] | None = None,
        tools_registered: Callable[[], bool] | None = None,
    ):
        self.plugin_dir = Path(plugin_dir)

        # 初始化插件配置
        self.plugin_config = PluginConfig(config, data_dir)
        self.models = ModelGateway(lambda: self.plugin_config, llm_service_getter)
        self._tools_registered = tools_registered

        self.base_dir: Path = self.plugin_config.data_dir
        self.raw_dir: Path = self.plugin_config.raw_dir
        self.categories_dir: Path = self.plugin_config.categories_dir
        self.cache_dir: Path = self.plugin_config.cache_dir
        self._kv_path: Path = self.base_dir / "kv_store.json"

        # 配置统一通过 self.plugin_config 读取（pydantic 模型）。

        # 初始化核心服务类
        self.db_service = DatabaseService(self.cache_dir / "emoji.db")
        self.source_service = SourceService(self)
        self.command_handler = CommandHandler(self)
        self.image_commands = ImageManagementCommand(self)
        self.index_commands = IndexRebuildCommand(self)
        self.target_commands = TargetFilterCommand(self)
        self.plugin_api = PluginAPI(self)

        self.event_handler = EventHandler(self)
        self.image_processor_service = ImageProcessorService(self)
        self.image_render_service = ImageRenderService(self)
        self.meme_selector = MemeSelector(self)
        self.task_scheduler = TaskScheduler()

        self.index_manager = IndexManager(self)
        self._emoji_sender_engine = MemeSenderEngine(self)
        self.maintenance = MaintenanceService(self)

        # 运行时属性
        self._terminated: bool = False  # 终止标志位，防止重复清理
        self._embedding_task: asyncio.Task | None = None

    def __getattr__(self, name: str):
        """将未定义的属性访问自动代理到 plugin_config。

        core/ 中部分代码直接访问 plugin_instance.steal_meme 等配置项，
        通过 __getattr__ 自动代理，无需逐个修改调用方。
        """
        if name == "plugin_config":
            raise AttributeError(f"'{type(self).__name__}' object has no attribute 'plugin_config'")
        cfg = self.__dict__.get("plugin_config")
        if cfg is not None and hasattr(cfg, name):
            return getattr(cfg, name)
        # 区分：plugin_config 未初始化 vs. 属性完全不存在
        if cfg is None:
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}' "
                f"(plugin_config 尚未初始化)"
            )
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}' "
            f"(plugin_config 中也不存在该属性)"
        )

    def meme_tools_registered(self) -> bool:
        """search_meme / send_meme 是否已注册到 mohobot 的 LLM 工具表。"""
        checker = self._tools_registered
        if checker is None:
            return True
        try:
            return bool(checker())
        except Exception:
            return False

    # ── 简单 KV（WebUI 偏好等）────────────────────────────

    def _read_kv(self) -> dict[str, Any]:
        try:
            data = json.loads(self._kv_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        data = await asyncio.to_thread(self._read_kv)
        return data.get(key, default)

    def _write_kv(self, key: str, value: Any) -> None:
        data = self._read_kv()
        data[key] = value
        self._kv_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".kv_store.", suffix=".tmp", dir=str(self._kv_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self._kv_path)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    async def put_kv_data(self, key: str, value: Any) -> None:
        await asyncio.to_thread(self._write_kv, key, value)

    def _apply_prompts(self, prompts: dict) -> None:
        """应用提示词配置。"""
        for key, value in prompts.items():
            setattr(self, key, value)
        final_prompts = self.plugin_config.get_prompts(prompts)
        self.image_processor_service.update_config(
            emoji_classification_prompt=final_prompts.get("emoji_classification_prompt"),
        )

    def _auto_merge_existing_categories(self) -> None:
        """自动合并已存在的分类目录到配置中。

        注意：基于用户当前已加载的 categories（来自 categories.json）而非
        DEFAULT_CATEGORIES 作为合并基线。这样用户主动删除的预定义类别不会
        被重新加回，仅自动发现磁盘上用户未配置的自定义类别。
        """
        current = list(getattr(self, "categories", None) or [])
        # 兼容：若 categories 尚未加载，回退到已存储配置或默认列表
        if not current:
            current = list(getattr(self.plugin_config, "categories", None) or [])
        if not current:
            current = list(getattr(self.plugin_config, "DEFAULT_CATEGORIES", []) or [])
        current_set = set(current)
        protected = set(getattr(self.plugin_config, "DEFAULT_CATEGORIES", []) or [])
        discovered: set[str] = set()
        try:
            if self.categories_dir.exists():
                for child in self.categories_dir.iterdir():
                    if not child.is_dir():
                        continue
                    key = child.name.strip()
                    if not key or key == "unknown":
                        continue
                    try:
                        if any(p.is_file() for p in child.iterdir()):
                            discovered.add(key)
                    except OSError:
                        discovered.add(key)
        except Exception as e:
            logger.warning(f"[Config] 扫描分类目录时出错: {e}")
        try:
            index = (
                self.db_service.get_index_cache_readonly()
                if self.db_service.count_total() > 0
                else {}
            )
            for meta in index.values():
                if not isinstance(meta, dict):
                    continue
                cat = str(meta.get("category", "")).strip()
                if not cat or cat == "unknown":
                    continue
                discovered.add(cat)
        except Exception as e:
            logger.warning(f"[Config] 从索引合并分类时出错: {e}")
        to_add = sorted(
            cat
            for cat in (discovered - current_set)
            # 仅自动发现「自定义」类别；用户已删除的预定义类别即使磁盘上
            # 仍有残留文件也不会被重新加回（避免重启后复活已被删除的预定义分类）。
            if cat not in protected
        )
        if not to_add:
            return
        merged_categories = current + to_add
        self.update_config({"categories": merged_categories})
        self.plugin_config.ensure_category_dirs(to_add)

    def _validate_config(self) -> bool:
        """验证配置参数的有效性。"""
        cfg = self.plugin_config
        errors = []
        fixed = []
        fixed_values = {}
        if not isinstance(cfg.max_reg_num, int) or cfg.max_reg_num <= 0:
            errors.append("最大表情数量必须大于0的整数")
            fixed.append("最大表情数量已重置为100")
            fixed_values["max_reg_num"] = 100
        if not isinstance(cfg.meme_chance, (int, float)) or not (0 <= cfg.meme_chance <= 1):
            errors.append("表情发送概率必须在0-1之间")
            fixed.append("表情发送概率已重置为0.4")
            fixed_values["meme_chance"] = 0.4
        if cfg.steal_mode not in ("probability", "cooldown"):
            errors.append(f"偷图模式 '{cfg.steal_mode}' 无效，必须为 probability 或 cooldown")
            fixed.append("偷图模式已重置为 probability")
            fixed_values["steal_mode"] = "probability"
        if not isinstance(cfg.steal_chance, (int, float)) or not (0 <= cfg.steal_chance <= 1):
            errors.append("偷图概率必须在0-1之间")
            fixed.append("偷图概率已重置为0.6")
            fixed_values["steal_chance"] = 0.6
        if not isinstance(cfg.steal_pool_capacity, int) or cfg.steal_pool_capacity < 10:
            errors.append("待审核池容量必须是不小于10的整数")
            fixed.append("待审核池容量已重置为200")
            fixed_values["steal_pool_capacity"] = 200
        source_limits = {
            "external_source_max_items": (1, 20_000, 2000),
            "external_source_max_image_bytes": (1024, 1024 * 1024 * 1024, 32 * 1024 * 1024),
            "external_source_max_archive_bytes": (
                1024,
                8 * 1024 * 1024 * 1024,
                1024 * 1024 * 1024,
            ),
            "external_source_max_uncompressed_bytes": (
                1024,
                16 * 1024 * 1024 * 1024,
                4 * 1024 * 1024 * 1024,
            ),
            "external_source_max_pixels": (1, 200_000_000, 40_000_000),
        }
        for name, (minimum, maximum, default) in source_limits.items():
            value = getattr(cfg, name, default)
            if not isinstance(value, int) or not minimum <= value <= maximum:
                errors.append(f"外部源限制 {name} 超出安全范围")
                fixed_values[name] = default
        if any(name in fixed_values for name in source_limits):
            fixed.append("外部源资源限制已恢复为安全默认值")
        if errors:
            logger.warning(f"配置验证发现问题: {'; '.join(errors)}")
        if fixed:
            logger.info(f"配置已自动修复: {'; '.join(fixed)}")
            try:
                self.update_config(fixed_values)
            except Exception as e:
                logger.error(f"持久化配置修复失败: {e}")
        return True

    def _get_event_handler(
        self,
        *,
        log_message: str | None = None,
        log_level: str = "warning",
    ):
        """获取可用的 EventHandler 实例，集中记录缺失日志。"""
        event_handler = getattr(self, "event_handler", None)
        if event_handler is None and log_message:
            if log_level == "debug":
                logger.debug(log_message)
            elif log_level == "error":
                logger.error(log_message)
            else:
                logger.warning(log_message)
        return event_handler

    def _safe_create_task(self, coro, *, name: str = "") -> asyncio.Task:
        """创建 fire-and-forget task，并复用 TaskScheduler 的异常日志。"""
        return TaskScheduler.create_detached_task(coro, name=name)

    async def _migrate_legacy_category_storage(self) -> None:
        """迁移旧中文分类目录、SQLite 分类字段和待审核分类。"""
        mapping = self.plugin_config.get_legacy_category_key_map()
        if not mapping:
            return

        db = self.db_service
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        index = db.get_index_cache_readonly() if db else {}

        for old_key, new_key in mapping.items():
            if old_key == new_key:
                continue
            old_dir = self.categories_dir / old_key
            new_dir = self.plugin_config.ensure_category_dir(new_key)

            for stored_path, meta in list(index.items()):
                if not isinstance(meta, dict):
                    continue
                path_obj = Path(stored_path)
                in_old_dir = path_obj.parent.name.casefold() == old_key.casefold()
                if str(meta.get("category", "") or "") != old_key and not in_old_dir:
                    continue

                target_path = path_obj
                moved_file = False
                if in_old_dir:
                    target_path = new_dir / path_obj.name
                    if target_path.exists() and target_path.resolve() != path_obj.resolve():
                        stem, suffix = target_path.stem, target_path.suffix
                        counter = 1
                        while target_path.exists():
                            target_path = new_dir / f"{stem}_legacy{counter}{suffix}"
                            counter += 1
                    if path_obj.is_file() and target_path != path_obj:
                        try:
                            await asyncio.to_thread(shutil.move, str(path_obj), str(target_path))
                            moved_file = True
                        except OSError as exc:
                            logger.warning(f"旧分类图片迁移失败 {path_obj}: {exc}")
                            target_path = path_obj

                updates = {"category": new_key}
                if db:
                    if str(target_path) != str(path_obj) and moved_file:
                        if not await db.move_path(str(path_obj), str(target_path), new_key, updates):
                            await asyncio.to_thread(shutil.move, str(target_path), str(path_obj))
                            await db.update_path(str(path_obj), updates)
                    else:
                        await db.update_path(str(path_obj), updates)

            if old_dir.is_dir():
                for child in list(old_dir.iterdir()):
                    if not child.is_file() or child.suffix.lower() not in image_exts:
                        continue
                    target = new_dir / child.name
                    if target.exists():
                        stem, suffix = target.stem, target.suffix
                        counter = 1
                        while target.exists():
                            target = new_dir / f"{stem}_legacy{counter}{suffix}"
                            counter += 1
                    try:
                        await asyncio.to_thread(shutil.move, str(child), str(target))
                    except OSError as exc:
                        logger.warning(f"孤立旧分类图片迁移失败 {child}: {exc}")

            if db:
                pending_rows, _, _ = db.get_pending_paginated(page=1, page_size=100000)
                for row in pending_rows:
                    if str(row.get("category", "") or "") == old_key:
                        await db.update_pending(int(row["id"]), {"category": new_key})

            try:
                if old_dir.is_dir() and not any(old_dir.iterdir()):
                    old_dir.rmdir()
            except OSError as exc:
                logger.debug(f"旧分类目录清理跳过 {old_dir}: {exc}")

        logger.info(
            "旧分类兼容迁移完成: "
            + ", ".join(f"{old}->{new}" for old, new in mapping.items())
        )

    def _relocated_path(self, raw_path: Any, anchor: str, depth: int) -> Path | None:
        """旧绝对路径 → 当前数据目录下同一相对位置的文件；不需要或找不到时返回 None。"""
        raw = str(raw_path or "")
        if not raw:
            return None
        base = self.base_dir.resolve()
        try:
            Path(raw).resolve().relative_to(base)
            return None
        except (OSError, ValueError):
            pass
        parts = (PureWindowsPath(raw) if "\\" in raw else PurePosixPath(raw)).parts
        if len(parts) <= depth or parts[-(depth + 1)] != anchor:
            return None
        candidate = base / anchor / Path(*parts[-depth:])
        return candidate if candidate.is_file() else None

    async def _relocate_library_paths(self) -> int:
        """数据目录整体搬家后（如从 AstrBot 版迁移），把库里的绝对路径改到当前目录。

        表情与待审核记录保存的是绝对路径。不处理的话，启动清理会把库里的条目
        当成「文件已丢失」删掉，或把复制过来的图片当成「孤儿文件」删掉。
        只要文件在当前数据目录的同一相对位置（categories/<分类>/<文件>、
        pending/<文件>），就改写路径并保留描述、标签、收藏、使用次数等元数据。
        """
        db = self.db_service
        moved = 0
        for old_path in db.get_all_paths():
            target = self._relocated_path(old_path, "categories", depth=2)
            if target is not None and await db.move_path(
                str(old_path), str(target), target.parent.name
            ):
                moved += 1
        rows, _, _ = db.get_pending_paginated(page=1, page_size=100000)
        for row in rows:
            target = self._relocated_path(row.get("path"), "pending", depth=1)
            if target is not None and await db.update_pending(
                int(row["id"]), {"path": str(target)}, allowed_fields=("path",)
            ):
                moved += 1
        if moved:
            logger.info(f"[Stealer] 数据目录已迁移，改写了 {moved} 条记录的文件路径")
        return moved

    def _precheck_image_file(self, file_path: str) -> tuple[bool, str]:
        """轻量校验图片，避免明显无效文件进入 VLM 流水线。"""
        path = Path(file_path)
        if not path.exists():
            return False, f"图片文件不存在: {file_path}"
        if not path.is_file():
            return False, f"路径不是文件: {file_path}"
        if path.suffix.lower() not in PluginAPI.ALLOWED_IMAGE_EXTS:
            return False, f"不支持的图片类型: {path.suffix or '无扩展名'}"
        try:
            size = path.stat().st_size
        except OSError as e:
            return False, f"无法读取图片文件: {e}"
        if size <= 0:
            return False, "图片文件为空"
        if size > 25 * 1024 * 1024:
            return False, "图片文件过大，超过 25MB"
        try:
            with Image.open(path) as img:
                img.verify()
        except Exception as e:
            return False, f"图片格式校验失败: {e}"
        return True, ""

    def get_event_target(self, event: StealerEvent) -> tuple[str, str]:
        if self.plugin_config is None:
            return "", ""
        try:
            return self.plugin_config.get_event_target(event)
        except Exception:
            return "", ""

    def _is_action_enabled_for_event(self, action: str, event: StealerEvent) -> bool:
        """检查指定操作是否在当前事件中启用。"""
        if self.plugin_config is None:
            return True
        try:
            return bool(self.plugin_config.is_action_allowed(action, event))
        except Exception:
            return True

    def is_send_enabled_for_event(self, event: StealerEvent) -> bool:
        return self._is_action_enabled_for_event("send", event)

    def is_steal_enabled_for_event(self, event: StealerEvent) -> bool:
        return self._is_action_enabled_for_event("steal", event)

    def begin_force_capture(self, event: StealerEvent, seconds: int) -> None:
        """委托给 EventHandler。"""
        event_handler = self._get_event_handler(
            log_message="event_handler 未初始化，无法进入强制接收模式"
        )
        if event_handler is None:
            return
        event_handler.begin_force_capture(event, seconds)

    def get_force_capture_entry(self, event: StealerEvent) -> dict[str, object] | None:
        """委托给 EventHandler。"""
        event_handler = self._get_event_handler(
            log_message="event_handler 未初始化，无法获取强制接收状态",
            log_level="debug",
        )
        if event_handler is None:
            return None
        return event_handler.get_force_capture_entry(event)

    def consume_force_capture(self, event: StealerEvent) -> None:
        """委托给 EventHandler。"""
        event_handler = self._get_event_handler(
            log_message="event_handler 未初始化，无法消费强制接收状态",
            log_level="debug",
        )
        if event_handler is None:
            return
        event_handler.consume_force_capture(event)

    def _apply_plugin_config_updates(self, config_dict: dict) -> bool:
        """持久化已知配置项并同步运行时权重。"""
        if not self.plugin_config.update_config(config_dict):
            return False
        self._sync_similarity_weights()
        return True

    def _sync_similarity_weights(self) -> None:
        """把文字距离融合权重同步到 text_similarity 模块（魔法数字 → 配置项）。"""
        try:
            from .core.search import text_similarity

            cfg = self.plugin_config
            preset = cfg.SIM_WEIGHT_PRESETS.get(
                getattr(cfg, "sim_weight_preset", "balanced"), cfg.SIM_WEIGHT_PRESETS["balanced"]
            )
            text_similarity.configure_similarity(
                weights={
                    "ngram": preset["ngram"],
                    "cosine": preset["cosine"],
                    "substring": preset["substring"],
                    "char": preset["char"],
                    "edit": preset["edit"],
                },
                negation_penalty=preset["negation"],
            )
        except Exception as e:
            logger.warning(f"[Config] 同步相似度权重失败: {e}")

    def _sync_image_processor_from_runtime(self) -> None:
        cfg = self.plugin_config
        final_prompts = cfg.get_prompts(
            {"EMOJI_CLASSIFICATION_PROMPT": getattr(self, "EMOJI_CLASSIFICATION_PROMPT", None)}
        )
        self.image_processor_service.update_config(
            categories=list(cfg.categories or []) or list(cfg.DEFAULT_CATEGORIES),
            emoji_classification_prompt=final_prompts.get("emoji_classification_prompt"),
        )

    def update_config(self, config_dict: dict) -> bool:
        """从配置字典更新插件配置。"""
        if not config_dict:
            return False
        try:
            if self.plugin_config is None or not self._apply_plugin_config_updates(config_dict):
                return False
            self._sync_image_processor_from_runtime()
            try:
                cats = list(self.plugin_config.categories or []) or list(
                    self.plugin_config.DEFAULT_CATEGORIES
                )
                self.plugin_config.ensure_category_dirs(cats)
            except Exception as e:
                logger.warning(f"[Config] 创建分类目录失败: {e}")
            logger.debug("[Config] 配置已保存，后续处理将使用新配置")
            return True
        except Exception as e:
            logger.error(f"更新配置失败: {e}")
            return False

    def apply_external_config(self, config: dict[str, Any]) -> set[str]:
        """mohobot 面板保存插件配置后同步到运行时；返回值有变化的键。

        面板已经把配置写进存档，这里只更新内存，不再回写。
        """
        cfg = self.plugin_config
        fields = getattr(type(cfg), "model_fields", {}) or {}
        changed: set[str] = set()
        for key, value in (config or {}).items():
            if key not in fields or getattr(cfg, key, None) == value:
                continue
            try:
                setattr(cfg, key, value)
            except Exception as e:
                logger.warning(f"[Config] 应用配置项 {key} 失败: {e}")
                continue
            changed.add(key)
        data = getattr(cfg, "_data", None)
        if isinstance(data, dict):
            data.update({k: v for k, v in (config or {}).items() if k in fields})
        if changed:
            self._sync_similarity_weights()
            self._sync_image_processor_from_runtime()
            if changed & self.EMBEDDING_CONFIG_KEYS:
                self.schedule_embedding_init()
            logger.info(f"[Config] 已应用面板配置: {', '.join(sorted(changed))}")
        return changed

    # ── /meme 指令 ──────────────────────────────────────────

    # 子命令 → (处理器属性路径, 是否仅管理员, 位置参数个数)
    COMMANDS: dict[str, tuple[str, bool, int]] = {
        "on": ("command_handler.meme_on", True, 0),
        "off": ("command_handler.meme_off", True, 0),
        "auto_on": ("command_handler.auto_on", True, 0),
        "auto_off": ("command_handler.auto_off", True, 0),
        "group": ("target_commands.group_filter", True, 5),
        "偷": ("command_handler.capture", True, 0),
        "status": ("command_handler.status", False, 0),
        "tag_stats": ("command_handler.tag_stats", True, 1),
        "clean": ("command_handler.clean", True, 1),
        "capacity": ("command_handler.enforce_capacity", True, 0),
        "list": ("image_commands.list_images", False, 3),
        "delete": ("image_commands.delete_image", True, 1),
        "blacklist": ("image_commands.blacklist_image", True, 1),
        "scope": ("image_commands.set_image_scope", True, 2),
        "rebuild_index": ("index_commands.rebuild_index", True, 0),
    }

    COMMAND_HELP = (
        "表情包小偷指令（/meme 前缀）：\n"
        "/meme status — 运行状态与表情包统计\n"
        "/meme list [分类] [每页数量] [页码] — 列出表情包\n"
        "管理员：\n"
        "/meme on | off — 开关表情包偷取\n"
        "/meme auto_on | auto_off — 开关聊天配表情\n"
        "/meme 偷 — 30 秒内发的下一张图直接入库\n"
        "/meme group <send|steal> <wl|bl> <add|del|clear|show> [目标] — 名单管理\n"
        "/meme delete <序号|文件名> / blacklist <序号|文件名>\n"
        "/meme scope <序号|文件名> <public|local>\n"
        "/meme clean [raw] / capacity / rebuild_index / tag_stats [N]"
    )

    async def run_command(
        self, event: StealerEvent, args: list[str], *, is_admin: bool
    ) -> AsyncIterator[Any]:
        """执行 /meme 子命令，逐条产出回复（文本或 MessageEventResult）。"""
        if not args or args[0].lower() in {"help", "帮助"}:
            yield event.plain_result(self.COMMAND_HELP)
            return
        name = args[0] if args[0] in self.COMMANDS else args[0].lower()
        spec = self.COMMANDS.get(name)
        if spec is None:
            yield event.plain_result(f"未知子命令: {args[0]}\n\n{self.COMMAND_HELP}")
            return
        handler_path, admin_only, arity = spec
        if admin_only and not is_admin:
            yield event.plain_result("❌ 该指令仅限管理员使用")
            return
        params = list(args[1:])
        if len(params) > arity:
            # 末位参数吸收多余的词（如文件名里带空格），与位置参数个数对齐
            params = params[: max(arity - 1, 0)] + [" ".join(params[max(arity - 1, 0):])] if arity else []
        owner_name, method_name = handler_path.split(".", 1)
        handler = getattr(getattr(self, owner_name), method_name)
        async for result in handler(event, *params):
            yield result

    # ── LLM 工具 ────────────────────────────────────────────

    @staticmethod
    async def _collect_text(results: AsyncIterator[Any]) -> str:
        parts: list[str] = []
        async for item in results:
            text = item.get_plain_text() if isinstance(item, MessageEventResult) else str(item or "")
            if text:
                parts.append(text)
        return "\n".join(parts)

    async def search_meme(self, event: StealerEvent, description: str) -> str:
        return await self._collect_text(self._search_meme_impl(event, description))

    async def send_meme(self, event: StealerEvent, emoji_id: Any) -> str:
        return await self._collect_text(self._send_meme_impl(event, emoji_id))

    async def steal_meme(self, event: StealerEvent, image_ref: str = "") -> str:
        return await self._collect_text(self._steal_sticker_impl(event, image_ref))

    def has_queued_meme(self, event: StealerEvent) -> bool:
        state = self._emoji_sender_engine.emoji_turn_state(event)
        return state._queued is not None

    # ── 消息钩子 ────────────────────────────────────────────

    def cancel_pending_meme(self, event: StealerEvent) -> None:
        """新消息到达：取消该 bot 在本会话里还没发出的表情。"""
        if getattr(self.plugin_config, "auto_meme_cancel_on_new_message", True):
            self._emoji_sender_engine.cancel_pending_auto_emoji(event)

    async def on_message(self, event: StealerEvent) -> None:
        """消息观察：记录聊天上下文，偷取消息中的图片并标注入库。"""
        event_handler = self._get_event_handler(
            log_message="[Stealer] event_handler 未初始化，跳过消息处理",
            log_level="debug",
        )
        if event_handler is None:
            return
        try:
            await event_handler.on_message(event)
        except Exception as e:
            logger.error(f"[Stealer] 处理消息时发生错误: {e}", exc_info=True)

    async def build_meme_hint(self, event: StealerEvent) -> str:
        """本轮通过门控时返回选图提示（由 mohobot 并入本轮请求，不进上下文）。"""
        try:
            return await self._emoji_sender_engine.build_meme_hint(event)
        except Exception as e:
            logger.warning(f"[Stealer] 生成选图提示失败: {e}")
            return ""

    def after_reply(self, event: StealerEvent, reply_text: str = "") -> bool:
        """本条消息处理完毕（回复已发出）：补录 Bot 回复，发出本轮排队的表情。"""
        text = str(reply_text or "").strip()
        event_handler = getattr(self, "event_handler", None)
        if text and event_handler is not None:
            try:
                event_handler.chat_context.record_bot_reply(event, text)
            except Exception as e:
                logger.debug(f"[Stealer] 记录 Bot 回复失败: {e}")
        return self._emoji_sender_engine.dispatch_queued_meme(event, text)

    # ── 生命周期 ────────────────────────────────────────────

    async def _load_prompts(self) -> None:
        prompts_path = self.plugin_dir / "prompts.json"
        if not prompts_path.exists():
            return
        try:
            content = await asyncio.to_thread(prompts_path.read_text, encoding="utf-8-sig")
            self._apply_prompts(json.loads(content.lstrip("\ufeff")))
        except Exception as e:
            logger.error(f"初始化提示词失败: {e}")

    async def initialize(self):
        """初始化插件运行时资源（本地 IO）；嵌入向量的初始化与回填放到后台。"""
        try:
            self._validate_config()
            self.plugin_config.ensure_base_dirs()
            await self._relocate_library_paths()
            await self._migrate_legacy_category_storage()
            self.plugin_config.ensure_category_dirs(
                list(self.plugin_config.categories or []) or list(
                    self.plugin_config.DEFAULT_CATEGORIES
                )
            )
            await self.source_service.initialize()
            await self.image_processor_service._auto_migrate_categories()
            self._auto_merge_existing_categories()
            await self._load_prompts()
            await self.index_manager.load_index()
            await self.index_manager.migrate_blacklist()
            await self.maintenance.run_startup_cleanup()
            self._sync_image_processor_from_runtime()
            self._sync_similarity_weights()  # 启动时把持久化的文字距离权重同步到 text_similarity 模块
            self.maintenance.start_periodic_tasks()
            await self.event_handler.start_background_workers()
            self.schedule_embedding_init()
            if not self.models.describe("vision"):
                logger.warning(
                    "[Stealer] 未配置视觉模型：请在插件配置填写 vision_model，"
                    "或在 mohobot 全局配置设置 llm.vision_model，否则无法审核与标注表情包"
                )
            logger.info("[Stealer] 插件初始化完成")
        except Exception as e:
            logger.error(f"初始化插件失败: {e}")
            raise

    def schedule_embedding_init(self) -> None:
        """后台初始化向量库并为缺向量的旧表情补算（不阻塞 mohobot 启动）。"""
        if not self.plugin_config.enable_embedding_search:
            return
        previous = self._embedding_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._embedding_task = self._safe_create_task(
            self._init_embedding(), name="meme_stealer_embedding_init"
        )

    async def _init_embedding(self) -> None:
        try:
            embedding_service = self.meme_selector.embedding_service
            if embedding_service is None:
                return
            await embedding_service.initialize()
            # 分批回填旧数据（每批 20 条）
            backfilled = await embedding_service.backfill_existing(batch_size=20)
            if backfilled > 0:
                logger.info(f"[Embedding] 旧数据回填完成: {backfilled} 条新向量")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[Embedding] 初始化失败: {e}")

    async def terminate(self):
        """释放运行时资源。"""
        if self._terminated:
            return
        self._terminated = True
        task = self._embedding_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self.task_scheduler.cancel_task("raw_cleanup_loop")
            await self.task_scheduler.cancel_task("capacity_control_loop")
        except Exception:
            pass
        if self.source_service:
            try:
                await self.source_service.close()
            except Exception:
                pass
        if self.task_scheduler:
            try:
                await self.task_scheduler.cleanup()
            except Exception:
                pass
        # 关闭嵌入向量服务
        try:
            smart_service = getattr(self.meme_selector, "_smart_select_service", None)
            if smart_service and smart_service._embedding_service:
                await smart_service._embedding_service.close()
        except Exception:
            pass

        if self.image_processor_service:
            try:
                self.image_processor_service.cleanup()
                self.image_render_service.cleanup()
            except Exception:
                pass
        if self.command_handler:
            try:
                self.command_handler.cleanup()
            except Exception:
                pass
        if self.event_handler:
            try:
                await self.event_handler.stop_background_workers()
            except Exception:
                pass
            try:
                await self.event_handler.cleanup_async()
            except Exception:
                pass
            try:
                self.event_handler.cleanup()
            except Exception:
                pass
        try:
            await self.models.close()
        except Exception:
            pass
        logger.info("[Stealer] 插件资源清理完成")
