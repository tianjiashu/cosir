"""工具产物落盘请求值对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactRequest:
    """描述需要由工具执行链统一落盘的文本产物。

    参数:
        kind: 产物类型。
        content: 需要写入的 UTF-8 文本。
        summary: 用于模型和 UI 的简短摘要。
        mime_type: 产物 MIME 类型。

    返回:
        不可变的产物创建请求。

    异常:
        无。

    副作用:
        无。实际写入只能由 ArtifactService 完成。
    """

    kind: str
    content: str
    summary: str
    mime_type: str = "text/plain"
