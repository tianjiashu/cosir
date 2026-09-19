"""Strict workspace-relative arguments for deleting one regular file."""

from pydantic import BaseModel, ConfigDict, Field


class DeleteFileArgs(BaseModel):
    """Arguments for ``delete_file``; directory deletion is never supported."""

    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(
        description=(
            "Workspace-relative path to one existing regular file (binary and image files are "
            "allowed; the contents are never read). The resolved target must remain inside the "
            "active workspace. Directories, missing paths, absolute paths, and paths outside the "
            "workspace are rejected."
        )
    )
