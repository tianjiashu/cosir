"""Strict workspace-relative arguments for moving one regular text file."""

from pydantic import BaseModel, ConfigDict, Field


class MoveFileArgs(BaseModel):
    """Arguments for moving one existing UTF-8 text file without changing its contents."""

    model_config = ConfigDict(strict=True, extra="forbid")

    source_path: str = Field(
        description=(
            "Workspace-relative path to one existing UTF-8 text file. The resolved source must "
            "remain inside the active workspace; directories and missing paths are rejected."
        )
    )
    destination_path: str = Field(
        description=(
            "Workspace-relative destination path inside the active workspace. Its parent "
            "directory must already exist, and the destination must not already exist."
        )
    )
