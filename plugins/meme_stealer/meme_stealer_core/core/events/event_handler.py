import asyncio
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

from ...host import Image, MessageChain, Plain, StealerEvent, logger
from ...host.components import is_inline_image
from ..db.index_manager import delete_index_paths

from ..util.normalization import bounded_int
from ..util.safe_io import safe_remove_file
from ..maintenance.retention import (
    eviction_candidates,
    library_counts,
    redundancy_eviction_candidates,
    soft_limit_chance,
)
from .background_steal_queue import BackgroundStealQueue
from .chat_context_buffer import ChatContextBuffer
from .event_context import get_event_session_key
from .platform_detector import PlatformDetector
from .image_download_service import ImageDownloadService


class EventHandler:
    """事件处理服务类，负责处理所有与插件相关的事件操作。"""

    HTTP_TIMEOUT_SECONDS = 30
    HTTP_CONNECTOR_LIMIT = 10
    HTTP_CONNECTOR_LIMIT_PER_HOST = 5
    HTTP_DNS_CACHE_SECONDS = 300

    def __init__(self, plugin_instance: Any):
        """初始化事件处理服务。

        Args:
            plugin_instance: Main 实例，用于访问插件的配置和服务
        """
        self.plugin = plugin_instance
        self._cleaned = False  # 清理标志位

        # 图片处理节流相关
        self._last_process_time: float = 0.0  # 上次处理时间（用于interval和cooldown模式）

        # 强制捕获窗口（需要锁保护）
        self._force_capture_windows: dict[str, dict[str, object]] = {}
        self._force_capture_lock = threading.RLock()  # 可重入锁，保护并发访问

        # 子服务
        self._platform_detector = PlatformDetector(plugin_instance)
        self._image_download_service = ImageDownloadService(plugin_instance)
        self._background_queue: BackgroundStealQueue | None = None
        self.chat_context = ChatContextBuffer()

        # 软上限：库存数量缓存，避免每条消息都全量读取索引
        self._library_count_cache: tuple[float, int] = (0.0, 0)

    async def start_background_workers(self) -> None:
        """Start the bounded passive-steal pipeline."""
        if self._background_queue is None:
            self._background_queue = BackgroundStealQueue(
                self.plugin, capacity=32, worker_count=2
            )
        await self._background_queue.start()

    async def stop_background_workers(self) -> None:
        queue = self._background_queue
        self._background_queue = None
        if queue is not None:
            await queue.stop()

    def _extract_store_emoji_urls(self, event: StealerEvent) -> list[str]:
        """从 OneBot raw_message 里提取 QQ 商城表情的可下载 URL。"""
        return self._platform_detector.extract_store_emoji_urls(event)

    async def _download_to_temp(
        self, url: str, *, log_download: bool = False
    ) -> tuple[str | None, bool]:
        """从 URL 下载文件到临时文件（已迁移到 ImageDownloadService）。"""
        return await self._image_download_service.download_to_temp(url, log_download=log_download)

    async def _download_url_to_temp(self, url: str) -> tuple[str | None, bool]:
        """从 URL 下载文件到临时文件（已迁移到 ImageDownloadService）。"""
        return await self._image_download_service.download_url_to_temp(url)

    # ===== EventHandler 核心逻辑 =====
    LIBRARY_COUNT_TTL_SECONDS = 60.0

    def _should_process_image(self, *, to_pending: bool = False) -> bool:
        """根据偷图模式（概率/冷却）和软上限判断是否应该处理图片。

        - probability 模式：每次收到图片按 steal_chance 概率决定
        - cooldown 模式：两次偷取之间至少间隔 image_processing_cooldown 秒
        - 软上限：目标库存超过上限后，采纳率按 (上限/当前数量)^k 衰减，且不低于下限
        """
        steal_mode = self.plugin.plugin_config.steal_mode
        pressure = self._soft_limit_factor(to_pending=to_pending)

        if steal_mode == "cooldown":
            if pressure < 1.0 and random.random() >= pressure:
                logger.debug(f"软上限：跳过偷取（采纳率={pressure:.3f}）")
                return False
            return self._check_cooldown()
        return self._check_probability(pressure)

    def _current_store_count(self, *, to_pending: bool) -> int:
        db = getattr(self.plugin, "db_service", None)
        if db is None:
            return 0
        if to_pending:
            try:
                return int(db.count_pending())
            except Exception:
                return 0
        now = time.monotonic()
        cached_at, cached = self._library_count_cache
        if now - cached_at < self.LIBRARY_COUNT_TTL_SECONDS:
            return cached
        try:
            count = library_counts(db.get_index_cache_readonly())["automatic"]
        except Exception:
            count = cached
        self._library_count_cache = (now, count)
        return count

    def invalidate_library_count(self) -> None:
        self._library_count_cache = (0.0, 0)

    def _soft_limit_factor(self, *, to_pending: bool) -> float:
        """返回软上限下的采纳率系数（1.0 表示未超限）。"""
        cfg = self.plugin.plugin_config
        cap_name = "steal_pool_capacity" if to_pending else "max_reg_num"
        try:
            cap = int(getattr(cfg, cap_name, 0) or 0)
            exponent = float(getattr(cfg, "soft_limit_exponent", 2.0))
            floor = float(getattr(cfg, "soft_limit_min_chance", 0.02))
        except (TypeError, ValueError):
            return 1.0
        count = self._current_store_count(to_pending=to_pending)
        return soft_limit_chance(1.0, count, cap, exponent, floor)

    def _check_cooldown(self) -> bool:
        """冷却模式：两次处理之间至少间隔 N 秒。"""
        cooldown = self.plugin.plugin_config.image_processing_cooldown
        try:
            cooldown = int(cooldown)
        except Exception:
            cooldown = 10

        current_time = time.time()
        time_since_last = current_time - self._last_process_time

        if time_since_last < cooldown:
            logger.debug(f"冷却检查：跳过（冷却={cooldown}秒，距上次={time_since_last:.1f}秒）")
            return False

        self._last_process_time = current_time
        logger.debug(f"冷却检查：通过（冷却={cooldown}秒，距上次={time_since_last:.1f}秒）")
        return True

    def mark_processing_started(self) -> None:
        """后台 worker 开始实际处理时调用。

        cooldown 模式必须覆盖真实处理开始时间，而不是仅覆盖入队时间；
        否则慢 VLM 下消息仍会在冷却窗口后持续入队，worker 随后连续处理，
        冷却间隔会失去意义。
        """
        try:
            steal_mode = self.plugin.plugin_config.steal_mode
        except Exception:
            return
        if steal_mode == "cooldown":
            self._last_process_time = time.time()
            logger.debug("冷却检查：后台处理开始，刷新冷却窗口")

    def _check_probability(self, pressure: float = 1.0) -> bool:
        """概率模式：按 steal_chance（叠加软上限衰减）决定是否偷取。"""
        steal_chance = self.plugin.plugin_config.steal_chance
        try:
            steal_chance = float(steal_chance)
        except Exception:
            steal_chance = 0.6
        if pressure < 1.0 and steal_chance > 0:
            cfg = self.plugin.plugin_config
            floor = float(getattr(cfg, "soft_limit_min_chance", 0.02) or 0.0)
            steal_chance = max(min(steal_chance, floor), steal_chance * pressure)
            logger.debug(f"软上限：偷图概率衰减为 {steal_chance:.3f}")

        if steal_chance <= 0:
            logger.debug("偷图概率为0，跳过偷取")
            return False
        if steal_chance >= 1.0:
            logger.debug("偷图概率为1.0，直接通过")
            return True
        if random.random() >= steal_chance:
            logger.debug(f"概率检查：未通过（概率={steal_chance}）")
            return False

        logger.debug(f"概率检查：通过（概率={steal_chance}）")
        return True

    def _library_vectors(self) -> dict:
        selector = getattr(self.plugin, "meme_selector", None)
        service = getattr(selector, "embedding_service", None)
        if service is None:
            return {}
        try:
            return service.load_vector_map()
        except Exception as e:
            logger.warning(f"[容量控制] 读取语义向量失败: {e}")
            return {}

    def _select_items_for_removal(self, image_index: dict) -> list[tuple[str, int]]:
        """软上限触发后的淘汰：优先按语义冗余，没有向量时退回使用次数+入库时间。"""
        cfg = self.plugin.plugin_config
        try:
            limit = int(cfg.max_reg_num)
        except (TypeError, ValueError):
            return []
        if library_counts(image_index)["automatic"] <= limit:
            return []

        vectors = self._library_vectors()
        if vectors:
            removed, radius = redundancy_eviction_candidates(
                image_index,
                vectors,
                radius=getattr(cfg, "eviction_radius", 0.0),
                min_neighbors=getattr(cfg, "eviction_min_neighbors", 3),
                grace_days=getattr(cfg, "eviction_grace_days", 7),
            )
            logger.info(
                f"[容量控制] 语义冗余淘汰: r0={radius:.4f}, 候选 {len(removed)} 张"
            )
            return removed

        logger.info("[容量控制] 无可用语义向量，退回按使用次数和入库时间淘汰")
        return eviction_candidates(image_index, limit)

    def _resolve_index_file_path(self, path_str: str, image_info: dict | None) -> str | None:
        candidates: list[Path] = []
        if path_str:
            candidates.append(Path(path_str))

        category = ""
        if isinstance(image_info, dict):
            category = str(image_info.get("category", "") or "").strip()

        categories_dir = getattr(self.plugin, "categories_dir", None)
        if category and categories_dir:
            candidates.append(Path(categories_dir) / category / Path(path_str).name)

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
        return None

    def _normalize_capacity_index_paths(self, image_index: dict) -> tuple[int, int]:
        rekeyed = 0
        stale_removed = 0

        for path_str, image_info in list(image_index.items()):
            if not isinstance(path_str, str):
                image_index.pop(path_str, None)
                stale_removed += 1
                continue

            resolved = self._resolve_index_file_path(
                path_str, image_info if isinstance(image_info, dict) else None
            )
            if resolved is None:
                image_index.pop(path_str, None)
                stale_removed += 1
                continue

            if resolved != path_str:
                image_index.pop(path_str, None)
                existing = image_index.get(resolved)
                if not isinstance(existing, dict) or (
                    isinstance(image_info, dict)
                    and int(image_info.get("created_at", 0) or 0)
                    < int(existing.get("created_at", 0) or 0)
                ):
                    image_index[resolved] = image_info
                rekeyed += 1

        return rekeyed, stale_removed

    async def _enforce_capacity(self, image_index: dict) -> list[str]:
        """容量控制，按加权排序删除超出通用库限制的表情包（文件+索引一起清理）。

        Args:
            image_index: 索引字典

        Returns:
            list[str]: 成功删除的文件路径列表
        """
        files_actually_deleted: list[str] = []

        try:
            rekeyed, stale_removed = self._normalize_capacity_index_paths(image_index)
            if rekeyed or stale_removed:
                logger.info(
                    f"[capacity] Normalized {rekeyed} index paths and removed "
                    f"{stale_removed} stale records before enforcing capacity"
                )

            items_to_remove = self._select_items_for_removal(image_index)
            if not items_to_remove:
                return files_actually_deleted

            logger.info(f"[容量控制-索引] 将删除 {len(items_to_remove)} 个通用库淘汰条目")
            self.invalidate_library_count()

            for remove_path, _ in items_to_remove:
                if remove_path not in image_index:
                    continue

                # 收集该条目对应的所有物理文件路径
                entry_files: list[str] = [remove_path]
                if isinstance(image_index[remove_path], dict):
                    category = image_index[remove_path].get("category", "")
                    if category and self.plugin.base_dir:
                        file_name = Path(remove_path).name
                        category_file_path = str(
                            Path(self.plugin.base_dir) / "categories" / category / file_name
                        )
                        # 避免重复添加（正常流程中 remove_path 就是 categories 下的路径）
                        if category_file_path != remove_path:
                            entry_files.append(category_file_path)

                # 先尝试删除文件；只要至少一个文件被删除（或已不存在），就从索引中移除
                file_gone = False
                seen_paths: set[str] = set()
                for file_path in entry_files:
                    if file_path in seen_paths:
                        continue
                    seen_paths.add(file_path)

                    if Path(file_path).exists():
                        try:
                            removed = await safe_remove_file(file_path)
                            if removed:
                                files_actually_deleted.append(file_path)
                                file_gone = True
                            else:
                                logger.warning(f"[容量控制] 删除文件失败: {file_path}")
                        except Exception as e:
                            logger.warning(f"[容量控制] 删除文件异常 {file_path}: {e}")
                    else:
                        # Alternate candidates may be absent; only remove the index
                        # after the primary indexed file is confirmed gone.
                        logger.debug(f"[capacity] Candidate file is already missing: {file_path}")

                if not Path(remove_path).exists():
                    file_gone = True

                # 只有文件确实被清理后才从索引中删除，避免产生新的"僵尸文件"
                if file_gone:
                    await delete_index_paths(self.plugin, [remove_path])
                    del image_index[remove_path]
                else:
                    logger.warning(f"[容量控制] 文件删除失败，保留索引条目: {remove_path}")

        except Exception as e:
            logger.error(f"同步容量控制失败: {e}")

        return files_actually_deleted

    def _get_force_capture_key(self, event) -> str:
        """获取强制捕获的唯一键。

        Args:
            event: 消息事件对象

        Returns:
            str: 唯一键
        """
        return get_event_session_key(event)

    def _cleanup_expired_capture_windows(self) -> int:
        """清理所有过期的强制捕获窗口。

        Returns:
            int: 清理的过期窗口数量
        """
        now = time.time()
        expired_keys = [
            key
            for key, entry in self._force_capture_windows.items()
            if isinstance(entry, dict) and float(entry.get("until", 0)) < now
        ]
        for key in expired_keys:
            self._force_capture_windows.pop(key, None)
        return len(expired_keys)

    def _get_force_capture_sender_id(self, event) -> str | None:
        """获取发送者ID。"""
        try:
            sid = event.get_sender_id()
            if sid:
                return str(sid)
        except Exception:
            pass

        # 单层兜底：从 message_obj.sender 取 user_id
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None) if message_obj is not None else None
        uid = getattr(sender, "user_id", None) if sender is not None else None
        return str(uid) if uid else None

    def begin_force_capture(self, event, seconds: int) -> None:
        """开始强制捕获窗口。

        Args:
            event: 消息事件对象
            seconds: 捕获窗口持续时间（秒）
        """
        key = self._get_force_capture_key(event)
        sender_id = self._get_force_capture_sender_id(event)
        until = time.time() + max(1, int(seconds))
        with self._force_capture_lock:
            self._force_capture_windows[key] = {"until": until, "sender_id": sender_id}

    def get_force_capture_entry(self, event) -> dict[str, object] | None:
        """获取强制捕获条目。

        Args:
            event: 消息事件对象

        Returns:
            dict | None: 捕获条目，如果不存在或已过期则返回None
        """
        # 先清理所有过期的捕获窗口
        self._cleanup_expired_capture_windows()

        key = self._get_force_capture_key(event)
        with self._force_capture_lock:
            entry = self._force_capture_windows.get(key)
            if not entry:
                return None

            try:
                until = float(entry.get("until", 0))
            except Exception:
                self._force_capture_windows.pop(key, None)
                return None

            if time.time() > until:
                self._force_capture_windows.pop(key, None)
                return None

            expected_sender_id = entry.get("sender_id")
            if expected_sender_id:
                current_sender_id = self._get_force_capture_sender_id(event)
                if current_sender_id and str(current_sender_id) != str(expected_sender_id):
                    return None

            return entry

    def consume_force_capture(self, event) -> None:
        """消费强制捕获条目。

        Args:
            event: 消息事件对象
        """
        key = self._get_force_capture_key(event)
        with self._force_capture_lock:
            self._force_capture_windows.pop(key, None)

    @staticmethod
    def _get_media_ref(img: Image) -> str:
        """Extract a local path, HTTP URL or inline base64 ref from an image component.

        Prefer an attribute that points at an existing local file. A stale
        local path must not shadow a usable URL in another attribute.
        """
        candidates: list[str] = []
        for attr in ("path", "file", "url"):
            value = getattr(img, attr, "") or ""
            if not value:
                continue
            ref = str(value)
            candidates.append(ref)
            if not is_inline_image(ref) and os.path.exists(ref):
                return ref
        for ref in candidates:
            if ref.startswith(("http://", "https://")):
                return ref
        for ref in candidates:
            if is_inline_image(ref):
                return ref
        return ""

    async def _resolve_media_ref(self, img: Image) -> str:
        """同 _get_media_ref；NapCat 群图只有文件名时先经 get_image 换成 data URI。"""
        media_ref = self._get_media_ref(img)
        if media_ref:
            return media_ref
        resolve = getattr(img, "resolve_url", None)
        if callable(resolve):
            try:
                await resolve()
            except Exception as e:
                logger.debug(f"解析图片引用失败: {e}")
                return ""
        return self._get_media_ref(img)

    def _extract_raw_image_data(
        self, event: StealerEvent
    ) -> tuple[list[dict], dict[str, dict]]:
        image_segments: list[dict] = []
        file_map: dict[str, dict] = {}
        try:
            raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
            logger.debug(
                f"raw_event type: {type(raw_event).__name__}, is_dict: {isinstance(raw_event, dict)}"
            )
            raw_message = None
            if isinstance(raw_event, dict):
                raw_message = raw_event.get("message")
            elif raw_event is not None:
                raw_message = getattr(raw_event, "message", None)
            logger.debug(
                f"raw_message type: {type(raw_message).__name__ if raw_message else 'None'}"
            )
            if isinstance(raw_message, list):
                image_segments = [
                    segment
                    for segment in raw_message
                    if isinstance(segment, dict) and segment.get("type") == "image"
                ]
                for segment in image_segments:
                    data = segment.get("data", {}) or {}
                    if not isinstance(data, dict):
                        continue
                    file_ref = self._platform_detector._normalize_str(data.get("file", ""))
                    if file_ref and file_ref not in file_map:
                        file_map[file_ref] = data
                logger.debug(
                    f"提取到 {len(image_segments)} 个原始图片段, {len(file_map)} 个文件映射"
                )
        except Exception as exc:
            logger.debug(f"提取原始图片段失败: {exc}")
            return [], {}
        return image_segments, file_map

    def _collect_image_candidates(
        self,
        event: StealerEvent,
        images: list[Image],
        image_segments: list[dict],
        file_map: dict[str, dict],
        origin_target: str,
    ) -> list[tuple[int, Image, dict]]:
        candidates: list[tuple[int, Image, dict]] = []
        for index, image in enumerate(images):
            try:
                is_platform_emoji = self._platform_detector.check_platform_emoji_metadata(
                    image,
                    event,
                    img_index=index,
                    image_segments=image_segments,
                    image_file_map=file_map,
                )
                if not is_platform_emoji:
                    subtype = getattr(image, "subType", "unknown")
                    logger.debug(f"跳过非表情包图片 (subType={subtype})")
                    continue

                extra_meta = None
                try:
                    segment = image_segments[index] if 0 <= index < len(image_segments) else None
                    data = segment.get("data", {}) if isinstance(segment, dict) else {}
                    if isinstance(data, dict) and (
                        data.get("emoji_id") or data.get("emoji_package_id")
                    ):
                        extra_meta = {
                            "source": "qq_store",
                            "qq_emoji_id": str(data.get("emoji_id") or ""),
                            "qq_emoji_package_id": str(data.get("emoji_package_id") or ""),
                            "origin_url": self._platform_detector._normalize_str(
                                data.get("url", "")
                            ),
                            "qq_key": self._platform_detector._normalize_str(
                                data.get("key", "")
                            ),
                        }
                except Exception:
                    extra_meta = None

                if origin_target:
                    if extra_meta is None:
                        extra_meta = {}
                    extra_meta["origin_target"] = origin_target
                candidates.append((index, image, extra_meta or {}))
            except Exception as exc:
                logger.error(f"收集图片信息失败: {exc}")
        return candidates

    def build_chat_context(self, event: StealerEvent) -> str:
        """渲染图片所在消息之前的聊天记录（条数来自配置），供 VLM 理解图片。"""
        cfg = getattr(self.plugin, "plugin_config", None)
        if cfg is None or event is None:
            return ""
        sender_limit = bounded_int(
            getattr(cfg, "vlm_context_sender_messages", 5), 5, 0, 50
        )
        group_limit = bounded_int(
            getattr(cfg, "vlm_context_group_messages", 15), 15, 0, 100
        )
        if not sender_limit and not group_limit:
            return ""
        try:
            return self.chat_context.render_for_event(
                event,
                before_seq=int(getattr(event, "_stealer_context_seq", 0) or 0),
                sender_limit=sender_limit,
                group_limit=group_limit,
            )
        except Exception as e:
            logger.debug(f"渲染聊天上下文失败: {e}")
            return ""

    async def _queue_background_capture(
        self,
        candidates: list[tuple[int, Image, dict]],
        store_urls: list[str],
        *,
        to_pending: bool,
        origin_target: str,
        chat_context: str = "",
    ) -> bool:
        if self._background_queue is None:
            return False

        descriptors: list[dict[str, Any]] = []
        for _index, image, extra_meta in candidates:
            media_ref = await self._resolve_media_ref(image)
            if media_ref:
                descriptors.append(
                    {
                        "media_ref": media_ref,
                        "source": "automatic",
                        "to_pending": to_pending,
                        "extra_meta": extra_meta,
                        "chat_context": chat_context,
                    }
                )
        for url in store_urls[:3]:
            extra_meta = {
                "source": "qq_store",
                "origin_url": self._platform_detector._normalize_str(url),
            }
            if origin_target:
                extra_meta["origin_target"] = origin_target
            descriptors.append(
                {
                    "media_ref": url,
                    "source": "automatic",
                    "to_pending": to_pending,
                    "extra_meta": extra_meta,
                    "chat_context": chat_context,
                }
            )
        if descriptors:
            await self._background_queue.submit_capture_async(descriptors)
        return True

    async def _process_image_candidates(
        self,
        event: StealerEvent,
        plugin_instance: Any,
        candidates: list[tuple[int, Image, dict]],
        *,
        to_pending: bool,
        chat_context: str = "",
    ) -> dict[str, Any]:
        merged_index: dict[str, Any] = {}
        if not candidates:
            return merged_index

        logger.debug(f"开始并行下载 {len(candidates)} 张图片")
        download_results = await asyncio.gather(
            *[
                self._image_download_service.download_original_image(image)
                for _index, image, _extra_meta in candidates
            ],
            return_exceptions=True,
        )
        process_tasks = []
        for (_index, _image, extra_meta), result in zip(candidates, download_results):
            if isinstance(result, Exception):
                logger.error(f"下载图片异常: {result}")
                continue
            temp_path, _is_gif = result
            if not temp_path or not Path(temp_path).exists():
                logger.warning(f"临时文件不存在: {temp_path}")
                continue
            process_tasks.append(
                plugin_instance._process_image(
                    event,
                    temp_path,
                    is_temp=True,
                    is_platform_emoji=True,
                    extra_meta=extra_meta,
                    to_pending=to_pending,
                    chat_context=chat_context,
                )
            )

        if not process_tasks:
            return merged_index
        logger.debug(f"开始并行处理 {len(process_tasks)} 张图片")
        results = await asyncio.gather(*process_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"处理图片异常: {result}")
                continue
            success, index = result
            if success and isinstance(index, dict):
                merged_index.update(index)
        return merged_index

    async def _process_store_urls(
        self,
        event: StealerEvent,
        plugin_instance: Any,
        store_urls: list[str],
        *,
        to_pending: bool,
        origin_target: str,
        chat_context: str = "",
    ) -> dict[str, Any]:
        merged_index: dict[str, Any] = {}
        if not store_urls:
            return merged_index

        logger.debug(f"开始并行下载 {min(len(store_urls), 3)} 个商城表情")

        async def download_store_url(url: str) -> tuple[str | None, str]:
            try:
                temp_path, _is_gif = await self._download_url_to_temp(url)
                return temp_path, url
            except Exception as exc:
                logger.error(f"下载商城表情失败: {exc}")
                return None, url

        download_results = await asyncio.gather(
            *[download_store_url(url) for url in store_urls[:3]],
            return_exceptions=True,
        )
        process_tasks = []
        for result in download_results:
            if isinstance(result, Exception):
                continue
            temp_path, url = result
            if not temp_path or not Path(temp_path).exists():
                continue
            extra_meta = {
                "source": "qq_store",
                "origin_url": self._platform_detector._normalize_str(url),
            }
            if origin_target:
                extra_meta["origin_target"] = origin_target
            process_tasks.append(
                plugin_instance._process_image(
                    event,
                    temp_path,
                    is_temp=True,
                    is_platform_emoji=True,
                    extra_meta=extra_meta,
                    to_pending=to_pending,
                    chat_context=chat_context,
                )
            )
        if not process_tasks:
            return merged_index

        results = await asyncio.gather(*process_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                continue
            success, index = result
            if success and isinstance(index, dict):
                merged_index.update(index)
        return merged_index

    async def on_message(self, event: StealerEvent):
        """消息监听：偷取消息中的图片并分类存储。"""
        if self._cleaned or self.plugin is None:
            return
        if not hasattr(event, "get_messages"):
            return
        # 所有消息都进入上下文缓冲，序号用于之后只截取“图片之前”的记录。
        try:
            event._stealer_context_seq = self.chat_context.record_event(event)  # type: ignore[attr-defined]
        except Exception as e:
            logger.debug(f"记录聊天上下文失败: {e}")
        plugin_instance = self.plugin
        force_entry = None
        try:
            force_entry = plugin_instance.get_force_capture_entry(event)
        except (AttributeError, KeyError):
            force_entry = None
        force_active = force_entry is not None
        try:
            if not force_active and not plugin_instance.is_steal_enabled_for_event(event):
                return
        except (AttributeError, TypeError, KeyError):
            return
        if not plugin_instance.steal_meme and not force_active:
            return
        # audit_required=False（默认）→ VLM 审核通过即入库
        # audit_required=True → 自动偷取进 pending 池等待人工审核
        cfg = getattr(plugin_instance, "plugin_config", None)
        audit_required = bool(getattr(cfg, "audit_required", False)) if cfg else False
        to_pending = audit_required
        imgs: list[Image] = [comp for comp in event.get_messages() if isinstance(comp, Image)]
        store_urls = self._extract_store_emoji_urls(event)
        if not imgs and not store_urls:
            return
        if force_active:
            # 强制收录是管理员主动命令，保持同步处理以保留明确的
            # 成功/失败反馈，也保留 convert_to_file_path 兜底。
            await self._handle_force_capture(event, plugin_instance, imgs, store_urls)
            return
        logger.debug(f"开始处理 {len(imgs)} 个表情")
        raw_image_segments, raw_image_file_map = self._extract_raw_image_data(event)
        origin_target_str = ""
        try:
            cfg = getattr(plugin_instance, "plugin_config", None)
            if cfg:
                scope, target_id = cfg.get_event_target(event)
                if scope and target_id:
                    origin_target_str = f"{scope}:{target_id}"
        except (AttributeError, KeyError) as e:
            logger.debug(f"提取来源群信息失败: {e}")
        image_candidates = self._collect_image_candidates(
            event,
            imgs,
            raw_image_segments,
            raw_image_file_map,
            origin_target_str,
        )

        # 冷却只针对确认过的表情包生效。普通图片不能消耗冷却窗口，
        # 否则紧随其后的真实表情包会被直接跳过。
        if (image_candidates or store_urls) and not self._should_process_image(
            to_pending=to_pending
        ):
            return

        # 只有通过概率/冷却判断后，才记录即将执行的自动偷取，避免让检测日志
        # 造成“每个检测到的表情包都会被偷”的误解。
        if image_candidates or store_urls:
            logger.info("检测到表情包，准备偷走它！")

        # 在进入后台队列前截取上下文，之后到达的消息不会混进来。
        chat_context = self.build_chat_context(event)

        if await self._queue_background_capture(
            image_candidates,
            store_urls,
            to_pending=to_pending,
            origin_target=origin_target_str,
            chat_context=chat_context,
        ):
            return

        merged_index = await self._process_image_candidates(
            event,
            plugin_instance,
            image_candidates,
            to_pending=to_pending,
            chat_context=chat_context,
        )
        merged_index.update(
            await self._process_store_urls(
                event,
                plugin_instance,
                store_urls,
                to_pending=to_pending,
                origin_target=origin_target_str,
                chat_context=chat_context,
            )
        )
        if merged_index:
            await plugin_instance.index_manager.save_index(merged_index)

    async def _handle_force_capture(
        self,
        event: StealerEvent,
        plugin_instance,
        imgs: list,
        store_urls: list[str],
    ) -> None:
        """处理强制捕获模式：下载并处理单张图片后直接返回。"""
        try:
            temp_path: str | None = None
            is_gif = False

            if imgs:
                img = imgs[0]
                result = await self._image_download_service.download_original_image(img)
                # _download_original_image 固定返回二元组 (temp_path, is_gif)
                temp_path, is_gif = result
                if not temp_path:
                    temp_path = await img.convert_to_file_path()
            elif store_urls:
                result = await self._download_url_to_temp(store_urls[0])
                # _download_url_to_temp 固定返回二元组 (temp_path, is_gif)
                temp_path, is_gif = result

            if not temp_path or not Path(temp_path).exists():
                await event.send(MessageChain([Plain(text="❌ 收录失败：图片临时文件不存在")]))
            else:
                success, idx = await plugin_instance._process_image(
                    event,
                    temp_path,
                    is_temp=True,
                    is_platform_emoji=True,
                    chat_context=self.build_chat_context(event),
                )
                if success and isinstance(idx, dict):
                    await plugin_instance.index_manager.save_index(idx)
                    await event.send(MessageChain([Plain(text="✅ 已收录并自动分类入库")]))
                else:
                    await event.send(
                        MessageChain(
                            [
                                Plain(
                                    text="❌ 未收录（可能被判定为非表情包/审核不通过/重复或处理失败）"
                                )
                            ]
                        )
                    )
        except Exception as e:
            await event.send(MessageChain([Plain(text=f"❌ 收录失败：{e}")]))
        finally:
            try:
                plugin_instance.consume_force_capture(event)
            except Exception:
                pass

    async def _clean_raw_directory(self) -> int:
        """清理 raw 目录中的所有临时文件。"""
        deleted = 0
        try:
            raw_dir = getattr(self.plugin, "raw_dir", None)
            if raw_dir and raw_dir.exists():
                for f in raw_dir.iterdir():
                    try:
                        if f.is_file():
                            f.unlink()
                            deleted += 1
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"清理 raw 目录失败: {e}")
        if deleted:
            logger.debug(f"raw 目录清理: 删除了 {deleted} 个文件")
        return deleted

    async def cleanup_async(self) -> None:
        """异步清理资源。"""
        await self.stop_background_workers()
        await self._image_download_service.close()

    def cleanup(self):
        """清理资源。"""
        if self._cleaned:
            return
        self._cleaned = True
        # 清理强制捕获窗口
        if hasattr(self, "_force_capture_windows"):
            self._force_capture_windows.clear()
        # 清理插件引用
        self.plugin = None
        logger.debug("EventHandler 资源已清理")
