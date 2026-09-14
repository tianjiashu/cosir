"""find_files 工具参数。"""

from pydantic import BaseModel, ConfigDict, Field


class FindFilesArgs(BaseModel):
    """在单文件或递归目录中按 glob 查找文件。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    pattern: str = Field(description="File-name or workspace-relative glob, for example '*.py'.")
    path: str = Field(
        description=(
            "File or directory to search, relative to the workspace root. Use '.' for the "
            "entire workspace."
        )
    )
    limit: int = Field(default=50, ge=1, description="Maximum files to return.")
    offset: int = Field(default=0, ge=0, description="Number of matching files to skip.")
