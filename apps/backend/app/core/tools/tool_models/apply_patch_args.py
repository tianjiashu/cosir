"""Strict arguments for applying Git unified diffs to existing files."""

from pydantic import BaseModel, ConfigDict, Field


class ApplyPatchArgs(BaseModel):
    """Accept one Git-style unified diff that modifies existing workspace files."""

    model_config = ConfigDict(strict=True, extra="forbid")

    patch: str = Field(
        description=(
            "Required Git-style unified diff with one or more 'diff --git', '---', '+++', and "
            "'@@' sections. Use context lines prefixed with a space, removed lines prefixed with "
            "'-', and added lines prefixed with '+'. Each 'a/' and 'b/' path must identify the "
            "same existing workspace-relative UTF-8 text file. File creation, deletion, rename, "
            "copy, binary, combined diff, absolute paths, and paths outside the workspace are "
            "not supported. Use write_file to create or replace a complete file, delete_file to "
            "delete a file, or move_file to move a file."
        )
    )
