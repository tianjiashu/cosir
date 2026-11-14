"""edit_file 的参数模型。字段说明照搬 Claude Code 的 Edit 工具，只把路径改成相对工作区根。"""

from pydantic import BaseModel, ConfigDict, Field


class EditFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(description="Path to the file to modify, relative to the workspace root.")
    old_string: str = Field(description="The text to replace")
    new_string: str = Field(description="The text to replace it with (must be different from old_string)")
    replace_all: bool = Field(default=False, description="Replace all occurrences of old_string (default false)")
