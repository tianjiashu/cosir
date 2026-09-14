from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

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
    service._tasks = SimpleNamespace(
        get_task=lambda _task_id: SimpleNamespace(workspace_id=7),
        list_tasks_for_workspace=lambda _workspace_id: [],
    )
    service._workspaces = SimpleNamespace(
        get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(root))
    )
    service._runs = SimpleNamespace(list_runs_for_task=lambda _task_id: [])
    return service


def test_delete_protects_hash_referenced_by_another_task_in_same_workspace(tmp_path: Path) -> None:
    asset_id = "a" * 64
    service = _service(tmp_path)
    service._tasks.list_tasks_for_workspace = lambda _workspace_id: [
        SimpleNamespace(id=7),
        SimpleNamespace(id=8),
    ]
    service._runs.list_runs_for_task = lambda task_id: [
        SimpleNamespace(image_paths=[f".cosir/Attachment/{asset_id}.png"])
    ] if task_id == 8 else []
    attachment_dir = tmp_path / ".cosir" / "Attachment"
    attachment_dir.mkdir(parents=True)
    (attachment_dir / f"{asset_id}.png").write_bytes(b"image")

    try:
        service.delete(7, asset_id)
    except ImageNormalizationError as error:
        assert error.code == "ATTACHMENT_NOT_OWNED"
    else:
        raise AssertionError("a shared image must not be deleted")
    assert (attachment_dir / f"{asset_id}.png").exists()


async def test_upload_uses_sha256_as_asset_id_and_deduplicates_bytes(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = await service.upload(7, _image_upload())
    second = await service.upload(7, _image_upload("renamed.png"))

    expected = hashlib.sha256(
        next(iter((tmp_path / ".cosir" / "Attachment" / ".uploading").glob("*.png"))).read_bytes()
    ).hexdigest()
    assert first.asset_id == second.asset_id == expected
    assert len(list((tmp_path / ".cosir" / "Attachment" / ".uploading").glob("*.png"))) == 1
    assert len(first.asset_id) == 64
    assert first.asset_id == first.asset_id.lower()


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
