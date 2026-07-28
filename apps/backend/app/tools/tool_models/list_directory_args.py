"""list_directory 工具的 Pydantic 参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class ListDirectoryArgs(BaseModel):
    """list_directory 工具接受的校验参数。

    字段：
        path: 待列举的目录路径（相对项目根）。
        offset: 跳过前 N 个条目。
        limit: 单页最多返回的条目数。
        include_hidden: 为 true 时包含 dot 条目，默认跳过以保持清单紧凑。

    校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(description="Directory path relative to the project root.")
    offset: int = Field(default=0, ge=0, description="Skip first N entries for pagination.")
    limit: int = Field(
        default=200,
        ge=1,
        le=500,
        description="Maximum number of directory entries to return.",
    )
    include_hidden: bool = Field(
        default=False,
        description="When true, include dot entries (names starting with '.'); "
        "skipped by default to keep directory listings concise.",
    )
