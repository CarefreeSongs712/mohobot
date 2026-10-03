import asyncio
from collections import Counter
from typing import Any

from ...host import logger

from ...host import StealerEvent

from ..maintenance.retention import library_counts


class CommandHandler:
    """命令处理服务类，负责处理所有与插件相关的命令操作。"""

    def __init__(self, plugin_instance: Any):
        """初始化命令处理服务。

        Args:
            plugin_instance: StealerPlugin 实例，用于访问插件的配置和服务
        """
        self.plugin = plugin_instance
        self._cleaned = False  # 清理标志位

    def _apply_config_updates(self, updates: dict) -> bool:
        try:
            return bool(self.plugin.update_config(updates))
        except Exception as exc:
            logger.error(f"指令更新配置失败: {exc}")
            return False

    async def meme_on(self, event: StealerEvent):
        """开启偷表情包功能。"""
        saved = self._apply_config_updates({"steal_meme": True})
        yield event.plain_result("已开启偷表情包" if saved else "❌ 开启偷表情包失败：配置未保存")

    async def meme_off(self, event: StealerEvent):
        """关闭偷表情包功能。"""
        saved = self._apply_config_updates({"steal_meme": False})
        yield event.plain_result("已关闭偷表情包" if saved else "❌ 关闭偷表情包失败：配置未保存")

    async def auto_on(self, event: StealerEvent):
        """开启自动发送功能。"""
        saved = self._apply_config_updates({"auto_send_meme": True})
        yield event.plain_result("已开启自动发送" if saved else "❌ 开启自动发送失败：配置未保存")

    async def auto_off(self, event: StealerEvent):
        """关闭自动发送功能。"""
        saved = self._apply_config_updates({"auto_send_meme": False})
        yield event.plain_result("已关闭自动发送" if saved else "❌ 关闭自动发送失败：配置未保存")

    async def capture(self, event: StealerEvent):
        window_seconds = 30

        if hasattr(self.plugin, "begin_force_capture"):
            self.plugin.begin_force_capture(event, window_seconds)
            yield event.plain_result(
                f"✅ 已进入强制接收窗口：{window_seconds} 秒内发送 1 张图片将自动分类并入库"
            )
            return

        yield event.plain_result("❌ 插件未初始化强制接收能力")

    async def status(self, event: StealerEvent):
        """显示插件状态和详细的表情包统计信息。"""
        stealing_status = "开启" if self.plugin.plugin_config.steal_meme else "关闭"
        auto_send_meme_status = "开启" if self.plugin.plugin_config.auto_send_meme else "关闭"

        image_index = await self.plugin.index_manager.load_index()
        total_count = len(image_index)

        # 添加视觉模型信息（只显示模型名，不带接口地址）
        models = getattr(self.plugin, "models", None)
        vision_sig = models.describe("vision") if models is not None else ""
        vision_model = vision_sig.split("@", 1)[0] if vision_sig else "未设置（无法审核与标注）"
        embedding_sig = models.describe("embedding") if models is not None else ""
        embedding_model = embedding_sig.split("@", 1)[0] if embedding_sig else "未设置（降级为关键词检索）"

        # 基础状态信息
        steal_mode = self.plugin.plugin_config.steal_mode
        if steal_mode == "probability":
            mode_desc = f"概率模式 (概率={self.plugin.plugin_config.steal_chance})"
        else:
            mode_desc = f"冷却模式 (冷却={self.plugin.plugin_config.image_processing_cooldown}秒)"

        status_text = "🔧 插件状态:\n"
        status_text += f"偷取: {stealing_status}\n"
        status_text += f"偷图模式: {mode_desc}\n"
        status_text += f"自动发送: {auto_send_meme_status}\n"
        status_text += f"发送概率: {self.plugin.plugin_config.meme_chance}\n"
        audit_mode = "人工审核后入库" if self.plugin.plugin_config.audit_required else "VLM 审核通过即入库"
        status_text += f"审核: {audit_mode}\n"
        status_text += f"视觉模型: {vision_model}\n"
        status_text += f"嵌入模型: {embedding_model}\n\n"

        # 后台任务状态
        status_text += "⚙️ 后台任务:\n"
        status_text += "Raw清理: 自动 (30min)\n"
        status_text += "容量控制: 自动 (60min)\n\n"

        # 表情包统计信息
        if total_count == 0:
            status_text += "📊 表情包统计:\n暂无表情包数据"
        else:
            # 按分类统计
            category_stats = Counter(
                img_info.get("category", "未分类")
                for img_info in image_index.values()
                if isinstance(img_info, dict)
            )

            # 构建统计信息
            status_text += "📊 表情包统计:\n"
            counts = library_counts(image_index)
            status_text += f"总数量: {total_count}\n"
            status_text += f"通用库数量 / 软上限: {counts['automatic']}/{self.plugin.plugin_config.max_reg_num}\n"
            status_text += f"收藏: {counts['favorites']}；角色表情库: {counts['characters']}（均不参与自动淘汰）\n\n"

            # 分类统计 - 只显示前5个最多的分类
            status_text += "📂 分类统计 (前5):\n"
            sorted_categories = sorted(category_stats.items(), key=lambda x: x[1], reverse=True)
            for category, count in sorted_categories[:5]:
                percentage = count / total_count * 100
                status_text += f"  {category}: {count}张 ({percentage:.1f}%)\n"

            if len(sorted_categories) > 5:
                status_text += f"  ...还有{len(sorted_categories) - 5}个分类\n"

            # 存储统计
            raw_count = (
                len(list(self.plugin.raw_dir.glob("*"))) if self.plugin.raw_dir.exists() else 0
            )
            status_text += "\n💾 存储信息:\n"
            status_text += f"  原始图片: {raw_count}张 | 分类图片: {total_count}张"

        yield event.plain_result(status_text)

    async def tag_stats(self, event: StealerEvent, limit: str = ""):
        """标签/场景统计：高频标签、低频标签、零标签条目。

        用法: /meme tag_stats [N]   （N 为 Top 数量，默认 15）
        """
        try:
            db = getattr(self.plugin, "db_service", None)
            if db is None or not hasattr(db, "get_tag_stats"):
                yield event.plain_result("❌ 数据库服务不可用")
                return

            top_n = 15
            try:
                parsed = int(str(limit or "").strip())
                if 1 <= parsed <= 50:
                    top_n = parsed
            except ValueError:
                pass

            # 同步 sqlite 查询丢线程池，避免阻塞事件循环（best practice）
            stats = await asyncio.to_thread(db.get_tag_stats, top_n)

            lines: list[str] = []
            lines.append("🏷️ 标签统计（打标质量体检）:")
            lines.append(
                f"总表情: {stats['total_emojis']} | 有标签: {stats['total_with_tags']} | "
                f"无标签: {stats['zero_tag_count']}"
            )
            if stats["zero_tag_count"]:
                lines.append("⚠️ 无标签的表情无法被关键词检索，建议在 WebUI 补充标签")

            lines.append(f"\n📈 高频标签 Top {top_n}:")
            if stats["top_tags"]:
                for i, item in enumerate(stats["top_tags"], 1):
                    lines.append(f"  {i}. {item['tag']} ×{item['count']}")
            else:
                lines.append("  （暂无标签）")

            if stats["single_use_tags"]:
                lines.append("\n🔍 低频标签（仅出现 1 次，疑似噪声/同义词，可考虑合并）:")
                shown = "、".join(stats["single_use_tags"][:10])
                more = f" 等共 {len(stats['single_use_tags'])} 个" if len(stats["single_use_tags"]) > 10 else ""
                lines.append(f"  {shown}{more}")

            if stats["top_scenes"]:
                lines.append(f"\n🎬 高频场景 Top {min(5, len(stats['top_scenes']))}:")
                for i, item in enumerate(stats["top_scenes"][:5], 1):
                    lines.append(f"  {i}. {item['scene']} ×{item['count']}")

            lines.append("\n💡 提示: 低频标签多说明 VLM 输出不稳定，可在审核时统一措辞")

            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"标签统计失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 标签统计失败: {e}")

    async def clean(self, event: StealerEvent, mode: str = ""):
        """手动触发清理操作，清理raw目录中的原始图片文件，不影响已分类的表情包。

        Args:
            event: 消息事件
            mode: 留空或 raw，均只清理 raw 原始图片
        """
        if str(mode or "").strip().lower() not in {"", "raw"}:
            yield event.plain_result("用法: /meme clean [raw]；仅清理 raw 原始图片")
            return
        try:
            # 清理所有raw文件（因为成功分类的文件已经被立即删除了）
            deleted_count = await self._force_clean_raw_directory()
            yield event.plain_result(f"✅ raw目录清理完成，共删除 {deleted_count} 张原始图片")
        except Exception as e:
            logger.error(f"手动清理失败: {e}")
            yield event.plain_result(f"❌ 清理失败: {str(e)}")

    async def _force_clean_raw_directory(self) -> int:
        """强制清理raw目录中的所有文件（忽略保留期限），返回删除的文件数量。"""
        event_handler = self.plugin.event_handler
        if event_handler is not None:
            return await event_handler._clean_raw_directory()
        return 0

    async def enforce_capacity(self, event: StealerEvent):
        """手动执行容量控制：超过软上限时淘汰语义冗余且少用的通用表情。"""
        try:
            # 加载图片索引
            image_index = await self.plugin.index_manager.load_index()

            current_count = library_counts(image_index)["automatic"]
            max_count = self.plugin.plugin_config.max_reg_num

            if current_count <= max_count:
                yield event.plain_result(
                    f"当前通用库 {current_count} 张，未超过软上限 {max_count}，无需清理"
                )
                return

            # 执行容量控制
            await self.plugin.event_handler._enforce_capacity(image_index)
            await self.plugin.index_manager.save_index(image_index)

            # 重新统计
            new_count = library_counts(image_index)["automatic"]
            removed_count = max(0, current_count - new_count)

            yield event.plain_result(
                f"容量控制完成\n"
                f"删除了 {removed_count} 个语义冗余的通用表情包\n"
                f"通用库数量 / 软上限: {new_count}/{max_count}（收藏和角色库另行管理；"
                "没有冗余时允许超过上限，靠降低偷图采纳率控制增长）"
            )
        except Exception as e:
            logger.error(f"容量控制失败: {e}")
            yield event.plain_result(f"容量控制失败: {str(e)}")

    def cleanup(self):
        """清理资源。"""
        if self._cleaned:
            return
        self._cleaned = True
        # CommandHandler 主要是无状态的，清理插件引用即可
        self.plugin = None
