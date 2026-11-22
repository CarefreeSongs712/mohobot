"""PendingRoutes methods for PluginAPI."""

import asyncio
import hashlib
import json
import os
import shutil
import time
from typing import Any

from ..web.http import jsonify, request

from ..host import logger

from ..core.db.pending_repository import LOCAL_SCOPE_ORIGIN_ERROR
from ..core.util.blacklist import add_blacklist_hash
from ..core.db.index_manager import refresh_search_entry
from ..core.util.normalization import is_valid_origin_target, normalize_scope_mode
from ..core.util.safe_io import safe_remove_file

class PendingRoutes:
    # ── Pending (待审核池) ────────────────────────────────────

    def _build_pending_item(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """待审核记录与图库使用同一套展示字段。"""
        item = self._build_image_item(str(row.get("path") or ""), row)
        if item is not None:
            item.update(id=row.get("id"), review_status=str(row.get("review_status") or "pending"))
        return item

    async def handle_list_pending(self):
        """GET /pending —— 分页返回待审核列表（分类筛选/搜索/sort=newest）。"""
        try:
            db = self._db
            if not db or not hasattr(db, "get_pending_paginated"):
                return jsonify(
                    {
                        "success": True,
                        "images": [],
                        "total": 0,
                        "category_total": 0,
                        "categories": {},
                    }
                )

            page = request.args.get("page", 1, type=int)
            page_size = request.args.get("size", 50, type=int)
            category = request.args.get("category", None)
            search = str(request.args.get("q", "")).strip().lower()

            raw, total, cat_counts = db.get_pending_paginated(
                page=max(1, page),
                page_size=max(1, min(page_size, 200)),
                category=category if category else None,
                search_query=search if search else None,
            )
            images = [item for item in (self._build_pending_item(r) for r in raw) if item]
            return jsonify(
                {
                    "success": True,
                    "images": images,
                    "total": total,
                    "category_total": sum(int(count) for count in cat_counts.values()),
                    "categories": self._build_categories_list(cat_counts),
                }
            )
        except Exception as e:
            logger.error(f"列出待审核失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_pending_stats(self):
        """GET /pending/stats —— 供审核区顶部进度条。"""
        try:
            db = self._db
            pending = db.count_pending() if db and hasattr(db, "count_pending") else 0
            capacity = int(getattr(self.plugin, "steal_pool_capacity", 200) or 200)
            return jsonify(
                {
                    "success": True,
                    "stats": {
                        "pending": pending,
                        "capacity": capacity,
                        "paused": pending >= capacity,
                    },
                }
            )
        except Exception as e:
            logger.error(f"待审核统计失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def _resolve_pending_ids(self, data: dict[str, Any]) -> list[int]:
        """支持 {id} 或 {ids:[...]} 两种入参，返回去重后的 int id 列表。"""
        raw = data.get("ids")
        if raw is None:
            single = data.get("id")
            raw = [single] if single is not None else []
        if not isinstance(raw, list):
            raw = []
        return list({int(i) for i in raw if i is not None})

    # ── 审核事务辅助 ─────────────────────────────────────────

    def _approve_marker_dir(self) -> str:
        cache_dir = getattr(self._cfg, "cache_dir", None)
        return os.path.join(str(cache_dir), "pending_approve") if cache_dir else ""

    def _approve_marker_path(self, pending_id: int) -> str:
        marker_dir = self._approve_marker_dir()
        return os.path.join(marker_dir, f"{int(pending_id)}.json") if marker_dir else ""

    def _write_approve_marker(self, pending_id: int, src_path: str, cat_path: str) -> bool:
        """写恢复标记。文件移动与 DB 提交之间进程中断时靠它恢复/回滚。

        返回 False 时调用方不得继续移动文件/写库：没有标记就无法在崩溃
        后恢复，宁可不审批也不能制造不一致(issue #132 review)。
        """
        marker_path = self._approve_marker_path(pending_id)
        if not marker_path:
            logger.error(f"写审核恢复标记失败 id={pending_id}: 未配置 cache_dir")
            return False
        try:
            os.makedirs(os.path.dirname(marker_path), exist_ok=True)
            tmp_path = marker_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "pending_id": int(pending_id),
                        "src": src_path,
                        "cat": cat_path,
                        "created_at": int(time.time()),
                    },
                    handle,
                    ensure_ascii=False,
                )
            os.replace(tmp_path, marker_path)
        except Exception as exc:
            try:
                if os.path.isfile(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            logger.error(f"写审核恢复标记失败 id={pending_id}, 已中止本次审核: {exc}")
            return False
        return True

    def _read_approve_marker(self, pending_id: int) -> dict[str, Any] | None:
        marker_path = self._approve_marker_path(pending_id)
        if not marker_path or not os.path.isfile(marker_path):
            return None
        try:
            with open(marker_path, "r", encoding="utf-8") as handle:
                marker = json.load(handle)
        except Exception:
            return None
        return marker if isinstance(marker, dict) else None

    def _clear_approve_marker(self, pending_id: int) -> None:
        marker_path = self._approve_marker_path(pending_id)
        if not marker_path:
            return
        try:
            os.remove(marker_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"清理审核恢复标记失败 id={pending_id}: {exc}")

    @staticmethod
    async def _settle_future(future: asyncio.Future) -> None:
        """在取消状态下等子步骤真正结束，避免补偿动作本身被打断。"""
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break

    async def _run_step(self, coro: Any) -> tuple[Any, bool, BaseException | None]:
        """执行一个步骤并返回 (结果, 是否被取消, 异常)。

        与普通 await 不同：当前任务被取消时先把子步骤结清，拿到确定结果，
        调用方再决定回滚还是继续，避免 move/DB 写到一半被 Cancel 撕裂。
        """
        future = asyncio.ensure_future(coro)
        cancelled = False
        try:
            return await asyncio.shield(future), cancelled, None
        except asyncio.CancelledError:
            cancelled = True
        except BaseException as exc:
            return None, cancelled, exc
        await self._settle_future(future)
        if future.cancelled():
            return None, cancelled, asyncio.CancelledError()
        exc = future.exception()
        if exc is not None:
            return None, cancelled, exc
        return future.result(), cancelled, None

    async def _rollback_approval(
        self,
        db,
        pending_id: int,
        src_path: str,
        cat_path: str,
        *,
        inserted: bool,
        expected_hash: str = "",
    ) -> bool:
        """审核失败/取消后的补偿：删掉本次写入的图库记录，把文件移回 pending。

        只处理哈希与 pending 行一致的记录；哈希不匹配说明同名路径已被其他
        并发写入占用，此时文件与 DB 都保持原样，避免误删他人数据或让图库
        记录指向被移走的文件(issue #132 review)。返回 False 表示已跳过。
        """
        skipped = False
        if inserted:
            try:
                if hasattr(db, "delete_paths"):
                    current_hash = ""
                    if expected_hash and hasattr(db, "get_emoji"):
                        emoji = db.get_emoji(cat_path)
                        if emoji:
                            current_hash = str(emoji.get("hash", "") or "")
                    if not expected_hash or not current_hash or current_hash == expected_hash:
                        await db.delete_paths([cat_path])
                        await refresh_search_entry(self.plugin, cat_path)
                    else:
                        skipped = True
                        logger.warning(
                            f"审核回滚跳过 id={pending_id}: 图库记录哈希已变化, "
                            "不删除记录也不移动文件"
                        )
            except Exception as exc:
                logger.warning(f"审核回滚删除图库记录失败 id={pending_id}: {exc}")
        if skipped:
            # 记录已不属于本次操作: 清掉恢复标记, 但文件/DB 保持不变。
            self._clear_approve_marker(pending_id)
            return False
        try:
            if cat_path and os.path.isfile(cat_path) and not os.path.exists(src_path):
                parent = os.path.dirname(src_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                await asyncio.to_thread(shutil.move, cat_path, src_path)
        except Exception as exc:
            logger.warning(f"审核回滚移动文件失败 id={pending_id}: {exc}")
        self._clear_approve_marker(pending_id)
        return True

    def _find_orphan_by_hash(self, hash_val: str, basename: str) -> str:
        """文件已不在 pending 路径时，按内容哈希在分类目录里找回孤儿文件。"""
        if not hash_val or not basename:
            return ""
        categories_dir = getattr(self._cfg, "categories_dir", None)
        if not categories_dir or not os.path.isdir(str(categories_dir)):
            return ""
        try:
            entries = [entry for entry in os.scandir(str(categories_dir)) if entry.is_dir()]
        except Exception:
            return ""
        for entry in entries:
            candidate = os.path.join(entry.path, basename)
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() == str(hash_val):
                        return candidate
            except Exception:
                continue
        return ""

    async def _recover_interrupted_approve(self, db, pending_id: int) -> bool:
        """处理上次审核中断留下的恢复标记。返回 True 表示已补完提交。"""
        marker = self._read_approve_marker(pending_id)
        if not marker:
            return False
        src_path = str(marker.get("src") or "")
        cat_path = str(marker.get("cat") or "")
        row = db.get_pending(pending_id) if hasattr(db, "get_pending") else None
        if row is None:
            # pending 行已删，说明提交已完成，只需清理标记
            self._clear_approve_marker(pending_id)
            return False

        emoji = None
        if cat_path and hasattr(db, "get_emoji"):
            try:
                emoji = db.get_emoji(cat_path)
            except Exception:
                emoji = None
        if emoji is not None:
            # DB 已写入、pending 行未删：补齐提交
            try:
                if hasattr(db, "promote_source_pending_path") and src_path and cat_path:
                    await db.promote_source_pending_path(src_path, cat_path)
                db.delete_pending(pending_id)
                await refresh_search_entry(self.plugin, cat_path, emoji)
            except Exception as exc:
                logger.warning(f"恢复审核提交失败 id={pending_id}: {exc}")
                return False
            self._clear_approve_marker(pending_id)
            return True

        # DB 未提交：文件可能已被移到分类目录，移回 pending 后走正常审核
        if src_path and cat_path and os.path.isfile(cat_path) and not os.path.exists(src_path):
            try:
                parent = os.path.dirname(src_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                await asyncio.to_thread(shutil.move, cat_path, src_path)
            except Exception as exc:
                logger.warning(f"恢复审核文件失败 id={pending_id}: {exc}")
        self._clear_approve_marker(pending_id)
        return False

    async def _recover_interrupted_reject(self, db, pending_id: int) -> None:
        """拒绝前的补偿：上一次审核可能已把文件/记录写进图库，按拒绝语义清掉。"""
        marker = self._read_approve_marker(pending_id)
        if not marker:
            return
        cat_path = str(marker.get("cat") or "")
        row = db.get_pending(pending_id) if hasattr(db, "get_pending") else None
        if hasattr(db, "get_pending") and row is None:
            # pending 行已删除说明上次审核已提交，不能再按"拒绝"删除图库记录。
            logger.warning(f"拒绝恢复跳过 id={pending_id}: pending 行已不存在(审核已提交)")
            self._clear_approve_marker(pending_id)
            return
        expected_hash = str((row or {}).get("hash", "") or "")
        if cat_path and os.path.isfile(cat_path):
            current_hash = ""
            if hasattr(db, "get_emoji"):
                try:
                    emoji = db.get_emoji(cat_path)
                except Exception:
                    emoji = None
                if emoji:
                    current_hash = str(emoji.get("hash", "") or "")
            if expected_hash and current_hash and current_hash != expected_hash:
                logger.error(
                    f"拒绝恢复跳过 id={pending_id}: 图库记录哈希不匹配, 不删除他人数据"
                )
                self._clear_approve_marker(pending_id)
                return
            try:
                if hasattr(db, "delete_paths"):
                    await db.delete_paths([cat_path])
                await refresh_search_entry(self.plugin, cat_path)
            except Exception as exc:
                logger.warning(f"拒绝恢复删除图库记录失败 id={pending_id}: {exc}")
            try:
                await safe_remove_file(cat_path)
            except Exception as exc:
                logger.warning(f"拒绝恢复删除文件失败 id={pending_id}: {exc}")
        self._clear_approve_marker(pending_id)

    async def _approve_one(self, db, pending_id: int) -> tuple[bool, str]:
        """审核通过单条：pending 文件 → categories/，写 emoji+tag+scene，删 pending 行。

        事务化语义（issue #132）：
        - 移动前写恢复标记，进程中断后下次审核/拒绝可据此补齐或回滚；
        - move / insert 被取消时先结清子步骤，再按是否已提交补偿；
        - 提交点 = 删除 pending 行；提交后 promote/索引失败只告警，不再回滚；
        - 空文件按内容哈希找回孤儿文件，找不到才清理 pending 行。
        """
        if await self._recover_interrupted_approve(db, pending_id):
            return True, ""

        row = db.get_pending(pending_id)
        if not row:
            return False, "pending not found"

        src_path = str(row.get("path", "") or "")
        if not src_path or not os.path.isfile(src_path):
            orphan = self._find_orphan_by_hash(
                str(row.get("hash", "") or ""), os.path.basename(src_path)
            )
            if orphan:
                try:
                    parent = os.path.dirname(src_path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    await asyncio.to_thread(shutil.move, orphan, src_path)
                    logger.warning(f"审核通过从分类目录找回孤儿文件 id={pending_id}: {orphan}")
                except Exception as exc:
                    logger.warning(f"找回孤儿文件失败 id={pending_id}: {exc}")
        if not src_path or not os.path.isfile(src_path):
            # 文件已丢失且无法找回：清理孤儿 pending 行
            db.delete_pending(pending_id)
            self._clear_approve_marker(pending_id)
            return False, "pending file missing"

        scope_mode = normalize_scope_mode(row.get("scope_mode"))
        origin_target = str(row.get("origin_target", "") or "").strip()
        if scope_mode == "local" and not is_valid_origin_target(origin_target):
            return False, LOCAL_SCOPE_ORIGIN_ERROR

        category = str(row.get("category", "") or "").strip()
        known = set(self._cfg.get_vlm_categories() if hasattr(self._cfg, "get_vlm_categories") else (self._cfg.categories or []))
        if not category or category == "other" or category not in known:
            category = self._cfg.closest_category(category) if hasattr(self._cfg, "closest_category") else "confused"

        cat_dir = self._cfg.ensure_category_dir(category)
        cat_path = str(cat_dir / os.path.basename(src_path))
        if os.path.abspath(src_path) != os.path.abspath(cat_path) and os.path.exists(cat_path):
            return False, "目标分类目录已存在同名文件"

        inserted = False
        committed = False
        cancelled_any = False
        if not self._write_approve_marker(pending_id, src_path, cat_path):
            return False, "无法写入审核恢复标记，本次审核未做任何修改"
        try:
            if os.path.abspath(src_path) != os.path.abspath(cat_path):
                _, cancelled, error = await self._run_step(
                    asyncio.to_thread(shutil.move, src_path, cat_path)
                )
                cancelled_any = cancelled_any or cancelled
                if error is not None:
                    raise error
                if cancelled:
                    raise asyncio.CancelledError()

            emoji_entry: dict[str, Any] = {
                "path": cat_path,
                "hash": str(row.get("hash", "") or ""),
                "phash": row.get("phash") or "",
                "category": category,
                "desc": str(row.get("desc", "") or ""),
                "source": str(row.get("source", "") or ""),
                "origin_target": str(row.get("origin_target", "") or ""),
                "scope_mode": scope_mode or "public",
                "created_at": int(time.time()),
                "use_count": 0,
                "last_used_at": 0,
                "reviewed_at": int(time.time()),
                "tags": list(row.get("tags", []) or []),
                "scenes": list(row.get("scenes", []) or []),
                "overlay_text": str(row.get("overlay_text", "") or ""),
                "emotions": list(row.get("emotions", []) or []),
                "character": str(row.get("character", "") or ""),
                # v5：从 pending 继承元数据（宽高/格式/字节/来源/入库方式）
                "source_url": row.get("source_url"),
                "original_name": row.get("original_name"),
                "width": row.get("width"),
                "height": row.get("height"),
                "format": row.get("format"),
                "bytes": row.get("bytes"),
                "add_method": row.get("add_method"),
                "retention_class": str(row.get("retention_class", "native") or "native"),
                # REQ-3: 适用 bot 白名单(空串 = 全部 bot 可用)
                "allowed_bots": str(row.get("allowed_bots", "") or ""),
            }
            result, cancelled, error = await self._run_step(db.insert_batch([emoji_entry]))
            cancelled_any = cancelled_any or cancelled
            if error is not None:
                raise error
            if not result:
                raise RuntimeError("insert emoji returned 0")
            inserted = True
            if cancelled:
                raise asyncio.CancelledError()

            # 提交点：删除 pending 行；此后文件与图库记录已一致
            db.delete_pending(pending_id)
            committed = True
            self._clear_approve_marker(pending_id)

            if hasattr(db, "promote_source_pending_path"):
                _, cancelled, error = await self._run_step(
                    db.promote_source_pending_path(src_path, cat_path)
                )
                cancelled_any = cancelled_any or cancelled
                if error is not None:
                    logger.warning(f"审核通过同步来源记录失败 id={pending_id}: {error}")
            _, cancelled, error = await self._run_step(
                refresh_search_entry(self.plugin, cat_path, emoji_entry)
            )
            cancelled_any = cancelled_any or cancelled
            if error is not None:
                logger.warning(f"审核通过刷新检索索引失败 id={pending_id}: {error}")

            if cancelled_any:
                raise asyncio.CancelledError()
            return True, ""
        except asyncio.CancelledError:
            if not committed:
                await self._run_step(
                    self._rollback_approval(
                        db, pending_id, src_path, cat_path, inserted=inserted,
                        expected_hash=str(row.get("hash", "") or ""),
                    )
                )
            raise
        except Exception as e:
            if not committed:
                await self._run_step(
                    self._rollback_approval(
                        db, pending_id, src_path, cat_path, inserted=inserted,
                        expected_hash=str(row.get("hash", "") or ""),
                    )
                )
            logger.error(f"审核通过失败 id={pending_id}: {e}", exc_info=True)
            return False, str(e)

    async def handle_pending_approve(self):
        """POST /pending/approve —— 批量通过 {id} 或 {ids:[]}。"""
        try:
            data = await request.get_json() or {}
            ids = await self._resolve_pending_ids(data)
            if not ids:
                return jsonify({"success": False, "error": "缺少 id/ids"})

            db = self._db
            if not db:
                return jsonify({"success": False, "error": "db 不可用"})

            approved = 0
            errors: list[str] = []
            for pending_id in ids:
                ok, msg = await self._approve_one(db, pending_id)
                if ok:
                    approved += 1
                elif msg and msg not in ("pending not found",):
                    errors.append(f"id={pending_id}: {msg}")

            return jsonify(
                {
                    "success": approved > 0,
                    "approved": approved,
                    "errors": errors,
                }
            )
        except Exception as e:
            logger.error(f"审核通过失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_pending_reject(self):
        """POST /pending/reject —— 批量拒绝 {id} 或 {ids:[...], blacklist?:bool}。"""
        try:
            data = await request.get_json() or {}
            ids = await self._resolve_pending_ids(data)
            if not ids:
                return jsonify({"success": False, "error": "缺少 id/ids"})
            blacklist = bool(data.get("blacklist", False))

            db = self._db
            if not db:
                return jsonify({"success": False, "error": "db 不可用"})

            # 拒绝前先处理上次审核中断的恢复标记，避免残留图库记录/分类文件
            rows: list[dict[str, Any]] = []
            for pending_id in ids:
                if hasattr(db, "get_pending"):
                    row = db.get_pending(pending_id)
                    if row:
                        rows.append(row)
                await self._recover_interrupted_reject(db, pending_id)

            removed_rows = db.delete_pending_batch(ids)
            if not rows:
                rows = removed_rows
            deleted = 0
            blacklisted = 0
            for r in rows:
                p = str(r.get("path", "") or "")
                h = str(r.get("hash", "") or "")
                if p:
                    try:
                        if await safe_remove_file(p):
                            deleted += 1
                    except Exception as e:
                        logger.warning(f"拒绝时删除文件失败 {p}: {e}")
                if blacklist and h:
                    if await add_blacklist_hash(self.plugin, h):
                        blacklisted += 1
                    else:
                        logger.warning(f"拉黑失败 {h}")
            return jsonify(
                {"success": True, "deleted": deleted, "blacklisted": blacklisted}
            )
        except Exception as e:
            logger.error(f"审核拒绝失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_pending_update(self):
        """POST /pending/update —— 修改一条 pending 的元数据（分类/描述/标签/场景/作用域）。"""
        try:
            data = await request.get_json() or {}
            try:
                pending_id = int(data.get("id") or 0)
            except (TypeError, ValueError):
                pending_id = 0
            if pending_id <= 0:
                return jsonify({"success": False, "error": "缺少 id"})

            db = self._db
            if not db or not hasattr(db, "update_pending"):
                return jsonify({"success": False, "error": "db 不可用"})

            row = db.get_pending(pending_id) if hasattr(db, "get_pending") else None
            if hasattr(db, "get_pending") and row is None:
                return jsonify({"success": False, "error": "pending 不存在"})

            # 字段白名单 + 类型归一化（与 update_pending 内部白名单一致）
            fields: dict[str, Any] = {}
            if "category" in data:
                category = str(data.get("category") or "").strip()
                if category and category in (self._cfg.categories or []):
                    fields["category"] = category
                elif category:
                    return jsonify(
                        {"success": False, "error": f"分类无效: {category!r}"}
                    )
            if "desc" in data:
                fields["desc"] = str(data.get("desc") or "").strip()
            if "scope_mode" in data:
                fields["scope_mode"] = str(data.get("scope_mode") or "public").strip()
            if "tags" in data:
                tags_raw = data.get("tags")
                if isinstance(tags_raw, list):
                    fields["tags"] = [
                        str(t).strip() for t in tags_raw if str(t or "").strip()
                    ]
                else:
                    fields["tags"] = [
                        t.strip()
                        for t in str(tags_raw or "").split(",")
                        if t.strip()
                    ]
            if "scenes" in data:
                scenes_raw = data.get("scenes")
                if isinstance(scenes_raw, list):
                    fields["scenes"] = [
                        str(s).strip() for s in scenes_raw if str(s or "").strip()
                    ]
                else:
                    fields["scenes"] = [
                        s.strip()
                        for s in str(scenes_raw or "").split(",")
                        if s.strip()
                    ]
            if "character" in data:
                character = self._normalize_character_key(str(data.get("character") or ""))
                if character and character not in set(self._cfg.get_characters()):
                    return jsonify({"success": False, "error": f"角色无效: {character}"})
                fields["character"] = character

            if not fields:
                return jsonify({"success": False, "error": "没有可更新字段"})

            # local 作用域必须有有效来源（issue #132）：无来源的 local 会对无关群可见
            if row is not None:
                effective_scope = normalize_scope_mode(
                    fields.get("scope_mode", row.get("scope_mode"))
                )
                if effective_scope == "local" and not is_valid_origin_target(
                    row.get("origin_target")
                ):
                    return jsonify({"success": False, "error": LOCAL_SCOPE_ORIGIN_ERROR})

            updated = await db.update_pending(pending_id, fields)
            if not updated:
                return jsonify(
                    {"success": False, "error": "pending 不存在"}
                )

            return jsonify({"success": True, "item": self._build_pending_item(updated)})
        except Exception as e:
            logger.error(f"待审核更新失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})
