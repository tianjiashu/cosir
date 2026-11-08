"""Assistant Transport 普通本机文件附件引用。"""

from pydantic import BaseModel, ConfigDict, Field


class AssistantFileAttachment(BaseModel):
    """一次用户消息中的普通文件引用。

    ``path`` 是本机路径：新选的文件由宿主取得，撤回放回输入框的附件取自快照的展示绑定。该对象
    只描述本机路径，不上传或保存文件二进制。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    contentType: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=4096)
