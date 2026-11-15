"""把 MCP 工具调用结果（``CallToolResult``）归一化成模型能用的文本与图片。

``isError`` 是服务端正常返回的一种结果，不是异常：这里如实带出，由调用方决定怎么交给模型，不在
链路上抛异常再接住。

取舍：
- 文本优先取 content。规范要求返回 ``structuredContent`` 的工具同时在 content 里放一份序列化 JSON，
  只取 structured 会丢掉同一结果里的图片与说明文字（成熟客户端也把这种丢失当缺陷处理）；content
  里没有任何文本时才用 structured。
- 图片（含内嵌的图片资源）原样交出字节，存图与格式规则归附件服务（``AttachmentService``），这里不判断。
- 资源链接与文本资源写成文字；音频与非图片的二进制资源只说明类型，不回传载荷。
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from mcp import types


@dataclass(frozen=True)
class MCPImage:
    """结果里的一张图片：解码后的字节与服务声明的 MIME 类型。"""

    data: bytes
    mime_type: str


@dataclass(frozen=True)
class MCPCallResult:
    """一次调用的归一化结果；``is_error`` 时 ``text`` 是服务给出的错误说明，不带图片。"""

    is_error: bool
    text: str
    images: tuple[MCPImage, ...] = ()

    @classmethod
    def from_sdk(cls, result: types.CallToolResult) -> MCPCallResult:
        texts: list[str] = []
        images: list[MCPImage] = []
        for block in result.content:
            _collect(block, texts, images)
        if result.isError:
            return cls(is_error=True, text="\n".join(texts))
        if not any(text.strip() for text in texts) and result.structuredContent is not None:
            texts.insert(0, json.dumps(result.structuredContent, ensure_ascii=False, indent=2))
        return cls(is_error=False, text="\n".join(texts), images=tuple(images))


def _collect(block: types.ContentBlock, texts: list[str], images: list[MCPImage]) -> None:
    if isinstance(block, types.TextContent):
        texts.append(block.text)
    elif isinstance(block, types.ImageContent):
        _collect_image(block.data, block.mimeType, "image", texts, images)
    elif isinstance(block, types.AudioContent):
        texts.append(f"[audio content ({block.mimeType}) omitted: audio is not supported]")
    elif isinstance(block, types.ResourceLink):
        line = f"[resource link] {block.name}: {block.uri}"
        texts.append(f"{line} - {block.description}" if block.description else line)
    elif isinstance(block.resource, types.TextResourceContents):
        texts.append(f"[resource {block.resource.uri}]\n{block.resource.text}")
    else:
        mime_type = block.resource.mimeType or "application/octet-stream"
        if mime_type.startswith("image/"):
            _collect_image(block.resource.blob, mime_type, str(block.resource.uri), texts, images)
        else:
            texts.append(
                f"[resource {block.resource.uri} ({mime_type}) omitted: binary content]"
            )


def _collect_image(
    data: str, mime_type: str, label: str, texts: list[str], images: list[MCPImage]
) -> None:
    """服务端给的 base64 是进程外输入：解不开时如实告诉模型，不让整次调用失败。"""

    try:
        images.append(MCPImage(base64.b64decode(data, validate=True), mime_type))
    except (binascii.Error, ValueError):
        texts.append(f"[{label} ({mime_type}) omitted: the server sent invalid base64 data]")
