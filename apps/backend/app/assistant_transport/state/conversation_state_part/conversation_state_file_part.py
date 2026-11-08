"""Conversation Transport 普通文件 part。"""

from typing import Literal

from typing_extensions import TypedDict


class ConversationStateFilePart(TypedDict):
    """普通文件 part：稳定本机 locator、展示元数据与本机路径。

    ``path`` 让撤回或排队编辑放回输入框的附件能原样再发送（发送请求必须带路径）；它是用户自己
    交给页面的本机路径，前端只把它作为附件描述交回后端，不读文件。
    """

    type: Literal["file"]
    file: str
    name: str
    contentType: str
    path: str
