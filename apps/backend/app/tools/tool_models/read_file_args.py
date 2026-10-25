"""read_file 参数校验模型；字段 description 会进入模型可见的工具 schema。"""

from pydantic import BaseModel, ConfigDict, Field


class ReadFileArgs(BaseModel):
    """定义 read_file 的参数约束；字段说明面向调用该工具的 Agent。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(
        description=(
            "For files inside the workspace, pass a path relative to the workspace root. "
            "For files outside it, pass an absolute path. Absolute paths inside the "
            "workspace are also accepted; '~' is not expanded."
        )
    )
    offset: int = Field(
        default=1,
        ge=1,
        description=(
            "1-indexed line number to start reading from; use the continuation offset "
            "reported by a previous read to resume."
        ),
    )
    limit: int = Field(default=500, ge=1, le=2000, description="Maximum number of lines to return.")
