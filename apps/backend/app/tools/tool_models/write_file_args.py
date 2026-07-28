"""write_file 工具的 Pydantic 参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class WriteFileArgs(BaseModel):
    """write_file 工具接受的校验参数。

    字段：
        path: 相对项目根的文件路径。
        content: 待写入的文件内容，默认空字符串。

    校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(description="Path relative to the project root.")
    content: str = Field(default="", description="File content to write.")
