"""把 MCP 调用结果里的图片存成工作区附件，交给工具图片通道。

存图与格式、尺寸规则只归附件服务（``AttachmentService.save_image``，与用户上传同一套）；这里只决定
一次调用最多交出多少：张数与总字节沿用单次模型请求的上限（``Constant.Attachment``）。某张存不进去或
超出上限时只略过它、写一句说明，不让整次调用失败——服务端已经执行过了，丢掉其余结果没有意义。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.constant import Constant
from app.config.logging.logger import log
from app.service.attachment.attachment_service import AttachmentService
from app.service.attachment.image_normalizer import ImageNormalizationError
from app.tools.mcp.call_result import MCPImage

_MB = 1024 * 1024


@dataclass
class StoredResultImages:
    """存好的图片（给模型的相对路径、给卡片的 asset id）与略过时给模型的说明。"""

    paths: list[str] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def store(
        cls, images: tuple[MCPImage, ...], *, workspace_id: int, name: str, log_data: dict
    ) -> StoredResultImages:
        """逐张存图；没有图片时不构造附件服务（它要查工作区记录）。同步阻塞 IO，只在工具线程调用。"""

        stored = cls()
        if not images:
            return stored
        attachments = AttachmentService()
        limits = Constant.Attachment
        total_bytes = 0
        for index, image in enumerate(images, start=1):
            label = f"image {index} ({image.mime_type})"
            if len(stored.paths) >= limits.MAX_IMAGES_PER_REQUEST:
                stored.notes.append(
                    f"[{label} omitted: at most {limits.MAX_IMAGES_PER_REQUEST} images per call]"
                )
                continue
            try:
                saved = attachments.save_image(workspace_id, image.data, f"{name}-{index}")
            except ImageNormalizationError as exc:
                log.info(
                    "mcp_tool_image_rejected",
                    extra={
                        "msg": "MCP 工具返回的一张图片未能存为附件，已略过",
                        "data": {**log_data, "mime_type": image.mime_type, "code": exc.code},
                    },
                )
                stored.notes.append(f"[{label} omitted: it could not be stored ({exc.code})]")
                continue
            if total_bytes + saved.descriptor.byte_size > limits.REQUEST_TOTAL_MAX_BYTES:
                stored.notes.append(
                    f"[{label} omitted: the images in one call must fit in "
                    f"{limits.REQUEST_TOTAL_MAX_BYTES // _MB} MB in total]"
                )
                continue
            total_bytes += saved.descriptor.byte_size
            stored.paths.append(saved.path)
            stored.asset_ids.append(saved.descriptor.asset_id)
        return stored
