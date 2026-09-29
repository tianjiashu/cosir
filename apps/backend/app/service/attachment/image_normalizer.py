"""在上传边界完成图片格式、方向和尺寸规范化。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

from app.service.attachment.image_policy import DEFAULT_IMAGE_INPUT_POLICY


class ImageNormalizationError(ValueError):
    """图片无法满足统一输入策略时抛出的稳定错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    """上传阶段生成的最终图片元数据。

    ``path`` 指向调用方提供的临时输出文件；调用方负责在完成幂等发布后处理该文件。
    该值对象不负责持久化，也不负责再次验证模型能力。
    """

    source_format: str
    target_format: str
    content_type: str
    path: Path
    width: int
    height: int
    byte_size: int


@dataclass(frozen=True, slots=True)
class StoredImage:
    """已完成上传的图片文件句柄。

    该值对象只描述已由上传边界规范化并发布的文件，不重新解析图片内容。调用方仍须
    在读取前完成 workspace 路径和文件存在性检查。
    """

    path: Path
    content_type: str
    byte_size: int


_CONTENT_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
}


def _normalize_format(value: str | None) -> str:
    return (value or "").strip().lower().lstrip(".")


def _inspect(source_path: Path) -> tuple[str, int, int, int]:
    """读取图片格式、尺寸和字节数，并完成一次 Pillow 完整性检查。"""

    try:
        with Image.open(source_path) as image:
            detected = _normalize_format(image.format)
            width, height = image.size
            image.verify()
        if not detected or width <= 0 or height <= 0:
            raise ImageNormalizationError("ATTACHMENT_IMAGE_INVALID", "图片格式或尺寸无效")
        return detected, width, height, source_path.stat().st_size
    except ImageNormalizationError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise ImageNormalizationError(
            "ATTACHMENT_IMAGE_INVALID", "图片无法读取或存在安全风险"
        ) from exc


def _resize_to_policy(image: Image.Image) -> tuple[Image.Image, bool]:
    """按统一最长边限制等比例缩放图片，并返回是否发生了缩放。"""

    max_side = DEFAULT_IMAGE_INPUT_POLICY.max_image_side_px
    width, height = image.size
    longest_side = max(width, height)
    if longest_side <= max_side:
        return image, False

    scale = max_side / longest_side
    target_size = (
        max(1, round(width * scale)),
        max(1, round(height * scale)),
    )
    return image.resize(target_size, Image.Resampling.LANCZOS), True


def _save_image(image: Image.Image, target: BinaryIO, target_format: str) -> None:
    """以统一格式写出静态图片，并清理不再适用的颜色模式。"""

    save_kwargs: dict[str, object] = {}
    if target_format == "jpeg":
        if image.mode in {"RGBA", "LA", "P", "PA"}:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        save_kwargs.update(quality=95, optimize=True)
    elif target_format == "png" and image.mode not in {
        "1",
        "L",
        "LA",
        "P",
        "RGB",
        "RGBA",
        "I",
        "I;16",
    }:
        image = image.convert("RGBA" if "A" in image.mode else "RGB")
    image.save(target, format=target_format.upper(), **save_kwargs)


def normalize_image(source_path: Path, target_path: Path) -> NormalizedImage:
    """生成符合统一输入策略的 JPEG/PNG 图片。

    参数:
        source_path: 待上传的临时图片文件。
        target_path: 调用方提供的临时规范化输出路径；必须由调用方负责安全落盘。

    返回:
        ``NormalizedImage``，包含最终格式、尺寸、MIME 类型和字节数。

    异常:
        ImageNormalizationError: 源文件不是 JPEG/PNG、内容无效、存在 Pillow 安全风险、
            无法完成方向处理或规范化结果仍不满足统一最长边限制。

    副作用:
        仅读取源文件，并将规范化结果写入 ``target_path``；不写数据库、不发布事件。
    """

    supported = DEFAULT_IMAGE_INPUT_POLICY.supported_formats
    source_format, _, _, _ = _inspect(source_path)
    if source_format not in supported:
        raise ImageNormalizationError("ATTACHMENT_TYPE_UNSUPPORTED", "暂不支持该图片格式")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    output_created = False
    try:
        with Image.open(source_path) as image:
            orientation = image.getexif().get(274, 1)
            normalized = ImageOps.exif_transpose(image)
            normalized, resized = _resize_to_policy(normalized)
            needs_reencode = resized or orientation not in (None, 1)
            if not needs_reencode:
                with source_path.open("rb") as source, target_path.open("xb") as target:
                    output_created = True
                    shutil.copyfileobj(source, target)
            else:
                with target_path.open("xb") as target:
                    output_created = True
                    _save_image(normalized, target, source_format)
    except ImageNormalizationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        if output_created:
            target_path.unlink(missing_ok=True)
        raise ImageNormalizationError("IMAGE_CONVERSION_FAILED", "图片规范化失败") from exc

    try:
        output_format, width, height, byte_size = _inspect(target_path)
    except ImageNormalizationError as exc:
        if output_created:
            target_path.unlink(missing_ok=True)
        raise ImageNormalizationError("IMAGE_CONVERSION_FAILED", "图片规范化输出复检失败") from exc
    if output_format not in supported:
        if output_created:
            target_path.unlink(missing_ok=True)
        raise ImageNormalizationError("IMAGE_CONVERSION_FAILED", "图片规范化输出格式不符合统一策略")
    if max(width, height) > DEFAULT_IMAGE_INPUT_POLICY.max_image_side_px:
        if output_created:
            target_path.unlink(missing_ok=True)
        raise ImageNormalizationError("IMAGE_CONVERSION_FAILED", "图片规范化后仍超过最长边限制")

    return NormalizedImage(
        source_format=source_format,
        target_format=output_format,
        content_type=_CONTENT_TYPES[output_format],
        path=target_path,
        width=width,
        height=height,
        byte_size=byte_size,
    )


__all__ = [
    "ImageNormalizationError",
    "NormalizedImage",
    "StoredImage",
    "normalize_image",
]
