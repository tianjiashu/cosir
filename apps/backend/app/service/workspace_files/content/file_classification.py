"""文件类型判定：文件面板与交付记录的 ``kind`` / ``mime`` 只在这里得出，预览器按它分发。

判定顺序是「魔数 → 后缀表 → 能否解码为文本」：

- 魔数先行：PDF 与浏览器内核能显示的位图按文件头认，后缀写错也不影响；TIFF 同样按文件头认，资源接口会把它
  转成 PNG（``image_preview``）。相机 RAW（DNG、NEF、CR2 等）也以 TIFF 头开头，Pillow 只能读出其中的小缩略图，
  显示出来像是预览坏了，所以按后缀先行排除，归 ``binary``。
- 后缀表是唯一事实源，不用 ``mimetypes``：它读本机类型表，同一后缀在不同机器上结果不同（实测 ``.ts`` 被判成
  ``video/mp2t``、``.mmd`` 被判成卡拉 OK 格式），而 ``mime`` 直接成为资源响应的 ``Content-Type``。
- 音视频只看后缀、不看能否解码：二进制内容偶尔会被字符集探测「解码」成功；后缀错了的话播放器会报格式错误，
  前端统一降级到「看不了的文件」，不会停在黑框上。
- 其余文本类型（Markdown、SVG、表格、Mermaid）要求内容能解码为文本，否则按二进制处理。
- 都不是时兜底：能解码的归 ``text``（按源码显示），其余归 ``binary``；``mime`` 只取确定的值，不猜。
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.models.enums.file_kind import FileKind

TEXT_MIME = "text/plain"
BINARY_MIME = "application/octet-stream"

# BMP 的 DIB 信息头长度（偏移 14 处的小端 4 字节）：只比 b"BM" 两个字节会把 BMI 表格、BMW 笔记这类文本认成位图。
_BMP_INFO_HEADER_SIZES = frozenset({12, 40, 52, 56, 64, 108, 124})


@dataclass(frozen=True)
class _BrowserImage:
    """一种浏览器内核能直接显示的位图：怎么按文件头认它、它的 MIME、Pillow 给它的格式名。"""

    mime: str
    pillow_format: str
    matches: Callable[[bytes], bool]


# 浏览器内核能直接显示的位图，判定（按文件头认）与缩略（原文件能不能直接给浏览器）都只读这一张表：
# 新增一种格式只改这里，不会出现「判定认成图片、缩略却去转码」的两表不一致。TIFF 不在表里：内核显示不了，要转码。
_BROWSER_IMAGES: tuple[_BrowserImage, ...] = (
    _BrowserImage("image/png", "PNG", lambda sample: sample.startswith(b"\x89PNG\r\n")),
    _BrowserImage("image/jpeg", "JPEG", lambda sample: sample.startswith(b"\xff\xd8\xff")),
    _BrowserImage("image/gif", "GIF", lambda sample: sample.startswith((b"GIF87a", b"GIF89a"))),
    _BrowserImage("image/x-icon", "ICO", lambda sample: sample.startswith(b"\x00\x00\x01\x00")),
    _BrowserImage(
        "image/bmp",
        "BMP",
        lambda sample: sample[:2] == b"BM"
        and int.from_bytes(sample[14:18], "little") in _BMP_INFO_HEADER_SIZES,
    ),
    _BrowserImage(
        "image/webp", "WEBP", lambda sample: sample[:4] == b"RIFF" and sample[8:12] == b"WEBP"
    ),
    _BrowserImage("image/avif", "AVIF", lambda sample: sample[4:12] in {b"ftypavif", b"ftypavis"}),
)
BROWSER_IMAGE_FORMATS = frozenset(image.pillow_format for image in _BROWSER_IMAGES)
"""浏览器内核能直接显示的 Pillow 格式名（``image_preview`` 据此决定原文件能否直接给浏览器）。"""
_TIFF_SIGNATURES = (b"II*\x00", b"MM\x00*")

_RAW_PHOTO_SUFFIXES = frozenset(
    {".dng", ".nef", ".nrw", ".cr2", ".cr3", ".arw", ".orf", ".rw2", ".raf", ".pef", ".srw"}
)

# 音视频：后缀 → MIME。资源响应的 Content-Type 取自这里（带 nosniff），跨机器一致。
_MEDIA: dict[str, tuple[FileKind, str]] = {
    ".mp3": ("audio", "audio/mpeg"),
    ".wav": ("audio", "audio/wav"),
    ".m4a": ("audio", "audio/mp4"),
    ".aac": ("audio", "audio/aac"),
    ".flac": ("audio", "audio/flac"),
    ".ogg": ("audio", "audio/ogg"),
    ".oga": ("audio", "audio/ogg"),
    ".opus": ("audio", "audio/ogg"),
    ".mp4": ("video", "video/mp4"),
    ".m4v": ("video", "video/mp4"),
    ".mov": ("video", "video/quicktime"),
    ".webm": ("video", "video/webm"),
    ".mkv": ("video", "video/x-matroska"),
    ".avi": ("video", "video/x-msvideo"),
}

# 需要内容是文本的类型：后缀 → (kind, MIME)。
_TEXT: dict[str, tuple[FileKind, str]] = {
    ".md": ("markdown", "text/markdown"),
    ".markdown": ("markdown", "text/markdown"),
    ".mdown": ("markdown", "text/markdown"),
    ".svg": ("svg", "image/svg+xml"),
    ".csv": ("table", "text/csv"),
    ".tsv": ("table", "text/tab-separated-values"),
    ".mmd": ("mermaid", TEXT_MIME),
    ".mermaid": ("mermaid", TEXT_MIME),
}


# 操作系统能出缩略图、能快速查看的格式（Office、iWork、电子书、HEIC 等）。PDF 与视频按最终类型另算，免得没有
# 后缀的 PDF 拿不到缩略图。只用于决定要不要向宿主请求缩略图、空态怎么说：系统对不认识的格式不报错，而是返回一张
# 通用的白纸图标，所以不能「都试一下」。
_SYSTEM_PREVIEW_SUFFIXES = frozenset(
    {
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".key",
        ".pages",
        ".numbers",
        ".rtf",
        ".odt",
        ".ods",
        ".odp",
        ".epub",
        ".heic",
        ".heif",
    }
)


def system_preview(path: Path, kind: FileKind) -> bool:
    """操作系统能否给出这个文件的缩略图与快速查看（按最终类型与后缀表）。"""

    return kind in {"pdf", "video"} or path.suffix.lower() in _SYSTEM_PREVIEW_SUFFIXES


def classify(path: Path, sample: bytes, encoding: str | None) -> tuple[FileKind, str]:
    """按文件头样本、后缀与解码结果给出 ``(kind, mime)``。``encoding`` 为 None 表示样本不能解码为文本。"""

    suffix = path.suffix.lower()
    if sample.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    for image in _BROWSER_IMAGES:
        if image.matches(sample):
            return "image", image.mime
    if suffix in _RAW_PHOTO_SUFFIXES:
        return "binary", BINARY_MIME
    if sample.startswith(_TIFF_SIGNATURES):
        return "image", "image/tiff"
    if suffix in _MEDIA:
        return _MEDIA[suffix]
    if encoding is None:
        return "binary", BINARY_MIME
    return _TEXT.get(suffix, ("text", TEXT_MIME))


__all__ = ["BINARY_MIME", "BROWSER_IMAGE_FORMATS", "TEXT_MIME", "classify", "system_preview"]
