"""search_content 工具参数。"""

from pydantic import BaseModel, ConfigDict, Field


class SearchContentArgs(BaseModel):
    """在单文件或递归目录中搜索正则表达式。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    pattern: str = Field(description="Regular expression to search in file contents.")
    path: str = Field(
        description=(
            "**File** or **directory** to search, relative to the workspace root. Use '.' for the "
            "entire workspace. A file searches only that file; a directory searches recursively."
        )
    )
    file_glob: str | None = Field(
        default=None,
        description="Optional file-name filter for directory searches, for example '*.py'.",
    )
    limit: int = Field(default=50, ge=1, description="Maximum matching output lines to return.")
    offset: int = Field(default=0, ge=0, description="Number of matching output lines to skip.")
    context: int = Field(
        default=0,
        ge=0,
        description="Number of context lines before and after each matching line.",
    )
