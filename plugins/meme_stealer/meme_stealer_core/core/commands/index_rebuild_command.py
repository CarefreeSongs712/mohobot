"""索引重建命令：负责从文件重建索引并合并旧元数据。"""

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

from ...host import logger

from ...host import StealerEvent

from ..db.index_metadata_merge import has_meaningful_metadata, restore_rebuilt_metadata
from ..db.index_manager import _file_sha256, _settle_index_step, refresh_search_entry
from ..maintenance.retention import library_counts


class IndexRebuildCommand:
    """负责索引重建、元数据合并和迁移。"""

    def __init__(self, plugin_instance: Any) -> None:
        self.plugin = plugin_instance

    async def _run_rebuild_write(self, path: str, operation) -> bool:
        """Settle storage work under the gallery gate, then refresh outside it."""
        async with self.plugin.db_service._write_lock:
            changed, cancelled, error = await _settle_index_step(operation())
        if changed:
            _, refresh_cancelled, refresh_error = await _settle_index_step(
                refresh_search_entry(self.plugin, path)
            )
            cancelled = cancelled or refresh_cancelled
            if refresh_error is not None:
                logger.warning(f"重建索引检索刷新失败 [{path}]: {refresh_error}")
        if cancelled:
            raise asyncio.CancelledError()
        if error is not None:
            raise error
        return bool(changed)

    async def _persist_rebuilt_index(self, rebuilt_index: dict, old_index: dict) -> dict:
        """Apply verified scan results only while their original owner is unchanged."""
        db = self.plugin.db_service
        persisted = {}
        for path, metadata in rebuilt_index.items():
            if not isinstance(metadata, dict):
                continue
            expected_hash = str(metadata.get("hash") or "")
            if len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
                continue

            async def save_locked():
                # Include access-control and retention metadata, not just hash:
                # a user may have edited the same image while the scan ran.
                if db.get_emoji(path) != old_index.get(path):
                    return False
                if not Path(path).is_file():
                    return False
                if await asyncio.to_thread(_file_sha256, path) != expected_hash:
                    return False
                await db.sync_index({path: metadata}, _writer_locked=True)
                return True

            if await self._run_rebuild_write(path, save_locked):
                persisted[path] = metadata
        return persisted

    async def _prune_missing_records(self, old_index: dict) -> None:
        """Never prune a replacement owner or a file that appeared after scanning."""
        db = self.plugin.db_service
        for path, metadata in old_index.items():
            if not isinstance(metadata, dict) or not Path(path).is_absolute():
                continue
            expected_hash = str(metadata.get("hash") or "")
            if len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
                continue

            async def delete_locked():
                if db.get_emoji(path) != metadata or Path(path).exists():
                    return False
                return bool(await db.delete_paths(
                    [path], expected_hash=expected_hash, _writer_locked=True,
                ))

            await self._run_rebuild_write(path, delete_locked)

    async def rebuild_index(self, event: StealerEvent):
        """重建索引命令，用于从旧版本迁移或修复索引。

        扫描 categories 目录中的所有图片文件，重新构建索引。
        """
        try:
            yield event.plain_result("🔄 开始重建索引，请稍候...")

            # Capture owners before scanning so later writes cannot inherit a
            # newly approved image's path or overwrite concurrent policy edits.
            old_index = self.plugin.db_service.get_index_cache_readonly()
            logger.info(f"[rebuild_index] 从数据库加载 {len(old_index)} 条旧记录")

            # 调用插件的重建索引方法（只重建基础索引，不保存到数据库）
            rebuilt_index = await self.plugin.index_manager.rebuild_index_from_files()

            if not rebuilt_index:
                yield event.plain_result(
                    "⚠️ 未找到可重建的图片文件。\n"
                    f"请确保 categories 目录中存在图片文件:\n"
                    f"{self.plugin.plugin_config.categories_dir}"
                )
                return

            # --- 收集所有旧数据源（JSON + 数据库）---
            # 2. 尝试加载所有可能的旧版 JSON 文件（不依赖 _load_index 的迁移逻辑）
            legacy_metadata_count = 0
            legacy_data_map = {}
            possible_legacy_paths = []

            # 添加所有可能的 JSON 文件路径（包括迁移后的备份文件）
            if self.plugin.cache_dir:
                possible_legacy_paths.extend(
                    [
                        self.plugin.cache_dir / "index_cache.json",
                        self.plugin.cache_dir / "index_cache.json.backup",
                        self.plugin.cache_dir / "index_cache.json.migrated",  # 迁移后的备份
                    ]
                )
            if self.plugin.base_dir:
                possible_legacy_paths.extend(
                    [
                        self.plugin.base_dir / "index.json",
                        self.plugin.base_dir / "index.json.backup",
                        self.plugin.base_dir / "index.json.migrated",  # 迁移后的备份
                        self.plugin.base_dir / "image_index.json",
                        self.plugin.base_dir / "image_index.json.backup",
                        self.plugin.base_dir / "image_index.json.migrated",
                        self.plugin.base_dir / "cache" / "index.json",
                        self.plugin.base_dir / "cache" / "index.json.backup",
                        self.plugin.base_dir / "cache" / "index.json.migrated",
                        self.plugin.base_dir / "cache" / "index_cache.json",
                        self.plugin.base_dir / "cache" / "index_cache.json.backup",
                        self.plugin.base_dir / "cache" / "index_cache.json.migrated",
                    ]
                )

            for legacy_path in possible_legacy_paths:
                if legacy_path.exists():
                    try:
                        with open(legacy_path, encoding="utf-8") as f:
                            legacy_data = json.load(f)
                            if isinstance(legacy_data, dict) and legacy_data:
                                legacy_data_map.update(legacy_data)
                                legacy_metadata_count += len(legacy_data)
                                logger.info(
                                    f"[rebuild_index] 从 JSON 加载 {len(legacy_data)} 条: {legacy_path}"
                                )
                    except Exception as e:
                        logger.warning(f"[rebuild_index] 加载 JSON 失败 {legacy_path}: {e}")

            old_count = len(old_index) + len(legacy_data_map)
            restore_rebuilt_metadata(rebuilt_index, old_index, legacy_data_map)

            # Persist each verified owner before eviction; the scan dictionary
            # must never be saved wholesale after a concurrent gallery write.
            final_index = await self._persist_rebuilt_index(rebuilt_index, old_index)
            await self._prune_missing_records(old_index)

            # 重建后若超过容量限制，先执行容量控制清理
            max_reg = getattr(self.plugin.plugin_config, "max_reg_num", getattr(self.plugin, "max_reg_num", 0))
            current_index = self.plugin.db_service.get_index_cache_readonly()
            if max_reg > 0 and library_counts(current_index)["automatic"] > max_reg:
                logger.info(
                    f"[rebuild_index] 重建后数量 {len(current_index)} 超过限制 {max_reg}，"
                    f"执行容量控制清理"
                )
                await self.plugin.event_handler._enforce_capacity(current_index)

            # 增量同步还会保留扫描目录外的有效记录，统计以数据库为准。
            persisted_index = self.plugin.db_service.get_index_cache_readonly()
            new_count = len(persisted_index)
            recovered_count = sum(
                1 for meta in final_index.values()
                if isinstance(meta, dict) and has_meaningful_metadata(meta)
            )

            # 按分类统计
            category_stats = Counter(
                img_info.get("category", "未分类")
                for img_info in persisted_index.values()
                if isinstance(img_info, dict)
            )

            # 构建结果消息
            result_msg = "✅ 索引重建完成！\n\n"
            result_msg += "📊 统计信息:\n"
            result_msg += f"  重建前旧数据: {old_count} 条\n"
            if legacy_metadata_count > 0:
                result_msg += f"  旧版备份数据: {legacy_metadata_count} 条\n"
            result_msg += f"  重建后索引/文件: {new_count} 个\n"
            result_msg += f"  已恢复元数据: {recovered_count} 条\n"

            if category_stats:
                result_msg += "\n📂 分类统计:\n"
                for cat, count in sorted(category_stats.items(), key=lambda x: x[1], reverse=True):
                    result_msg += f"  {cat}: {count}张\n"

            yield event.plain_result(result_msg)

        except Exception as e:
            logger.error(f"重建索引失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 重建索引失败: {str(e)}")
