"""把 canonical 图片引用解析为模型 provider 的视觉输入 block。"""

from __future__ import annotations

import base64
import copy
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.models.errors.llm_provider_exceptions import VisionImageError
from app.service.attachment.attachment_service import AttachmentService


def resolve_messages_for_model(
    messages: Sequence[BaseMessage],
    *,
    workspace_id: int,
) -> list[BaseMessage]:
    """把已完成校验的图片引用解析为模型可消费的 base64 data URL。

    图片格式、尺寸、单图大小以及本次请求的聚合限制都在附件上传或 Run 创建边界完成；
    本函数只负责复制消息、检查 workspace 路径安全、读取已发布文件并完成协议转换。

    参数:
        messages: 本次要发给模型的消息序列（作为 deep copy 的来源）。
        workspace_id: 附件归属的工作区 id，用于解析工作区内的图片路径。

    返回:
        可直接发给模型的消息列表；消息里没有图片引用时返回入参的深拷贝。

    异常:
        VisionImageError: 图片引用缺路径或附件路径、文件不可用。

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

    attachment_service = AttachmentService()
    for message in resolved:
        if not isinstance(message, HumanMessage) or not isinstance(message.content, list):
            continue
        content: list[str | dict[str, Any]] = []
        for block in message.content:
            if not isinstance(block, dict) or block.get("type") != "cosir_image_ref":
                content.append(block)
                continue
            image_path = block.get("path")
            if not isinstance(image_path, str) or not image_path:
                raise VisionImageError("图片引用无效")
            stored = attachment_service.resolve_for_model(workspace_id, image_path)
            encoded = base64.b64encode(stored.path.read_bytes()).decode("ascii")
            # 统一处理可能已经带 ``data:`` 前缀的 MIME 值，避免重复拼接。
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"{stored.content_type};base64,{encoded}"
                        if stored.content_type.startswith("data:")
                        else f"data:{stored.content_type};base64,{encoded}"
                    },
                }
            )
        message.content = content
    return resolved


__all__ = ["resolve_messages_for_model"]
