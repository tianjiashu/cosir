"""有界文件探测与分段文本读取；读取版本变化向上报告，不拼接不同版本。"""

import codecs
import mimetypes
import os
import threading
from pathlib import Path
from typing import Literal

from charset_normalizer import from_bytes

from app.service.workspace_files.file_access import regular_file
from app.service.workspace_files.file_models import FileConflictError, FileInfo, TextPage

TEXT_CHUNK_BYTES = 512 * 1024
FULL_TEXT_BYTES = 2 * 1024 * 1024
ENCODINGS = {
    "utf-8",
    "utf-8-sig",
    "utf-16-le",
    "utf-16-be",
    "utf-32-le",
    "utf-32-be",
    "gb18030",
    "big5",
    "shift_jis",
    "cp1252",
    "ascii",
}


def version_of(stat: os.stat_result) -> str:
    return "-".join(
        format(value, "x")
        for value in (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    )


def encoding_of(sample: bytes) -> str | None:
    for marker, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if sample.startswith(marker):
            return encoding
    if b"\x00" in sample:
        return None
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        match = from_bytes(sample).best()
        return match.encoding if match and match.encoding in ENCODINGS else None


def inspect_file(root: Path, path: Path) -> FileInfo:
    with regular_file(path, root) as source:
        stat = os.fstat(source.fileno())
        sample = source.read(65536)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoding = encoding_of(sample)
    kind: Literal["text", "markdown", "image", "svg", "pdf", "binary"] = (
        "text" if encoding else "binary"
    )
    if sample.startswith(b"%PDF-"):
        kind, mime = "pdf", "application/pdf"
    elif sample.startswith(b"\x89PNG\r\n"):
        kind, mime = "image", "image/png"
    elif sample.startswith(b"\xff\xd8\xff"):
        kind, mime = "image", "image/jpeg"
    elif sample.startswith((b"GIF87a", b"GIF89a")):
        kind, mime = "image", "image/gif"
    elif sample.startswith(b"BM"):
        kind, mime = "image", "image/bmp"
    elif sample[:4] == b"RIFF" and sample[8:12] == b"WEBP":
        kind, mime = "image", "image/webp"
    elif sample[4:12] in {b"ftypavif", b"ftypavis"}:
        kind, mime = "image", "image/avif"
    elif sample.startswith(b"\x00\x00\x01\x00"):
        kind, mime = "image", "image/x-icon"
    elif path.suffix.lower() == ".svg" and encoding:
        kind, mime = "svg", "image/svg+xml"
    elif path.suffix.lower() in {".md", ".markdown", ".mdown"} and encoding:
        kind = "markdown"
    return FileInfo(
        path.relative_to(root).as_posix(),
        path.name,
        str(root),
        stat.st_size,
        stat.st_mtime_ns,
        version_of(stat),
        kind,
        mime,
        encoding,
    )


def read_text(
    path: Path,
    offset: int,
    encoding: str,
    version: str | None,
    tail: bool = False,
    *,
    root: Path,
    before: int | None = None,
    cancelled: threading.Event | None = None,
) -> TextPage:
    """游标是字节位置；增量解码保留跨块字符，末段定位会寻找合法字符边界。"""
    if encoding not in ENCODINGS:
        raise ValueError("不支持的文本编码")
    with regular_file(path, root) as source:
        stat_before = os.fstat(source.fileno())
        actual = version_of(stat_before)
        if version and actual != version:
            raise FileConflictError("文件已变化，请重新读取")
        size = stat_before.st_size
        read_end = size if before is None else before
        if read_end > size:
            raise ValueError("读取位置超出文件大小")
        align_start = tail or before is not None
        start = max(0, read_end - TEXT_CHUNK_BYTES) if align_start else offset
        if start > size:
            raise ValueError("读取位置超出文件大小")
        chunk_size = FULL_TEXT_BYTES if size <= FULL_TEXT_BYTES else TEXT_CHUNK_BYTES
        if align_start and start and encoding in {"gb18030", "big5", "shift_jis"}:
            # 这些编码不是自同步编码：从中间试解码可能“成功”却落在半个字符上。
            # 从开头增量扫描到目标字节，内存有界，允许取消；绝不靠猜测吞字符。
            alignment_decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            remaining = start
            while remaining:
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError("文本请求已取消")
                prefix = source.read(min(TEXT_CHUNK_BYTES, remaining))
                if not prefix:
                    raise FileConflictError("读取时文件发生变化，请重试")
                alignment_decoder.decode(prefix, final=False)
                remaining -= len(prefix)
            start -= len(alignment_decoder.getstate()[0])
        source.seek(start)
        raw = source.read(min(chunk_size + (4 if align_start else 0), read_end - start))
        # 从末尾进入时游标不来自解码器，最多跳过一个字符宽度重新对齐。
        if align_start and start:
            alignment = 4 if "32" in encoding else 2 if "16" in encoding else 1
            skip = (-start) % alignment
            raw, start = raw[skip:], start + skip
        decoder_name = "utf-8" if start and encoding == "utf-8-sig" else encoding
        decoder = codecs.getincrementaldecoder(decoder_name)(errors="strict")
        try:
            text = decoder.decode(raw, final=start + len(raw) >= read_end)
        except UnicodeDecodeError:
            if (
                not align_start
                or not start
                or ("utf" not in encoding and encoding not in {"gb18030", "big5", "shift_jis"})
            ):
                raise
            for skip in range(1, 5):
                decoder = codecs.getincrementaldecoder(decoder_name)(errors="strict")
                try:
                    text = decoder.decode(raw[skip:], final=True)
                    start += skip
                    raw = raw[skip:]
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise
        pending = decoder.getstate()[0]
        end = start + len(raw) - len(pending)
        if version_of(os.fstat(source.fileno())) != actual or version_of(path.stat()) != actual:
            raise FileConflictError("读取时文件发生变化，请重试")
    return TextPage(
        text.removeprefix("\ufeff") if start == 0 and encoding != "utf-8-sig" else text,
        start,
        end if end < size else None,
        size,
        actual,
        encoding,
    )
