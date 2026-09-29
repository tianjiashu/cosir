from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from starlette.datastructures import UploadFile

from app.service.attachment.attachment_service import AttachmentService
from app.service.attachment.image_normalizer import ImageNormalizationError


def _image_upload(name: str = "image.png") -> UploadFile:
    stream = BytesIO()
    Image.new("RGB", (4, 3), (20, 40, 60)).save(stream, format="PNG")
    stream.seek(0)
    return UploadFile(file=stream, filename=name, headers=None)


def _service(root: Path) -> AttachmentService:
    service = AttachmentService.__new__(AttachmentService)
    service._workspaces = SimpleNamespace(
        get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(root))
    )
    return service


async def test_upload_uses_sha256_as_asset_id_and_deduplicates_bytes(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = await service.upload(7, _image_upload())
    second = await service.upload(7, _image_upload("renamed.png"))

    expected = hashlib.sha256(
        next(iter((tmp_path / ".cosir" / "Attachment").glob("*.png"))).read_bytes()
    ).hexdigest()
    assert first.asset_id == second.asset_id == expected
    assert len(list((tmp_path / ".cosir" / "Attachment").glob("*.png"))) == 1
    assert not list((tmp_path / ".cosir" / "Attachment" / ".uploading").glob("*"))
    assert len(first.asset_id) == 64
    assert first.asset_id == first.asset_id.lower()


async def test_upload_resizes_image_before_publishing_final_attachment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    stream = BytesIO()
    Image.new("RGB", (10_000, 5_000), (20, 40, 60)).save(stream, format="JPEG")
    stream.seek(0)

    uploaded = await service.upload(
        7,
        UploadFile(file=stream, filename="large.jpg", headers=None),
    )

    assert uploaded.status == "ready"
    assert uploaded.width == 8_192
    assert uploaded.height == 4_096
    assert (tmp_path / ".cosir" / "Attachment" / f"{uploaded.asset_id}.jpeg").is_file()
    assert not list((tmp_path / ".cosir" / "Attachment" / ".uploading").glob("*"))


async def test_upload_rejects_gif_and_webp_inputs(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for image_format, suffix in (("GIF", ".gif"), ("WEBP", ".webp")):
        stream = BytesIO()
        Image.new("RGB", (4, 3), (20, 40, 60)).save(stream, format=image_format)
        stream.seek(0)
        upload = UploadFile(file=stream, filename=f"image{suffix}", headers=None)

        with pytest.raises(ImageNormalizationError) as error:
            await service.upload(7, upload)

        assert error.value.code == "ATTACHMENT_TYPE_UNSUPPORTED"


async def test_upload_rejects_non_image_bytes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    stream = BytesIO(b"not an image")
    upload = UploadFile(file=stream, filename="notes.txt", headers=None)

    try:
        await service.upload(7, upload)
    except Exception as error:
        assert getattr(error, "code", None) == "ATTACHMENT_IMAGE_INVALID"
    else:
        raise AssertionError("non-image upload should fail")
