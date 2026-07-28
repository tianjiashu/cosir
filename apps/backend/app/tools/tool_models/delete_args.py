"""delete 工具的 Pydantic 参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class DeleteArgs(BaseModel):
    """delete 工具接受的校验参数。

    字段：
        path: 相对项目根的文件或目录路径。
        recursive: 是否递归删除目录树；默认 ``False``（仅删空目录），删树需显式开启。

    校验边界：``extra="forbid"`` 拒绝多余字段，``strict=True`` 拒绝类型错误；
    ``min_length=1`` 保证路径非空。格式校验统一收口到 ``validation/arguments.py``。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, description="Path relative to the project root.")
    recursive: bool = Field(
        default=False, description="If true, delete a non-empty directory tree."
    )
