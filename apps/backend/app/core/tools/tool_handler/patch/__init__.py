"""patch 解析与应用引擎包。

本包承载 V4A 格式的解析（统一为 ``PatchOperation``）以及两阶段校验后应用
（Add/Update/Delete/Move），并提供应用结果的 unified diff 回显与统计投影。
不关心工具权限与业务语义。
"""

from app.core.tools.tool_handler.patch.fuzzy_match import (
    format_no_match_hint,
    fuzzy_find_and_replace,
)
from app.core.tools.tool_handler.patch.patch_apply import (
    PatchApplyError,
    apply_all,
    apply_all_with_diff,
    validate_all,
)
from app.core.tools.tool_handler.patch.patch_diff import (
    FileDiffResult,
    build_diff_stats,
    format_patch_diff,
    format_unified_diff,
)
from app.core.tools.tool_handler.patch.patch_parser import (
    Hunk,
    HunkLine,
    OperationType,
    PatchOperation,
    parse_v4a_patch,
)

__all__ = [
    "FileDiffResult",
    "Hunk",
    "HunkLine",
    "OperationType",
    "PatchApplyError",
    "PatchOperation",
    "apply_all",
    "apply_all_with_diff",
    "build_diff_stats",
    "format_no_match_hint",
    "format_patch_diff",
    "format_unified_diff",
    "fuzzy_find_and_replace",
    "parse_v4a_patch",
    "validate_all",
]
