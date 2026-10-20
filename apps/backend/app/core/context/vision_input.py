"""仅在请求副本中把本机附件定位符解析为 SDK image_url data URL。"""

from __future__ import annotations

import base64
import copy
from collections.abc import Sequence

from openai.types.chat import ChatCompletionMessageParam

from app.service.attachment.attachment_service import AttachmentService


def resolve_messages_for_model(
    messages: Sequence[ChatCompletionMessageParam], *, workspace_id: int
) -> list[ChatCompletionMessageParam]:
    """canonical 只存附件定位符；归属与文件读取由 AttachmentService 边界校验。

    返回的始终是深拷贝副本：解析出的 base64 只存在于本次模型请求，不回写 context。
    副作用：命中 ``cosir-attachment://`` 时会读附件文件并做 base64 编码，属于阻塞 IO，
    调用方须在工作线程执行。
    """
    resolved = copy.deepcopy(list(messages))
    service: AttachmentService | None = None
    for message in resolved:
        if message["role"] != "user" or isinstance(message["content"], str):
            continue
        for block in message["content"]:
            if block["type"] != "image_url":
                continue
            url = block["image_url"]["url"]
            if not url.startswith("cosir-attachment://"):
                continue
            if service is None:
                service = AttachmentService()
            stored = service.resolve_for_model(
                workspace_id, url.removeprefix("cosir-attachment://")
            )
            encoded = base64.b64encode(stored.path.read_bytes()).decode("ascii")
            block["image_url"]["url"] = f"data:{stored.content_type};base64,{encoded}"
    return resolved
