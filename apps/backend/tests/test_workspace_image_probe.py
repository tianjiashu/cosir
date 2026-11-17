"""文件信息里的图片探测：宽高是转正后的、读不出时宽高为空而不让 /files/info 失败，以及 BMP 文件头不误认文本。"""

import io
import struct
from pathlib import Path

import pytest
from PIL import Image

from app.service.workspace_files.file_content import inspect_file


def _bmp(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "teal").save(output, format="BMP")
    return output.getvalue()


# 目的：浏览器按 EXIF 方向转正显示，文件信息的宽高必须同一口径；潜在缺陷：竖拍照片报成横向尺寸，
# 面板误标「大图缩略预览」、100% 缩放比例错、单张大图卡装进横向的框。
@pytest.mark.parametrize(
    ("orientation", "expected"),
    [(1, (400, 300)), (3, (400, 300)), (6, (300, 400)), (8, (300, 400))],
)
def test_image_size_is_upright(tmp_path: Path, orientation: int, expected: tuple[int, int]) -> None:
    photo = Image.new("RGB", (400, 300), "orange")
    exif = photo.getexif()
    exif[0x0112] = orientation
    photo.save(tmp_path / "photo.jpg", exif=exif)
    info = inspect_file(tmp_path, tmp_path / "photo.jpg")
    assert (info.kind, info.width, info.height) == ("image", *expected)


# 潜在缺陷：以 BM 开头的文本（BMI 表格、BMW 笔记）被判成 BMP，短文件让 /files/info 422、面板读不了也交付不了。
@pytest.mark.parametrize(
    ("name", "content", "kind"),
    [
        ("bmi.csv", b"BMI,weight,height\n22.1,65,172\n", "table"),
        ("cars.md", b"BMW 3 series notes\n", "markdown"),
        ("notes.txt", b"BM25 ranking explained in detail\n", "text"),
    ],
)
def test_text_starting_with_bm_is_text(
    tmp_path: Path, name: str, content: bytes, kind: str
) -> None:
    (tmp_path / name).write_bytes(content)
    info = inspect_file(tmp_path, tmp_path / name)
    assert (info.kind, info.width) == (kind, None)


def test_real_bmp_is_still_an_image_with_its_size(tmp_path: Path) -> None:
    (tmp_path / "pic.bmp").write_bytes(_bmp(5, 3))
    info = inspect_file(tmp_path, tmp_path / "pic.bmp")
    assert (info.kind, info.mime, info.width, info.height) == ("image", "image/bmp", 5, 3)


# 潜在缺陷：文件头能认、解析时出错的图片让文件信息整体失败（Pillow 在插件认领之后抛普通 OSError）。
@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("cut.bmp", _bmp(5, 3)[:20]),
        ("cut.png", b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR\x00\x00"),
        ("cut.gif", b"GIF89a\x05"),
        ("cut.tif", b"II*\x00" + b"\x08\x00\x00\x00" + b"\x01"),
        ("cut.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
        ("cut.webp", b"RIFF\x10\x00\x00\x00WEBPVP8 "),
    ],
)
def test_unreadable_image_header_gives_no_size_instead_of_failing(
    tmp_path: Path, name: str, content: bytes
) -> None:
    (tmp_path / name).write_bytes(content)
    info = inspect_file(tmp_path, tmp_path / name)
    assert (info.kind, info.width, info.height) == ("image", None, None)
