"""图片资源预算与缩略预览；复用 Pillow，解码只在用户打开图片时发生。"""

import io
import os
import warnings
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_PIXELS = 80_000_000
THUMBNAIL_PIXELS = 16_000_000
MAX_IMAGE_BYTES = 32 * 1024 * 1024


def image_preview(source: BinaryIO) -> bytes | None:
    """普通图片保持原格式；大图缩到 4096 边长，超出解码预算明确报错。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                pixels = image.width * image.height
                if pixels > MAX_IMAGE_PIXELS:
                    raise ValueError("图片像素过大，无法在内置阅读器中安全解码")
                if (
                    pixels <= THUMBNAIL_PIXELS
                    and os.fstat(source.fileno()).st_size <= MAX_IMAGE_BYTES
                ):
                    return None
                image.draft("RGB", (4096, 4096))
                image.thumbnail((4096, 4096))
                output = io.BytesIO()
                image.convert("RGBA").save(output, format="PNG")
                return output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("图片像素过大，无法在内置阅读器中安全解码") from exc
    except UnidentifiedImageError as exc:
        raise ValueError("图片内容无法识别") from exc
