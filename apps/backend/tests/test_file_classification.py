"""文件类型判定：魔数先行、后缀表是唯一事实源、文本类型要求能解码，不读本机类型表。"""

import io
from pathlib import Path

import pytest
from PIL import Image

from app.service.workspace_files.file_classification import (
    BINARY_MIME,
    TEXT_MIME,
    classify,
    system_preview,
)

TEXT = b"name,score\nA,1\n"


@pytest.mark.parametrize(
    ("name", "kind", "mime"),
    [
        ("data.csv", "table", "text/csv"),
        ("data.TSV", "table", "text/tab-separated-values"),
        ("flow.mmd", "mermaid", TEXT_MIME),
        ("flow.mermaid", "mermaid", TEXT_MIME),
        ("notes.md", "markdown", "text/markdown"),
        ("icon.svg", "svg", "image/svg+xml"),
        # 目的：本机类型表把 .ts 判成 MPEG 传输流；后缀表里没有它，按能否解码兜底成文本。
        ("app.ts", "text", TEXT_MIME),
        ("notebook.ipynb", "text", TEXT_MIME),
        ("README", "text", TEXT_MIME),
    ],
)
def test_text_types_come_from_the_suffix_table(name: str, kind: str, mime: str) -> None:
    assert classify(Path(name), TEXT, "utf-8") == (kind, mime)


# 潜在缺陷：后缀表里的文本类型在内容是二进制时仍按表格、Mermaid 打开，解析出一屏乱码。
def test_text_types_require_decodable_content() -> None:
    assert classify(Path("data.csv"), b"\x00\x01", None) == ("binary", BINARY_MIME)
    assert classify(Path("blob"), b"\x00\x01", None) == ("binary", BINARY_MIME)


@pytest.mark.parametrize(
    ("name", "kind", "mime"),
    [
        ("a.mp3", "audio", "audio/mpeg"),
        ("a.wav", "audio", "audio/wav"),
        ("a.m4a", "audio", "audio/mp4"),
        ("a.opus", "audio", "audio/ogg"),
        ("a.mp4", "video", "video/mp4"),
        ("a.MOV", "video", "video/quicktime"),
        ("a.webm", "video", "video/webm"),
        ("a.avi", "video", "video/x-msvideo"),
    ],
)
def test_media_follows_the_suffix_even_when_the_sample_decodes(
    name: str, kind: str, mime: str
) -> None:
    # 目的：字符集探测偶尔把二进制「解码」成功，音视频不能因此退成文本；播不了由前端降级兜底。
    for encoding in ("utf-8", None):
        assert classify(Path(name), b"ID3\x03", encoding) == (kind, mime)


def test_magic_numbers_win_over_suffixes() -> None:
    png = io.BytesIO()
    Image.new("RGB", (1, 1)).save(png, format="PNG")
    assert classify(Path("picture.csv"), png.getvalue(), None) == ("image", "image/png")
    assert classify(Path("report.txt"), b"%PDF-1.7", "utf-8") == ("pdf", "application/pdf")
    assert classify(Path("scan.dat"), b"II*\x00rest", None) == ("image", "image/tiff")
    assert classify(Path("scan.tif"), b"MM\x00*rest", None) == ("image", "image/tiff")


# 潜在缺陷：相机 RAW 以 TIFF 头开头，被当成 TIFF 转码后只出一张很小的内嵌缩略图，像是预览坏了。
@pytest.mark.parametrize("name", ["photo.dng", "photo.NEF", "photo.cr2", "photo.arw"])
def test_camera_raw_is_binary_despite_the_tiff_header(name: str) -> None:
    assert classify(Path(name), b"II*\x00rest", None) == ("binary", BINARY_MIME)


# 目的：只对系统认识的格式向宿主要缩略图；潜在缺陷：对压缩包、未知格式也请求，界面显示一张通用白纸图标当缩略图。
@pytest.mark.parametrize(
    ("name", "kind", "expected"),
    [
        ("report.DOCX", "binary", True),
        ("deck.key", "binary", True),
        ("photo.heic", "binary", True),
        ("scan", "pdf", True),
        ("clip.webm", "video", True),
        ("archive.zip", "binary", False),
        ("tone.mp3", "audio", False),
        ("notes.md", "markdown", False),
    ],
)
def test_system_preview_follows_the_final_kind_and_the_suffix_table(
    name: str, kind: str, expected: bool
) -> None:
    assert system_preview(Path(name), kind) is expected  # type: ignore[arg-type]
