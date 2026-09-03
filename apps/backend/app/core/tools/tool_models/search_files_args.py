"""search_files 工具的 Pydantic 参数模型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchFilesArgs(BaseModel):
    """search_files 工具接受的校验参数（对齐 Hermes SEARCH_FILES_SCHEMA）。

    字段：
        pattern: content 模式为正则表达式；files 模式为 glob 模式（如 ``*.py``）。
        target: ``content`` 搜文件内容 / ``files`` 按文件名查找（默认 content）。
        path: 可选的基于项目根的子目录（默认 None，表示项目根）。
        file_glob: content 模式下的文件名 glob 过滤（默认 None）。
        limit: 单页最大结果条数（默认 50）。
        offset: 跳过前 N 条结果用于分页（默认 0）。
        output_mode: content 模式的输出格式 content / files_only / count。
        context: content 模式下命中行前后的上下文行数（默认 0）。

    校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    pattern: str = Field(
        description=(
            "Regex pattern for content search, or glob pattern (e.g., '*.py') for file search."
        )
    )
    target: Literal["content", "files"] = Field(
        default="content",
        description=(
            "'content' searches inside file contents, 'files' searches for files by name."
        ),
    )
    path: str | None = Field(default=None, description="Optional subdirectory to scope the search.")
    file_glob: str | None = Field(
        default=None,
        description="Filter files by pattern in content mode (e.g., '*.py').",
    )
    limit: int = Field(default=50, ge=1, description="Maximum number of results to return.")
    offset: int = Field(default=0, ge=0, description="Skip first N results for pagination.")
    output_mode: Literal["content", "files_only", "count"] = Field(
        default="content",
        description=(
            "Output format for content mode: 'content' shows matching lines with line "
            "numbers, 'files_only' lists file paths, 'count' shows match counts per file."
        ),
    )
    context: int = Field(
        default=0,
        ge=0,
        description="Number of context lines before and after each match (content mode only).",
    )
