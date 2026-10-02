"""VLM 调用服务：负责调用视觉模型分析图片。"""

import asyncio
import os
import tempfile
from pathlib import Path
from collections.abc import Callable

from ...host import logger
from ...host import StealerEvent

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw as PILImageDraw
except Exception:
    PILImage = None
    PILImageDraw = None


class VLMCallService:
    """负责调用视觉模型进行图片分析。"""

    STORYBOARD_SAMPLE_COUNT = 9
    STORYBOARD_COLUMNS = 3
    STORYBOARD_ROWS = 3
    STORYBOARD_GAP = 4
    STORYBOARD_LABEL_HEIGHT = 20
    MAX_VLM_DIMENSION = 2048
    STORYBOARD_FRAME_BACKGROUND = (128, 128, 128, 255)
    STORYBOARD_SEPARATOR_COLOR = (232, 232, 232)
    STORYBOARD_LABEL_BACKGROUND = (34, 34, 34)
    STORYBOARD_LABEL_COLOR = (255, 255, 255)
    ANIMATED_STORYBOARD_PROMPT = (
        "[GIF 九宫格时间序列说明]\n"
        "输入图是从同一 GIF 等距抽取 9 帧后拼成的 3×3 分镜。"
        "阅读顺序从左到右、从上到下：第一行 1→2→3，第二行 4→5→6，第三行 7→8→9。"
        "九格中相同的人物或物体是同一主体在不同时刻的样子；源 GIF 不足 9 帧时相邻格可能重复。\n"
        "请按时间顺序比较各格的变化，概括完整的动作或表情变化过程和最终传达的情绪。"
        "不要将九格描述为多人合照、九张独立图片或同时发生的多个场景。\n"
        "每格上方的深色编号栏与数字、格间浅色分隔线和中性灰透明区域底色均由预处理添加，忽略这些人工元素。"
        "识别图上文字时逐字转写原文；同一句文字跨格重复只记录一次，不要把帧序号写入 overlay_text。\n\n"
    )

    def __init__(self, plugin_instance) -> None:
        self.plugin = plugin_instance
        self.plugin_config = getattr(plugin_instance, "plugin_config", None)

    def _models(self):
        return getattr(self.plugin, "models", None)

    def describe_vision_model(self) -> str:
        """当前生效的视觉模型（model@地址），未配置返回空串。"""
        models = self._models()
        return models.describe("vision") if models is not None else ""

    async def _call_vision_model(
        self, event: StealerEvent | None, img_path: str, prompt: str,
        *, validate_response: Callable[[str], None] | None = None,
    ) -> str:
        """调用视觉模型分析图片。

        经插件的模型网关（OpenAI 兼容接口）调用视觉模型，支持指数退避重试。
        对于 GIF 动图，会均匀采样九帧并生成 3×3 分镜后分析。

        Args:
            event: 消息事件（保留参数，便于调用方统一传入）
            img_path: 图片绝对路径（调用方需保证已验证）
            prompt: 提示词

        Returns:
            str: 模型响应文本

        Raises:
            ValueError: 未配置视觉模型
            FileNotFoundError: 图片文件不存在
            Exception: 模型调用失败（已重试）
        """
        # 路径规范化
        img_path_obj = Path(img_path)
        if not img_path_obj.is_absolute():
            data_dir = getattr(self.plugin_config, "data_dir", None)
            img_path_obj = (
                (Path(data_dir) / img_path).resolve() if data_dir else img_path_obj.resolve()
            )
        img_path = str(img_path_obj)

        if not os.path.exists(img_path):
            raise FileNotFoundError(f"图片文件不存在: {img_path}")

        model_name = self.describe_vision_model()
        if not model_name:
            raise ValueError(
                "未配置视觉模型，无法进行图片分析。"
                "请在插件配置中填写 vision_model，或在 mohobot 全局配置里设置 llm.vision_model。"
            )

        # 处理 GIF 动图：均匀采样九帧并生成 3×3 分镜
        temp_file = None
        try:
            actual_img_path, is_animated = await self._prepare_image_for_vlm(img_path)
            if actual_img_path != img_path:
                temp_file = actual_img_path  # 标记为临时文件，分析后删除

            resolved_img_path = str(Path(actual_img_path).resolve())

            # 如果是动图九宫格，添加专用提示词前缀
            actual_prompt = prompt
            if is_animated:
                actual_prompt = self.ANIMATED_STORYBOARD_PROMPT + prompt

            return await self._do_vlm_call(
                model_name, actual_prompt, resolved_img_path, validate_response=validate_response
            )
        finally:
            # 清理临时文件
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                    logger.debug(f"已清理临时拼接图: {temp_file}")
                except Exception as e:
                    logger.warning(f"清理临时文件失败: {e}")

    async def _prepare_image_for_vlm(self, img_path: str) -> tuple[str, bool]:
        """为 VLM 分析准备图片，对 GIF 均匀采样并生成 3×3 分镜。

        Args:
            img_path: 原始图片路径

        Returns:
            tuple[str, bool]: (准备好的图片路径, 是否为动图拼接)
        """
        # 只处理 GIF 文件
        if not img_path.lower().endswith(".gif"):
            return img_path, False

        if PILImage is None:
            return img_path, False

        try:
            # 检测是否为动图
            def _check_animated(fp: str) -> tuple[bool, int, int, int]:
                with PILImage.open(fp) as im:
                    is_animated = bool(getattr(im, "is_animated", False))
                    n_frames = int(getattr(im, "n_frames", 1) or 1)
                    width, height = im.size
                    return is_animated, n_frames, width, height

            is_animated, n_frames, width, height = await asyncio.to_thread(
                _check_animated, img_path
            )

            # 非动图或帧数太少，直接返回原路径
            if not is_animated or n_frames <= 1:
                return img_path, False

            frame_indices = self._uniform_frame_indices(n_frames)
            frame_width, frame_height = self._storyboard_frame_size(width, height)

            def _extract_and_combine(fp: str) -> tuple[str, int, int, int]:
                decoded_frames = {}
                resampling = getattr(PILImage, "Resampling", PILImage).LANCZOS

                with PILImage.open(fp) as im:
                    for frame_idx in dict.fromkeys(frame_indices):
                        im.seek(frame_idx)
                        frame = im.convert("RGBA")
                        if frame.size != (frame_width, frame_height):
                            frame = frame.resize((frame_width, frame_height), resampling)

                        # GIF 透明像素由中性灰底承接，兼顾黑色与白色线条的可读性。
                        backdrop = PILImage.new(
                            "RGBA",
                            (frame_width, frame_height),
                            self.STORYBOARD_FRAME_BACKGROUND,
                        )
                        backdrop.alpha_composite(frame)
                        decoded_frames[frame_idx] = backdrop.convert("RGB")

                cell_height = frame_height + self.STORYBOARD_LABEL_HEIGHT
                grid_width = (
                    frame_width * self.STORYBOARD_COLUMNS
                    + self.STORYBOARD_GAP * (self.STORYBOARD_COLUMNS - 1)
                )
                grid_height = (
                    cell_height * self.STORYBOARD_ROWS
                    + self.STORYBOARD_GAP * (self.STORYBOARD_ROWS - 1)
                )
                combined = PILImage.new(
                    "RGB",
                    (grid_width, grid_height),
                    self.STORYBOARD_SEPARATOR_COLOR,
                )
                draw = PILImageDraw.Draw(combined) if PILImageDraw is not None else None

                for sample_idx, frame_idx in enumerate(frame_indices):
                    row, column = divmod(sample_idx, self.STORYBOARD_COLUMNS)
                    x = column * (frame_width + self.STORYBOARD_GAP)
                    y = row * (cell_height + self.STORYBOARD_GAP)
                    combined.paste(
                        PILImage.new(
                            "RGB",
                            (frame_width, self.STORYBOARD_LABEL_HEIGHT),
                            self.STORYBOARD_LABEL_BACKGROUND,
                        ),
                        (x, y),
                    )
                    if draw is not None:
                        draw.text(
                            (x + 6, y + 3),
                            str(sample_idx + 1),
                            fill=self.STORYBOARD_LABEL_COLOR,
                        )
                    combined.paste(
                        decoded_frames[frame_idx],
                        (x, y + self.STORYBOARD_LABEL_HEIGHT),
                    )

                temp_fd, temp_path = tempfile.mkstemp(suffix=".png")
                os.close(temp_fd)
                try:
                    # 使用无损 PNG，避免压缩噪声损伤字幕和细线。
                    combined.save(temp_path, "PNG", optimize=True)
                except Exception:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                    raise

                final_w, final_h = combined.size
                return temp_path, len(frame_indices), final_w, final_h

            temp_path, actual_frames, final_width, final_height = await asyncio.to_thread(
                _extract_and_combine, img_path
            )
            logger.debug(
                f"GIF 九宫格完成: {n_frames} 帧 -> {actual_frames} 个采样格, "
                f"采样索引: {frame_indices}, 输出尺寸: {final_width}x{final_height}"
            )
            return temp_path, True

        except Exception as e:
            logger.warning(f"GIF 动图帧提取失败，使用原图: {e}")
            return img_path, False

    @classmethod
    def _uniform_frame_indices(cls, frame_count: int) -> list[int]:
        """返回覆盖首尾的 9 个等距帧索引，短 GIF 通过重复邻近帧补齐。"""
        if frame_count <= 0:
            return []
        if cls.STORYBOARD_SAMPLE_COUNT <= 1:
            return [0]

        span = frame_count - 1
        denominator = cls.STORYBOARD_SAMPLE_COUNT - 1
        return [
            (sample_idx * span + denominator // 2) // denominator
            for sample_idx in range(cls.STORYBOARD_SAMPLE_COUNT)
        ]

    @classmethod
    def _storyboard_frame_size(cls, width: int, height: int) -> tuple[int, int]:
        """在保持帧比例的前提下，让完整九宫格落入 VLM 尺寸上限。"""
        if width <= 0 or height <= 0:
            raise ValueError("GIF 帧尺寸必须大于 0")

        available_width = cls.MAX_VLM_DIMENSION - cls.STORYBOARD_GAP * (
            cls.STORYBOARD_COLUMNS - 1
        )
        available_height = (
            cls.MAX_VLM_DIMENSION
            - cls.STORYBOARD_GAP * (cls.STORYBOARD_ROWS - 1)
            - cls.STORYBOARD_LABEL_HEIGHT * cls.STORYBOARD_ROWS
        )
        max_frame_width = max(1, available_width // cls.STORYBOARD_COLUMNS)
        max_frame_height = max(1, available_height // cls.STORYBOARD_ROWS)
        scale = min(1.0, max_frame_width / width, max_frame_height / height)
        return max(1, int(width * scale)), max(1, int(height * scale))

    async def _do_vlm_call(
        self, model_name: str, prompt: str, file_path: str,
        *, validate_response: Callable[[str], None] | None = None,
    ) -> str:
        """执行 VLM 调用（带重试）。

        Args:
            model_name: 模型描述（仅用于日志）
            prompt: 提示词
            file_path: 本地图片路径

        Returns:
            str: 模型响应文本
        """
        models = self._models()
        if models is None:
            raise ValueError("模型网关未初始化")
        # 重试配置
        try:
            max_retries = max(1, int(getattr(self.plugin, "vision_max_retries", 3)))
        except (TypeError, ValueError):
            max_retries = 3
        try:
            retry_delay = float(getattr(self.plugin, "vision_retry_delay", 1.0))
        except (TypeError, ValueError):
            retry_delay = 1.0
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                logger.debug(
                    f"调用VLM (尝试 {attempt + 1}/{max_retries}), "
                    f"model={model_name}, 图片={file_path}"
                )
                text = (await models.vision(prompt, file_path) or "").strip()
                if text:
                    if validate_response is not None:
                        validate_response(text)
                    logger.debug(f"VLM响应: {text[:200]}")
                    return text

                logger.warning("VLM返回空响应")
                last_error = Exception("VLM返回空响应")

            except Exception as e:
                last_error = e
                error_msg = str(e)
                is_rate_limit = any(
                    kw in error_msg
                    for kw in (
                        "429",
                        "RateLimit",
                        "rate limit",
                        "exceeded your current request limit",
                    )
                )
                is_auth_error = any(kw in error_msg for kw in ("401", "403", "Unauthorized"))
                if is_rate_limit:
                    logger.warning(f"VLM请求被限流 ({attempt + 1}/{max_retries})")
                elif is_auth_error:
                    logger.error(
                        f"VLM鉴权失败 ({attempt + 1}/{max_retries}): {e}\n"
                        f"  当前视觉模型: {model_name}\n"
                        f"  提示: 请检查插件配置的 vision_base_url / vision_api_key，"
                        f"  或清空它们改用 mohobot 全局视觉模型配置"
                    )
                else:
                    logger.error(f"VLM调用失败 ({attempt + 1}/{max_retries}): {e}")

            # 指数退避
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (2**attempt))

        raise Exception(f"视觉模型调用失败（已重试{max_retries}次）: {last_error}") from last_error
