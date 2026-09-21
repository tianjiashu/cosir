"""Strict arguments for applying Git unified diffs to existing files.

Field-level contract only: the accepted diff format, the hunk count rule, and the minimal example
have a single source of truth in the ``patch`` field description below. The tool description
(``apply_patch_tool.APPLY_PATCH_DESCRIPTION``) carries only the tool's responsibility and its
boundary against ``write_file`` / ``delete_file`` / ``move_file``, and never restates this format,
so the two texts cannot drift apart.
"""

from pydantic import BaseModel, ConfigDict, Field


class ApplyPatchArgs(BaseModel):
    """Accept one Git-style unified diff that modifies existing workspace files.

    The model carries the ``patch`` field only; that field's description is the single source of
    truth for the accepted diff format. A value can only modify existing UTF-8 text files inside
    the workspace: creation, deletion, rename, copy, binary and combined diffs are out of
    contract.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    patch: str = Field(
        description=(
            "Required Git-style unified diff text: the value must contain nothing but the diff, "
            "with no '*** Begin Patch' / '*** End Patch' markers and no prose around the hunks. "
            "It holds one or more 'diff --git a/<path> b/<path>' sections and accepts content "
            "hunks only: each file section must carry exactly one '---' header, exactly one "
            "'+++', and at least one '@@' hunk, must name the same workspace-relative path in "
            "its Git, '---', and '+++' headers, and must change file content. Sections that "
            "carry no content hunk (mode-only, rename-only, or binary) are rejected, and "
            "combined 'diff --cc' / 'diff --combined' sections are not supported. Inside a hunk "
            "every line starts with ' ' (context), '-' (removed), or '+' (added), and the "
            "numbers in '@@ -start,count +start,count @@' must match that hunk body exactly: the "
            "source count is that hunk's context plus '-' lines, the target count is its context "
            "plus '+' lines, and a count may be omitted only when it is 1. Keep those numbers "
            "accurate: a header whose counts disagree with its own body cannot be applied as "
            "written, so fix the counts instead of resending the same patch. Minimal accepted "
            "example:\n"
            "diff --git a/pkg/mod.py b/pkg/mod.py\n"
            "--- a/pkg/mod.py\n"
            "+++ b/pkg/mod.py\n"
            "@@ -10,3 +10,3 @@\n"
            " import os\n"
            "-old_line()\n"
            "+new_line()\n"
            " keep()\n"
        )
    )
