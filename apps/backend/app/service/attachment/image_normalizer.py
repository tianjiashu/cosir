"""按模型静态 capability 检测并归一化图片格式。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.llm_provider.capability.model_capability import ModelCapability


class ImageNormalizationError(ValueError):
    """图片无法满足目标模型 capability 时抛出的稳定错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    """一次图片格式归一化的结果值对象。"""

    source_format: str
    target_format: str
    content_type: str
    path: Path
    width: int
    height: int
    byte_size: int


_CONTENT_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
}


def _normalize_format(value: str | None) -> str:
    return (value or "").strip().lower().lstrip(".")


def _supported_formats(model_name: str) -> tuple[ModelCapability, frozenset[str]]:
    capability = ModelCapability.get_capability(model_name)
    if not capability.supports_image:
        raise ImageNormalizationError("VISION_NOT_SUPPORTED", "当前模型不支持图片输入")
    supported = frozenset(
        _normalize_format(item)
        for item in capability.image_limit.supported_formats
        if isinstance(item, str) and _normalize_format(item)
    )
    if not supported:
        raise ImageNormalizationError("VISION_NOT_SUPPORTED", "当前模型未声明可用图片格式")
    return capability, supported


def _inspect(source_path: Path) -> tuple[str, int, int, bool, int]:
    try:
        with Image.open(source_path) as image:
            detected = _normalize_format(image.format)
            width, height = image.size
            animated = bool(getattr(image, "is_animated", False))
            image.verify()
        if not detected or width <= 0 or height <= 0:
            raise ImageNormalizationError("ATTACHMENT_IMAGE_INVALID", "图片格式或尺寸无效")
        return detected, width, height, animated, source_path.stat().st_size
    except ImageNormalizationError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageNormalizationError(
            "ATTACHMENT_IMAGE_INVALID", "图片无法读取或存在安全风险"
        ) from exc


def _choose_target(image: Image.Image, supported: frozenset[str]) -> str:
    has_alpha = image.mode in {"RGBA", "LA", "PA"} or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        for candidate in ("png", "webp", "jpeg"):
            if candidate in supported:
                return candidate
    for candidate in ("jpeg", "png", "webp", "gif"):
        if candidate in supported:
            return candidate
    return sorted(supported)[0]


def _save_image(
    image: Image.Image,
    target: BinaryIO,
    target_format: str,
    animated: bool,
) -> None:
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
    elif target_format == "png":
        if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}:
            image = image.convert("RGBA" if "A" in image.mode else "RGB")
    elif target_format == "webp":
        save_kwargs.update(quality=95, method=6)
    elif target_format == "gif" and image.mode not in {"P", "L"}:
        image = image.convert("RGBA")

    if animated and target_format in {"gif", "webp"}:
        save_kwargs["save_all"] = True
        save_kwargs["append_images"] = []
    image.save(target, format=target_format.upper(), **save_kwargs)


def normalize_image(source_path: Path, target_path: Path, model_name: str) -> NormalizedImage:
    """检测源图并按模型 capability 输出最终图片。

    参数:
        source_path: workspace `.cosir/Attachment` 内的已暂存图片。
        target_path: 调用方提供的同一目录内临时输出路径。
        model_name: Run 最终选定的模型名。

    返回:
        输出格式、MIME、尺寸和大小已重新检测的 ``NormalizedImage``。

    异常:
        ImageNormalizationError: 模型不支持图片、源图无效、没有可用目标格式、动图
            无法保持语义或转换输出复检失败。

    副作用:
        读取源图片并将转换结果写入 ``target_path``；不写数据库、不创建 Run、不发布事件。
    """
    _, supported = _supported_formats(model_name)
    source_format, _, _, animated, _ = _inspect(source_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with Image.open(source_path) as image:
            target_format = (
                source_format
                if source_format in supported
                else _choose_target(image, supported)
            )
            # 当前实现只在不重编码时保留动图；跨格式逐帧转换需要额外处理 disposal / duration，
            # 在未实现前必须拒绝，不能静默只发送第一帧。
            if animated and target_format != source_format:
                raise ImageNormalizationError(
                    "IMAGE_CONVERSION_UNSUPPORTED",
                    "动图暂不支持跨格式转换",
                )
            if target_format == source_format:
                with source_path.open("rb") as source, target_path.open("xb") as target:
                    shutil.copyfileobj(source, target)
            else:
                normalized = ImageOps.exif_transpose(image)
                with target_path.open("xb") as target:
                    _save_image(normalized, target, target_format, animated)
    except ImageNormalizationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageNormalizationError("IMAGE_CONVERSION_FAILED", "图片格式转换失败") from exc

    try:
        output_format, width, height, _, byte_size = _inspect(target_path)
    except ImageNormalizationError as exc:
        raise ImageNormalizationError("IMAGE_CONVERSION_FAILED", "图片转换输出复检失败") from exc
    if output_format not in supported:
        raise ImageNormalizationError("IMAGE_CONVERSION_FAILED", "图片转换输出格式不符合模型能力")
    return NormalizedImage(
        source_format=source_format,
        target_format=output_format,
        content_type=_CONTENT_TYPES.get(output_format, f"image/{output_format}"),
        path=target_path,
        width=width,
        height=height,
        byte_size=byte_size,
    )


__all__ = ["ImageNormalizationError", "NormalizedImage", "normalize_image"]
