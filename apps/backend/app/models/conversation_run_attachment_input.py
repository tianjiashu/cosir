"""Conversation Run 输入阶段的普通附件引用。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConversationRunAttachmentInput:
    """一条用户输入中的普通本机文件引用。

    ``path`` 由前端随请求提供（撤回放回输入框的附件取自快照的展示绑定）。该输入值对象不保存
    文件内容，也不负责判断路径是否存在。
    """

    id: str
    name: str
    content_type: str
    path: str


__all__ = ["ConversationRunAttachmentInput"]
