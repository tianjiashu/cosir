"""Text-patch primitives shared by file modification tools.

The apply_patch parser and engine accept Git unified-diff updates only. Atomic writes,
diff formatting, and the fuzzy hunk matcher remain shared with the existing replace and
write_file handlers.
"""

from app.core.tools.tool_handler.patch_write.fuzzy_match import (
    format_no_match_hint,
    fuzzy_find_and_replace,
)
from app.core.tools.tool_handler.patch_write.patch_apply import (
    PatchApplyError,
    apply_all_with_diff,
    validate_all,
)
from app.core.tools.tool_handler.patch_write.patch_diff import (
    FileDiffResult,
    build_diff_stats,
    format_git_diff,
    format_unified_diff,
)
from app.core.tools.tool_handler.patch_write.patch_parser import (
    Hunk,
    HunkLine,
    PatchOperation,
    parse_git_unified_diff,
)

__all__ = [
    "FileDiffResult",
    "Hunk",
    "HunkLine",
    "PatchApplyError",
    "PatchOperation",
    "apply_all_with_diff",
    "build_diff_stats",
    "format_git_diff",
    "format_no_match_hint",
    "format_unified_diff",
    "fuzzy_find_and_replace",
    "parse_git_unified_diff",
    "validate_all",
]
