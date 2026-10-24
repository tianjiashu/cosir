"""Conversation Run 普通本机文件附件的持久化引用。"""

from typing_extensions import TypedDict


class ConversationRunFileAttachment(TypedDict):
    """普通本机文件附件的持久化引用。

    该类型只描述附件元数据和本机路径，不保存文件二进制内容。``path`` 是给模型与文件工具的
    引用而非承诺：本机 Agent 的附件来自用户自己选择的本机文件，这一层不做存在性、工作区边界
    或路径安全校验，真正读取附件的地方（文件工具）才是判断失败的地方。
    """

    id: str
    name: str
    content_type: str
    path: str


__all__ = ["ConversationRunFileAttachment"]
