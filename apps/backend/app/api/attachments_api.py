"""workspace 本地附件 HTTP 接口。"""

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.app import app
from app.service.attachment.attachment_service import AttachmentService
from app.service.attachment.image_normalizer import ImageNormalizationError


def _raise_attachment_error(error: ImageNormalizationError) -> None:
    status = (
        404
        if error.code == "ATTACHMENT_NOT_FOUND"
        else 409
        if error.code == "ATTACHMENT_NOT_OWNED"
        else 503
        if error.code == "ATTACHMENT_STORAGE_UNAVAILABLE"
        else 400
    )
    raise HTTPException(status_code=status, detail={"code": error.code, "message": error.message})


@app.post("/tasks/{task_id}/attachments")
async def upload_attachment(
    task_id: int,
    file: UploadFile = File(...),
) -> dict[str, object]:
    """上传并暂存一张图片；相同字节由 SHA-256 自动复用。"""
    try:
        descriptor = await AttachmentService().upload(task_id, file)
    except ImageNormalizationError as exc:
        _raise_attachment_error(exc)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return {
        "id": descriptor.asset_id,
        "name": descriptor.name,
        "contentType": descriptor.content_type,
        "byteSize": descriptor.byte_size,
        "width": descriptor.width,
        "height": descriptor.height,
        "locator": descriptor.locator,
        "status": descriptor.status,
    }


@app.delete("/tasks/{task_id}/attachments/{asset_id}", status_code=204)
async def delete_attachment(task_id: int, asset_id: str) -> None:
    try:
        AttachmentService().delete(task_id, asset_id)
    except ImageNormalizationError as exc:
        _raise_attachment_error(exc)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.get("/tasks/{task_id}/attachments/{asset_id}/content")
async def get_attachment_content(task_id: int, asset_id: str) -> FileResponse:
    try:
        path, content_type = AttachmentService().resolve_content(task_id, asset_id)
    except ImageNormalizationError as exc:
        _raise_attachment_error(exc)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return FileResponse(path, media_type=content_type, headers={"Cache-Control": "no-store"})
