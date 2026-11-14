"""文件编辑工具（``write_file`` / ``edit_file`` / ``apply_patch``）共用的内部件。

``apply_patch`` 的解析与应用只接受 Git unified diff 的内容修改；原文匹配、diff 展示与行号污染检测
三个工具共用。
"""

from app.tools.tool_handler.file_edit.patch_apply import (
    PatchApplyError,
    apply_all_with_diff,
    validate_all,
)
from app.tools.tool_handler.file_edit.patch_diff import (
    FileDiffResult,
    build_diff_stats,
    format_git_diff,
    format_unified_diff,
)
from app.tools.tool_handler.file_edit.patch_parser import (
    Hunk,
    HunkLine,
    PatchOperation,
    PatchParseOutcome,
    parse_git_unified_diff,
    parse_git_unified_diff_detailed,
)

__all__ = [
    "FileDiffResult",
    "Hunk",
    "HunkLine",
    "PatchApplyError",
    "PatchOperation",
    "PatchParseOutcome",
    "apply_all_with_diff",
    "build_diff_stats",
    "format_git_diff",
    "format_unified_diff",
    "parse_git_unified_diff",
    "parse_git_unified_diff_detailed",
    "validate_all",
]
