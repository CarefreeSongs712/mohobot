"""LLM meme search and send workflows used by the registered entrypoints."""

import os

from ...host import logger
from ...host import StealerEvent

from ..events.event_context import unwrap_event
from ..search.query_rewriter import MemeQueryRewriter
from ..util.normalization import canonicalize_path, normalize_label_list


class MemeSearchToolWorkflow:
    def _query_rewriter(self) -> MemeQueryRewriter:
        rewriter = self.__dict__.get("_meme_query_rewriter")
        if rewriter is None:
            rewriter = MemeQueryRewriter(self)
            self.__dict__["_meme_query_rewriter"] = rewriter
        return rewriter

    def _character_display_name(self, key: str) -> str:
        if not key:
            return ""
        info_map = getattr(self.plugin_config, "character_info", None) or {}
        info = info_map.get(key) if isinstance(info_map, dict) else None
        if isinstance(info, dict) and info.get("name"):
            return str(info["name"])
        return key

    def _format_candidate(self, number: int, meta: dict, *, recent: bool) -> list[str]:
        lines = [f"\n[{number}]"]
        overlay = str(meta.get("overlay_text", "") or "").strip()
        desc = str(meta.get("desc", "") or "").strip()
        scenes = normalize_label_list(meta.get("scenes") or meta.get("scene"))
        tags = normalize_label_list(meta.get("tags"))
        character = self._character_display_name(str(meta.get("character", "") or ""))
        if overlay:
            lines.append(f"    图上文字：{overlay}")
        if desc:
            lines.append(f"    描述：{desc}")
        if scenes:
            lines.append(f"    适合回应：{' / '.join(scenes)}")
        if character:
            lines.append(f"    角色：{character}")
        if tags:
            lines.append(f"    标签：{', '.join(tags)}")
        if recent:
            lines.append("    （最近刚发过）")
        return lines

    async def _search_meme_impl(self, event: StealerEvent, description: str):
        """按描述在表情库的向量空间中召回候选，交给会话模型自己挑。"""
        event = unwrap_event(event)
        description = str(description or "").strip()
        logger.info(f"[Tool] LLM 搜索表情包: {description}")
        turn_state = self._emoji_sender_engine.emoji_turn_state(event)

        try:
            if not description:
                yield "搜索失败：缺少 description。请描述你想发的表情包：画面、图上文字或语气。"
                return
            if not self.is_send_enabled_for_event(event):
                yield "搜索失败：当前会话已禁用表情包功能，请不要继续调用表情包工具。"
                return

            user_message = ""
            try:
                user_message = event.get_message_str() or ""
            except Exception:
                pass
            query = await self._query_rewriter().rewrite(
                event, description, user_message=user_message
            )

            limit = self._emoji_sender_engine.candidate_count()
            results = await self.meme_selector.semantic_candidates(
                query, limit=limit, event=event
            )
            if not results:
                turn_state.set_candidates([])
                yield (
                    f"没有找到和“{description}”接近的表情包。"
                    "可以换个说法再搜一次，或者这轮就不发表情包。"
                )
                return

            recent = self.meme_selector._recently_sent_paths()
            candidates = []
            lines = [f"找到 {len(results)} 个候选表情包："]
            for number, (path, meta, score) in enumerate(results, 1):
                candidates.append(
                    {"id": number, "path": path, "meta": meta, "score": score}
                )
                lines.extend(
                    self._format_candidate(
                        number, meta, recent=canonicalize_path(path) in recent
                    )
                )
            turn_state.set_candidates(candidates)
            lines.append(
                f"\n\n合适就 {self.SEND_MEME_TOOL_NAME}(emoji_id=编号)；都不合适就不发。"
            )
            logger.info(f"[Tool] 搜索完成，返回 {len(candidates)} 个候选")
            yield "\n".join(lines)

        except Exception as e:
            logger.error(f"[Tool] 搜索表情包失败: {e}", exc_info=True)
            yield f"搜索出错：{e}"

    async def _send_meme_impl(self, event: StealerEvent, emoji_id: int):
        """选定 search_meme 的候选；图片排队，等文字回复发出后再发送。"""
        event = unwrap_event(event)
        logger.info(f"[Tool] LLM 选择表情包编号: {emoji_id}")
        turn_state = self._emoji_sender_engine.emoji_turn_state(event)

        try:
            if not self.is_send_enabled_for_event(event):
                yield "发送失败：reason=send_disabled。当前会话已禁用表情包发送功能，请不要继续调用发送工具。"
                return
            try:
                emoji_id = int(emoji_id)
            except (TypeError, ValueError):
                yield f"发送失败：reason=invalid_id。编号 {emoji_id} 无法解析为整数。"
                return

            candidates = turn_state.get_candidates()
            if not candidates:
                yield f"发送失败：reason=candidate_expired。请先调用 {self.SEARCH_MEME_TOOL_NAME}。"
                return
            if emoji_id < 1 or emoji_id > len(candidates):
                yield f"发送失败：reason=invalid_id。可选编号范围：1-{len(candidates)}。"
                return

            selected = candidates[emoji_id - 1]
            path = selected["path"]
            if not os.path.exists(path):
                yield "发送失败：reason=file_missing。这张表情包文件已丢失，请选择其他候选。"
                return
            if not self.meme_selector.is_path_allowed_for_event(path, event):
                yield "发送失败：reason=scope_denied。这张表情包只能在来源会话发送，请选择其他候选。"
                return

            self._emoji_sender_engine.queue_meme(event, selected)
            logger.info(f"[Tool] 表情包已排队，回复后发送: {path}")
            yield f"已选定 [{emoji_id}]，会在文字回复后自动发出。正文里不用提到这张表情包。"

        except Exception as e:
            logger.error(f"[Tool] 选择表情包失败: {e}", exc_info=True)
            yield f"发送出错：{e}"
