"""Assistant Transport 普通本机文件附件引用。"""

from pydantic import BaseModel, ConfigDict, Field


class AssistantFileAttachment(BaseModel):
    """一次用户消息中的普通文件引用。

    ``path`` 在首次发送新文件时必需；编辑重发或 snapshot 恢复后的请求可以只带
    ``id``，由后端从当前 Run 的 ``extra`` 解析已有引用。该对象只描述本机路径，
    不上传或保存文件二进制。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    contentType: str = Field(min_length=1, max_length=128)
    path: str | None = Field(default=None, min_length=1, max_length=4096)
