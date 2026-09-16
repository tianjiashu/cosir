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
    """把消息里的 ``cosir_image_ref`` 图片引用解析为 provider 可消费的 base64 data URL。

    只作用于模型请求的 deep copy：不修改 canonical context，也不写 SQLite。图片数量、单图大小、
    请求总大小与最长边按 ``model_capabilities.json`` 的模型限制校验；模型声明不支持图片输入时
    静默忽略图片 block（仅保留文本），不报错。

    参数:
        messages: 本次要发给模型的消息序列（作为 deep copy 的来源）。
        task_id: 附件归属的任务 id，用于解析工作区内的图片路径。
        model_name: 目标模型名，用于读取该模型的图片能力限制。
        vision_input_format: provider 的视觉输入格式；当前只实现 ``"openai_url"``。

    返回:
        可直接发给模型的消息列表；消息里没有图片引用时返回入参的深拷贝。

    异常:
        VisionFormatNotSupportedError: 消息带图片引用，但 ``vision_input_format`` 不是已实现的格式。
        VisionImageError: 图片引用缺路径、单图超限、尺寸超限、数量超限或请求总大小超限。

    副作用:
        读取附件文件内容并做 base64 编码（仅读磁盘）；不写任何持久化状态。
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
    # 只实现了 openai_url 一种拼装方式；由于上面已对「无图片引用」提前返回，这里报错只会在
    # 真正要发图片、且该 provider 格式未实现时发生。
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
            # 兼容层返回的 content_type 可能已带 ``data:`` 前缀，避免重复拼接。
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
