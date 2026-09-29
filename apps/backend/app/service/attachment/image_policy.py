"""统一图片输入策略。

本模块只描述产品统一的图片格式、单图安全限制和请求聚合限制，不读取模型配置，也不判断
模型能力。模型是否支持图片由 ``ConversationRun`` 的用户配置快照决定。
"""

from dataclasses import dataclass

from app.config.constant import Constant


@dataclass(frozen=True, slots=True)
class ImageInputPolicy:
    """上传与 Run 创建边界共享的图片限制。"""

    supported_formats: frozenset[str]
    max_images_per_request: int
    single_image_max_bytes: int
    request_total_max_bytes: int
    max_image_side_px: int


DEFAULT_IMAGE_INPUT_POLICY = ImageInputPolicy(
    supported_formats=Constant.Attachment.IMAGE_FORMATS,
    max_images_per_request=Constant.Attachment.MAX_IMAGES_PER_REQUEST,
    single_image_max_bytes=Constant.Attachment.MAX_IMAGE_BYTES,
    request_total_max_bytes=Constant.Attachment.REQUEST_TOTAL_MAX_BYTES,
    max_image_side_px=Constant.Attachment.MAX_IMAGE_SIDE_PX,
)


__all__ = ["DEFAULT_IMAGE_INPUT_POLICY", "ImageInputPolicy"]
