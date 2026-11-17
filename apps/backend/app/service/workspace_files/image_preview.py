"""图片资源预算、缩略预览与转码；复用 Pillow，解码只在用户打开图片时发生。"""

import io
import os
import warnings
from typing import BinaryIO, Literal, cast

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from app.service.workspace_files.file_classification import BROWSER_IMAGE_FORMATS

MAX_IMAGE_PIXELS = 80_000_000
THUMBNAIL_PIXELS = 16_000_000
MAX_IMAGE_BYTES = 32 * 1024 * 1024

ImagePreview = Literal["tile", "card"]
"""交付物用的缩略图：只有两种，缓存变体有界（地址按版本长期缓存）。"""
# 前端显示尺寸的两倍（方块 120×96、大图卡 360×240），由 tests/test_image_preview_box_guard.py 守着。
_PREVIEW_BOXES: dict[ImagePreview, tuple[int, int]] = {"tile": (240, 192), "card": (720, 480)}
_EXIF_ORIENTATION = 0x0112


def upright_size(image: Image.Image) -> tuple[int, int, bool]:
    """转正后的宽高与是否转了四分之一圈：只看 EXIF 方向（5–8 交换宽高），不解码像素。

    浏览器显示原文件时按 EXIF 方向转正，缩略图也转正，所以文件信息的宽高与缩略尺寸都按这一份口径。附件不需要它：
    附件保存时就按方向重新编码了（``attachment/image_normalizer.py``），存下来的就是转正后的像素。
    """
    quarter_turn = image.getexif().get(_EXIF_ORIENTATION) in {5, 6, 7, 8}
    width, height = (image.height, image.width) if quarter_turn else (image.width, image.height)
    return width, height, quarter_turn


def image_size(source: BinaryIO) -> tuple[int, int] | None:
    """只读文件头取原图转正后的尺寸，不解码像素；读不出时返回 None。

    返回 None 让 ``/files/info`` 照常成功、前端按「无法预览」处理。

    「读不出」包括：像素超过 Pillow 两倍上限（``DecompressionBombError``），以及文件头认不出或解析出错——Pillow 对
    认不出的抛 ``UnidentifiedImageError``，对插件已认领、解析途中出错的（截断的 BMP、PNG、JPEG 等）抛普通
    ``OSError``，两者同属 ``OSError``。不捕获会被文件接口映射成 422，面板只剩一个永远不会成功的重试。上限以内但
    超过一倍的只告警，尺寸照读。句柄已由调用方打开，这里的 ``OSError`` 只会来自解析，不会掩盖文件读不到。
    """
    source.seek(0)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                width, height, _ = upright_size(image)
                return width, height
    except (Image.DecompressionBombError, OSError):
        return None


def image_preview(source: BinaryIO, preview: ImagePreview | None = None) -> bytes | None:
    """返回要发给浏览器的 PNG；返回 None 表示原文件就能直接用。超出解码预算或内容无法识别时抛 ``ValueError``，
    由文件接口映射为「无法预览」。

    - 不带 ``preview``（文件面板、灯箱）：内核能显示且不大的图保持原文件；大图缩到 4096 边长；TIFF 等内核不支持
      的格式整张转码。
    - ``preview="tile"``（交付物方块）：铺满 240×192 的框，倍数取 ``max(240/宽, 192/高)``。方块裁切铺满、保留顶部，
      按长边缩会让长截图铺进方块时被放大发糊。
    - ``preview="card"``（单张大图卡）：装进 720×480 的框，倍数取 ``min``，即 360×240 显示区的两倍清晰度。

    缩略倍数不超过 1：小图不放大，原文件就合适时直接用原文件。动图（GIF、动态 WebP）在字节预算内原样返回，
    保住动画。转码时按 EXIF 方向转正（PNG 不带方向信息）；多页 TIFF 只出第一页。
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                pixels = image.width * image.height
                if pixels > MAX_IMAGE_PIXELS:
                    raise ValueError("图片像素过大，无法在内置阅读器中安全解码")
                native = image.format in BROWSER_IMAGE_FORMATS
                small_file = os.fstat(source.fileno()).st_size <= MAX_IMAGE_BYTES
                # Pillow 的数据属性：多帧格式才有，单帧图片没有这个属性。
                if native and small_file and getattr(image, "is_animated", False):
                    return None
                # 转正后的宽高只看 EXIF 方向，不解码：draft（JPEG 按目标尺寸降采样解码）必须在解码前调用。
                upright_width, upright_height, quarter_turn = upright_size(image)
                if preview is None:
                    target = 4096 / max(upright_width, upright_height)
                    if native and pixels <= THUMBNAIL_PIXELS and small_file:
                        return None
                else:
                    width, height = _PREVIEW_BOXES[preview]
                    fit = max if preview == "tile" else min
                    target = fit(width / upright_width, height / upright_height)
                    if native and small_file and target >= 1:
                        return None
                scale = min(1.0, target)
                size = (max(1, round(upright_width * scale)), max(1, round(upright_height * scale)))
                if native:
                    image.draft("RGB", (size[1], size[0]) if quarter_turn else size)
                upright = ImageOps.exif_transpose(image)
                displayable = _displayable(upright).resize(size, Image.Resampling.LANCZOS)
                output = io.BytesIO()
                displayable.save(output, format="PNG")
                return output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("图片像素过大，无法在内置阅读器中安全解码") from exc
    except UnidentifiedImageError as exc:
        raise ValueError("图片内容无法识别") from exc
    except OSError as exc:
        # 文件头能认、像素数据坏了（截断的 TIFF 等）在 load 时才报错；文件读不到的错误在打开句柄时已上浮。
        raise ValueError("图片内容无法解码") from exc


def _displayable(image: Image.Image) -> Image.Image:
    """把任意模式换成 PNG 能存、看起来正确的模式。

    - 16 位与 32 位灰度、浮点：按实际取值范围线性拉伸到 8 位。直接 ``convert("L")`` 是截断而不是缩放，
      科学图像与 16 位扫描件会大片发白。
    - CMYK：带 ICC 配置时经 ImageCms 转到 sRGB，否则直接转 RGB（会偏色，但没有配置也无从校正）。
    """
    if image.mode in {"I;16", "I;16B", "I;16L", "I;16N", "I", "F"}:
        wide = image.convert("F")
        # 单通道图像的 getextrema 是 (最小, 最大) 两个数；类型标注同时覆盖多通道的元组形式。
        low, high = cast(tuple[float, float], wide.getextrema())
        scale = 255 / (high - low) if high > low else 0
        return wide.point(lambda value: (value - low) * scale).convert("L")
    if image.mode == "CMYK":
        profile = image.info.get("icc_profile")
        if profile:
            converted = ImageCms.profileToProfile(
                image, io.BytesIO(profile), ImageCms.createProfile("sRGB"), outputMode="RGB"
            )
            if converted is not None:
                return converted
        return image.convert("RGB")
    return image.convert("RGBA")
