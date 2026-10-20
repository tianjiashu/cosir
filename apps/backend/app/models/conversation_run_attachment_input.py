"""Conversation Run 输入阶段的普通附件引用。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConversationRunAttachmentInput:
    """一条用户输入中的普通本机文件引用。

    ``path`` 在首次发送时由 Transport 提供；编辑重发可以省略，由输入 service 从
    目标消息的展示绑定恢复。该输入值对象不保存文件内容，也不负责判断
    路径是否存在。
    """

    id: str
    name: str
    content_type: str
    path: str | None = None


__all__ = ["ConversationRunAttachmentInput"]
