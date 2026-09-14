"""把 canonical 图片引用解析为模型 provider 的视觉输入 block。"""

from __future__ import annotations

import base64
import copy
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.config.logging.logger import log
from app.core.llm_provider.capability.model_capability import ModelCapability
from app.models.errors.llm_provider_exceptions import (
    VisionFormatNotSupportedError,
    VisionImageError,
)
from app.service.attachment.attachment_service import AttachmentService


def resolve_messages_for_model(
    messages: Sequence[BaseMessage],
    *,
    task_id: int,
    model_name: str,
    vision_input_format: str,
) -> list[BaseMessage]:
    """将自定义 `cosir_image_ref` 转换为 provider 可消费的临时 data URL。

    该函数只作用于模型请求的 deep copy，不修改 canonical context，也不写 SQLite。图片
    数量、单图大小、总大小和最长边按 ``model_capabilities.json`` 的模型限制校验。若模型
    声明不支持图片输入，则静默忽略图片 block（仅保留文本），不报错。
    """
    resolved = copy.deepcopy(list(messages))
    has_image_ref = any(
        isinstance(message, HumanMessage)
        and isinstance(message.content, list)
        and any(
            isinstance(block, dict) and block.get("type") == "cosir_image_ref"
            for block in message.content
        )
        for message in resolved
    )
    if not has_image_ref:
        return resolved
    if vision_input_format != "openai_url":
        raise VisionFormatNotSupportedError("当前 provider 的视觉输入格式尚未实现")
    # 模型是否具备图片输入能力；不具备时图片 block 直接忽略（仅保留文本），不报错。
    capability = ModelCapability.get_capability(model_name)
    vision_supported = bool(
        capability
        and capability.supports_image
        and capability.image_limit.supported_formats
    )
    if has_image_ref and not vision_supported:
        log.info(
            "vision_unsupported_images_ignored",
            extra={
                "msg": "模型不支持图片输入，已忽略图片 block，仅保留文本",
                "data": {"model_name": model_name},
            },
        )

    attachment_service = AttachmentService()
    image_count = 0
    total_bytes = 0
    image_limit = capability.image_limit if vision_supported else None
    for message in resolved:
        if not isinstance(message, HumanMessage) or not isinstance(message.content, list):
            continue
        content: list[str | dict[str, Any]] = []
        for block in message.content:
            if not isinstance(block, dict) or block.get("type") != "cosir_image_ref":
                content.append(block)
                continue
            if not vision_supported:
                # 模型不支持图片输入：忽略该图片 block，仅保留文本上下文。
                continue
            image_path = block.get("path")
            if not isinstance(image_path, str) or not image_path:
                raise VisionImageError("图片引用无效")
            normalized = attachment_service.resolve_for_model(task_id, image_path, model_name)
            image_count += 1
            total_bytes += normalized.byte_size
            if image_limit is not None and (
                image_limit.single_image_inline_max_bytes
                and normalized.byte_size > image_limit.single_image_inline_max_bytes
            ):
                raise VisionImageError("单张图片超过模型限制")
            max_side = image_limit.max_image_side_px if image_limit is not None else 0
            if image_limit is not None and (
                image_limit.many_image_threshold
                and image_count >= image_limit.many_image_threshold
                and image_limit.max_image_side_px_when_many
            ):
                max_side = image_limit.max_image_side_px_when_many
            if max_side and max(normalized.width, normalized.height) > max_side:
                raise VisionImageError("图片尺寸超过模型限制")
            encoded = base64.b64encode(normalized.path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"{normalized.content_type};base64,{encoded}"
                        if normalized.content_type.startswith("data:")
                        else f"data:{normalized.content_type};base64,{encoded}"
                    },
                }
            )
        message.content = content

    if (
        image_limit is not None
        and image_limit.max_images_per_request
        and image_count > image_limit.max_images_per_request
    ):
        raise VisionImageError("图片数量超过模型限制")
    if (
        image_limit is not None
        and image_limit.request_total_max_bytes_inline_only
        and total_bytes > image_limit.request_total_max_bytes_inline_only
    ):
        raise VisionImageError("图片总大小超过模型限制")
    return resolved


__all__ = ["resolve_messages_for_model"]
