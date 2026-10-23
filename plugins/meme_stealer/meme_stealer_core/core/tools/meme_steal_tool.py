"""LLM meme image capture workflows used by the registered entrypoints."""

import asyncio
import os
from pathlib import Path
from typing import Any

from ...host import Image as MessageImage
from ...host import StealerEvent, logger
from ...host.components import is_inline_image

from ..events.event_context import unwrap_event
from ..events.image_download_service import ImageDownloadService
from ..util.normalization import canonicalize_path
from ..util.safe_io import safe_remove_file

class MemeStealToolWorkflow:
    async def _steal_sticker_impl(
        self,
        event: StealerEvent,
        image_ref: str,
        bots: str = "",
    ):
        """偷取图片入库。VLM 视觉模型会自动分析图片，打上分类、标签、描述和场景。

        使用时机：
        - 用户说"偷一下"/"收了这张图"时直接调用本工具。
        - 你看到当前消息里有适合作为表情包的图片时，也可以调用本工具补充素材库。

        注意：
        - image_ref 必须从当前消息中已有的图片 URL 或文件路径中选择，必填。
        - 不需要自己打标，工具会交给 VLM 自动完成分类、标签、描述和场景分析。
        - 工具返回的 VLM 分析结果可用于向用户说明偷到了什么。
        - 当插件的表情包偷取总开关关闭，或当前会话被偷取黑白名单禁用时，本工具会拒绝入库。

        Args:
            image_ref(string): 图片 URL 或文件路径，从当前消息已有的 Image URL 中选择。
            bots(string): 可选。适用 bot 白名单(逗号分隔); 留空 = 全部 bot 通用。
        """
        event = unwrap_event(event)
        try:
            if not self.plugin_config.steal_meme:
                yield "偷取失败：表情包偷取功能未开启，请先在插件配置中启用"
                return

            if not self.is_steal_enabled_for_event(event):
                yield "偷取失败：当前群聊已禁用偷取功能"
                return

            event_handler = self._get_event_handler(log_message="event_handler 未初始化，无法下载图片")
            if event_handler is None:
                yield "偷取失败：内部服务未初始化"
                return

            image_ref, source = await self._resolve_steal_image_ref(
                event, image_ref, event_handler
            )
            if not image_ref:
                yield "偷取失败：当前消息和引用的消息里都没有图片"
                return

            logger.info(f"[Tool] LLM 请求偷取: ref={image_ref[:80]}")

            # 下载图片
            if is_inline_image(image_ref):
                temp_path, _is_gif = await asyncio.to_thread(
                    ImageDownloadService.write_inline_to_temp, image_ref
                )
                if not temp_path:
                    yield "偷取失败：图片数据无法解码"
                    return
                is_temp = True
            elif image_ref.startswith("http://") or image_ref.startswith("https://"):
                temp_path, _is_gif = await event_handler._download_to_temp(image_ref, log_download=True)
                if not temp_path or not os.path.exists(temp_path):
                    yield f"偷取失败：无法下载图片 {image_ref[:100]}"
                    return
                is_temp = True
            elif image_ref.startswith("file:///"):
                local_path = image_ref[8:]
                if len(local_path) > 2 and local_path[0] == "/" and local_path[2] == ":":
                    local_path = local_path[1:]
                temp_path = os.path.abspath(local_path)
                is_temp = False
            else:
                temp_path = os.path.abspath(image_ref)
                is_temp = False

            if not os.path.exists(temp_path):
                hint = ""
                # 当 LLM 传来的是非 URL 形式（相对路径/裸文件名）且仍无法定位时，
                # 提示它从消息中已有的 Image URL 选择（issue #88）。
                ref_value = str(image_ref or "").strip()
                if ref_value and not (
                    ref_value.startswith("http://")
                    or ref_value.startswith("https://")
                    or ref_value.startswith("file:")
                ):
                    hint = "（请确认 image_ref 是当前消息中的图片 URL 或本地绝对路径）"
                yield f"偷取失败：图片文件不存在: {temp_path}{hint}"
                return

            precheck_ok, precheck_reason = self._precheck_image_file(temp_path)
            if not precheck_ok:
                if is_temp:
                    await safe_remove_file(temp_path)
                yield f"偷取失败：{precheck_reason}"
                return

            # 记下入库存前已有的路径，之后 diff 找出 VLM 分析结果
            idx_before = await self.index_manager.load_index()
            before_paths = set(idx_before.keys()) if idx_before else set()

            # 统一走 VLM 流水线
            # tool_capture_requires_review(默认 True): 模型标注完成后只进待审池,
            # 由 WebUI 管理员通过后才真正入库(不进索引/不参与检索发送)。
            cfg = getattr(self, "plugin_config", None)
            to_pending = bool(getattr(cfg, "tool_capture_requires_review", True))
            logger.info(
                f"[Tool] VLM 分析{'入待审池' if to_pending else '入库'}: {temp_path}"
            )
            extra_meta = self._build_steal_tool_extra_meta(
                event, image_ref, source=source
            )
            # REQ-3: 标注适用 bot(缺省空 = 全部 bot 可用)
            from ..util.bot_scope import format_allowed_bots

            allowed_bots = format_allowed_bots(bots)
            if allowed_bots:
                extra_meta["allowed_bots"] = allowed_bots
            success, merged_idx = await self._process_image(
                event,
                temp_path,
                is_temp=is_temp,
                extra_meta=extra_meta,
                to_pending=to_pending,
                chat_context=event_handler.build_chat_context(event),
            )

            if not success:
                yield "偷取失败：VLM 分析未通过（可能已存在、内容不合适或无法识别为表情包）"
                return

            if to_pending:
                # 待审路径: 标注结果照常回给模型(便于向用户说明), 但不入库、
                # 不进索引, 也不能声称"已入库"。
                detail = self._format_steal_tool_result(merged_idx, before_paths)
                suffix = "图片已提交管理员审核，通过后会加入表情包库"
                if detail:
                    yield "偷取成功！VLM 分析结果：\n" + detail + "\n- 状态：" + suffix
                else:
                    yield "偷取成功！" + suffix
                return

            if merged_idx:
                await self.index_manager.save_index(merged_idx)
                new_paths = set(merged_idx.keys()) - before_paths
                if new_paths:
                    new_entry = next((merged_idx[p] for p in new_paths if isinstance(merged_idx.get(p), dict)), None)
                    if new_entry and isinstance(new_entry, dict):
                        overlay = str(new_entry.get("overlay_text", "") or "")
                        tag_list = new_entry.get("tags", [])
                        tags_str = ", ".join(tag_list) if isinstance(tag_list, list) else str(tag_list)
                        desc_text = new_entry.get("desc", "")
                        scene_list = new_entry.get("scenes", [])
                        scenes_str = ", ".join(scene_list) if isinstance(scene_list, list) else str(scene_list)
                        yield (
                            f"偷取成功！VLM 分析结果：\n"
                            f"- 图上文字：{overlay or '无'}\n"
                            f"- 标签：{tags_str or '无'}\n"
                            f"- 描述：{desc_text or '无'}\n"
                            f"- 场景：{scenes_str or '无'}"
                        )
                        return
                yield "偷取成功！已通过 VLM 自动分析并入库"
            else:
                yield "偷取成功但索引更新失败"

        except Exception as e:
            logger.error(f"[Tool] 偷取表情包失败: {e}", exc_info=True)
            yield f"偷取出错：{e}"
            return

    @staticmethod
    def _format_steal_tool_result(
        merged_idx: dict[str, Any] | None, before_paths: set[str],
    ) -> str:
        """把新条目的 VLM 标注格式化为给模型看的多行摘要; 无新条目返回空串。"""
        if not isinstance(merged_idx, dict):
            return ""
        new_paths = set(merged_idx.keys()) - before_paths
        new_entry = next(
            (merged_idx[p] for p in new_paths if isinstance(merged_idx.get(p), dict)), None,
        )
        if not isinstance(new_entry, dict):
            return ""
        overlay = str(new_entry.get("overlay_text", "") or "")
        tag_list = new_entry.get("tags", [])
        tags_str = ", ".join(tag_list) if isinstance(tag_list, list) else str(tag_list)
        desc_text = new_entry.get("desc", "")
        scene_list = new_entry.get("scenes", [])
        scenes_str = ", ".join(scene_list) if isinstance(scene_list, list) else str(scene_list)
        return (
            f"- 图上文字：{overlay or '无'}\n"
            f"- 标签：{tags_str or '无'}\n"
            f"- 描述：{desc_text or '无'}\n"
            f"- 场景：{scenes_str or '无'}"
        )

    async def _resolve_steal_image_ref(
        self,
        event: StealerEvent,
        image_ref: str,
        event_handler: Any,
    ) -> tuple[str, str]:
        """Resolve an explicit or current-message image reference for steal_sticker.

        修复 issue #88：LLM 偶尔会传相对路径（如 ``./image.png``）或仅文件名。
        之前的实现直接把 ``explicit_ref`` 原样返回，下游 ``os.path.abspath``
        会拼到 CWD（如 ``/AstrBot/image.png``），触发"图片文件不存在"。

        现在对非 URL 形式的 ``image_ref``，优先在当前消息的 ``Image`` 组件中
        按 basename 匹配，再回退到组件自带的 url/file/path/convert_to_file_path。
        """
        explicit_ref = str(image_ref or "").strip()

        # 先收集"本事件内合法图片来源"(当前消息 + 引用消息 + QQ商城表情)。
        # issue #6 第 2 点: 旧实现在显式 ref 是 http/https/file 时直接信任,
        # 模型可以传任意网络 URL 或本机绝对路径把无关文件送进 VLM/图库;
        # 修复后显式 ref 必须能在这些合法来源里命中, 否则拒绝。
        candidates: list[tuple[str, str]] = []  # (ref, source)
        try:
            images = [comp for comp in event.get_messages() if isinstance(comp, MessageImage)]
        except Exception:
            images = []
        if not images:
            get_quoted = getattr(event, "get_quoted_images", None)
            if callable(get_quoted):
                try:
                    images = list(await get_quoted() or [])
                except Exception as e:
                    logger.debug(f"[Tool] 读取引用消息图片失败: {e}")
        for comp in images:
            try:
                ref = await event_handler._resolve_media_ref(comp)
            except Exception:
                ref = ""
            if ref:
                candidates.append((str(ref).strip(), "llm_tool"))

        # QQ 商城表情 URL 也是合法来源
        try:
            store_urls = event_handler._extract_store_emoji_urls(event)
        except Exception:
            store_urls = []
        for url in store_urls:
            url = str(url or "").strip()
            if url:
                candidates.append((url, "qq_store"))

        if explicit_ref:
            matched = self._match_candidate_ref(explicit_ref, candidates)
            if matched is not None:
                ref, source = matched
                return ref, source
            # 显式 ref 不来自当前上下文: 退回「消息解析」再试一次(相对路径/
            # 仅文件名等), 仍不命中则拒绝, 不再原样放行。
            resolved_explicit = await self._resolve_image_ref_against_event(
                event, explicit_ref
            )
            if resolved_explicit:
                return resolved_explicit, "llm_tool"
            logger.warning(
                f"[Tool] 拒绝越界 image_ref(只能是当前/引用消息里的图片): {explicit_ref[:120]}"
            )
            return "", "rejected"

        # 未提供 ref: 取本事件第一张合法图片(含引用消息/商城表情)
        if candidates:
            return candidates[0]

        return "", "llm_tool"

    @staticmethod
    def _match_candidate_ref(
        explicit_ref: str, candidates: list[tuple[str, str]],
    ) -> tuple[str, str] | None:
        """显式 ref 必须命中本事件的合法图片候选(按完整 ref / basename)。

        返回 ``(ref, source)`` 或 None。basename 匹配用于模型把 URL 简写成
        文件名、或传了消息图片的本地临时路径的场景。
        """
        ref = str(explicit_ref or "").strip()
        if not ref or not candidates:
            return None
        ref_norm = ref.replace("\\", "/")
        ref_clean = ref_norm.split("?", 1)[0].split("#", 1)[0]
        ref_base = ref_clean.rsplit("/", 1)[-1]
        for cand_ref, source in candidates:
            cand = str(cand_ref or "").strip()
            if not cand:
                continue
            cand_norm = cand.replace("\\", "/")
            cand_clean = cand_norm.split("?", 1)[0].split("#", 1)[0]
            if ref_clean == cand_clean or ref_norm == cand_norm:
                return cand, source
            if ref_base and ref_base == cand_clean.rsplit("/", 1)[-1]:
                return cand, source
        return None

    async def _resolve_image_ref_against_event(
        self,
        event: StealerEvent,
        image_ref: str,
    ) -> str:
        """在当前消息的 Image 组件中按 basename / 绝对路径 / 已有本地路径匹配。

        返回：
            - 命中组件时：组件真实的 url/file/path，或 ``convert_to_file_path()`` 结果；
            - 未命中或异常时：空串。
        """
        ref_norm = image_ref.replace("\\", "/").strip()
        ref_basename = Path(ref_norm).name if ref_norm else ""

        try:
            comps = list(event.get_messages())
        except Exception:
            return ""

        for comp in comps:
            if not isinstance(comp, MessageImage):
                continue

            # a. basename 命中组件的 url/file/path
            if ref_basename:
                for attr in ("url", "file", "path"):
                    value = str(getattr(comp, attr, "") or "").strip()
                    if not value:
                        continue
                    value_norm = value.replace("\\", "/").split("?", 1)[0].split("#", 1)[0]
                    value_basename = value_norm.rsplit("/", 1)[-1]
                    if value_basename and value_basename == ref_basename:
                        return value

            # b. ref 是已存在的绝对路径且等于组件本地路径
            if os.path.isabs(image_ref):
                for attr in ("file", "path"):
                    value = str(getattr(comp, attr, "") or "").strip()
                    if value and canonicalize_path(value) == canonicalize_path(image_ref):
                        return value

            # c. 调用组件自身方法把图片落到本地，返回真实可读路径
            if hasattr(comp, "convert_to_file_path"):
                try:
                    path = await comp.convert_to_file_path()
                    path = str(path or "").strip()
                except Exception:
                    path = ""
                if not path:
                    continue
                path_norm = path.replace("\\", "/")
                path_basename = path_norm.rsplit("/", 1)[-1].split("?")[0]
                if ref_basename and path_basename == ref_basename:
                    return path
                if os.path.isabs(image_ref) and canonicalize_path(path) == canonicalize_path(
                    image_ref
                ):
                    return path

        return ""

    def _build_steal_tool_extra_meta(
        self,
        event: StealerEvent,
        image_ref: str,
        *,
        source: str = "llm_tool",
    ) -> dict[str, Any] | None:
        extra_meta: dict[str, Any] = {}
        try:
            scope, target_id = self.get_event_target(event)
        except Exception:
            scope, target_id = "", ""
        if scope and target_id:
            extra_meta["origin_target"] = f"{scope}:{target_id}"

        if image_ref.startswith("http://") or image_ref.startswith("https://"):
            extra_meta["origin_url"] = image_ref
        if source:
            extra_meta["source"] = source
        return extra_meta or None

    async def _process_image(
        self,
        event: StealerEvent | None,
        file_path: str,
        is_temp: bool = False,
        idx: dict[str, Any] | None = None,
        is_platform_emoji: bool = False,
        extra_meta: dict[str, Any] | None = None,
        to_pending: bool = False,
        chat_context: str = "",
    ) -> tuple[bool, dict[str, Any] | None]:
        """统一处理图片的方法，包括审核、标注、存储和索引更新。"""
        try:
            success, updated_idx = await asyncio.wait_for(
                self.image_processor_service.process_image(
                    event=event,
                    file_path=file_path,
                    is_temp=is_temp,
                    idx=idx,
                    is_platform_emoji=is_platform_emoji,
                    extra_meta=extra_meta,
                    to_pending=to_pending,
                    chat_context=chat_context,
                ),
                timeout=self.IMAGE_PROCESSING_TIMEOUT_SECONDS,
            )
            if idx is None and updated_idx is not None and not to_pending:
                full_idx = await self.index_manager.load_index()
                full_idx.update(updated_idx)
                return success, full_idx
            return success, updated_idx
        except asyncio.TimeoutError:
            logger.warning(f"图片处理超时: {file_path}")
            if is_temp:
                await safe_remove_file(file_path)
            return False, idx if idx is not None else {}
        except Exception as e:
            logger.error(f"处理图片失败: {e}")
            if is_temp:
                await safe_remove_file(file_path)
            return False, idx if idx is not None else {}
