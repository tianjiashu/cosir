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
    """把 canonical 图片引用解析为模型可消费的 base64 图片块。

    图片格式、尺寸、单图大小以及本次请求的聚合限制都在附件上传或 Run 创建边界完成；
    本函数只负责复制消息、检查 workspace 路径安全、读取已发布文件并完成协议转换。

    判据是「带**未解析** ``file_id`` 的 image 块」：已解析（已含 ``base64``）的图片原样保留，
    因此本函数可重复调用而不重复编码。转换结果随后由 provider 适配器（langchain-openai）转成
    ``image_url`` 的 data URL，故 ``mime_type`` 必须随行给出。

    参数:
        messages: 本次要发给模型的消息序列（作为 deep copy 的来源，本函数不改写入参）。
        workspace_id: 附件归属的工作区 id，用于解析工作区内的图片路径。

    返回:
        可直接发给模型的消息列表；没有未解析图片引用时返回入参的深拷贝。

    异常:
        VisionImageError: image 块的 ``file_id`` 缺失或不是非空字符串（canonical 被写坏）。
        ImageNormalizationError: 附件不存在、越过 workspace 边界或存储不可用，由
            ``AttachmentService.resolve_for_model`` 抛出，本函数原样传播不包装。

    副作用:
        读取附件文件内容并做 base64 编码（仅读磁盘）；不写任何持久化状态。
    """

    resolved = copy.deepcopy(list(messages))
    if not any(_has_unresolved_image_ref(message) for message in resolved):
        return resolved

    attachment_service = AttachmentService()
    for message in resolved:
        if not _has_unresolved_image_ref(message):
            continue
        # 透传任意块：非 image 块原样保留，只替换 image 引用（langchain 的 ContentBlock 是
        # TypedDict 联合，这里按运行期形态处理）。
        content: list[Any] = []
        for block in message.content_blocks:
            if not isinstance(block, dict) or block.get("type") != "image":
                content.append(block)
                continue
            image_path = block.get("file_id")
            if not isinstance(image_path, str) or not image_path:
                raise VisionImageError("图片引用无效")
            stored = attachment_service.resolve_for_model(workspace_id, image_path)
            content.append(
                {
                    "type": "image",
                    "base64": base64.b64encode(stored.path.read_bytes()).decode("ascii"),
                    # provider 适配器要求 base64 块带 MIME：缺它会抛
                    # ``mime_type key is required for base64 data``。
                    "mime_type": stored.content_type,
                }
            )
        # 只能写 ``content``：``content_blocks`` 是 langchain-core 的只读 property
        # （``langchain_core/messages/base.py``），赋值会抛 ``AttributeError``。
        message.content = content
    return resolved


def _has_unresolved_image_ref(message: BaseMessage) -> bool:
    """消息是否带有尚未解析的 canonical 图片引用（``file_id``）。

    本判据同时供「整批是否需要解析」与「本条消息是否需要解析」两处使用；共用同一个函数是为了
    避免两处判据漂移——历史上这两处写反，导致带图消息被跳过而纯文本消息被误处理。
    """

    if not isinstance(message, HumanMessage):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "image" and "file_id" in block
        for block in message.content_blocks
    )


__all__ = ["resolve_messages_for_model"]
