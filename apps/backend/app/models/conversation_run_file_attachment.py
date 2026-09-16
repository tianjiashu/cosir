"""Conversation Run 普通本机文件附件的持久化引用。"""

from typing_extensions import TypedDict


class ConversationRunFileAttachment(TypedDict):
    """普通本机文件附件的持久化引用。

    该类型只描述附件元数据和本机路径，不保存文件二进制内容；具体的文件存在性、
    工作区边界与路径安全校验由附件 service 和 Assistant Transport service 负责。
    """

    id: str
    name: str
    content_type: str
    path: str


__all__ = ["ConversationRunFileAttachment"]
