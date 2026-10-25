"""list_directory 工具的 Pydantic 参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class ListDirectoryArgs(BaseModel):
    """list_directory 工具接受的校验参数；详细语义见各字段的 Field 描述。

    校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(
        description=(
            'Directory path relative to the project root. Use "." for the workspace root; '
            'do not leave it empty (e.g. "apps/backend" for a subfolder, "." for root).'
        )
    )
    offset: int = Field(default=0, ge=0, description="Skip first N entries for pagination.")
    limit: int = Field(
        default=200,
        ge=1,
        le=500,
        description="Maximum number of directory entries to return (must be >= 1).",
    )
    include_hidden: bool = Field(
        default=False,
        description="When true, include dot entries (names starting with '.'); "
        "skipped by default to keep directory listings concise.",
    )
    include_globs: list[str] = Field(
        default_factory=list,
        description="Only show entries whose NAME matches one of these glob patterns "
        "(e.g. '*.py', '.git'); others are hidden. Matches names only, not 'src/*.py'. "
        "Empty = no filtering (all shown, subject to include_hidden).",
    )
