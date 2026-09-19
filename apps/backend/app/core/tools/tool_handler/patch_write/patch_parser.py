"""Parse Git unified diffs into existing-file update hunks.

This module accepts only one-file-per-section Git unified diffs. It rejects file
creation, deletion, rename, copy, binary, and mode-change metadata before callers
resolve paths or touch the workspace.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError


@dataclass(frozen=True)
class HunkLine:
    """One context, removed, or added line from a unified-diff hunk."""

    prefix: str
    content: str


@dataclass(frozen=True)
class Hunk:
    """A parsed text hunk, with the original lines used for content matching."""

    context_hint: str | None = None
    lines: list[HunkLine] = field(default_factory=list)
    source_start: int = 0
    target_start: int = 0
    source_length: int = 0
    target_length: int = 0
    new_no_newline_at_eof: bool = False


@dataclass(frozen=True)
class PatchOperation:
    """One content-only update to an existing workspace-relative file."""

    file_path: str
    hunks: list[Hunk]


def _split_sections(patch: str) -> tuple[list[str], str | None]:
    """Split a patch at Git file headers and reject ambiguous preambles."""

    lines = patch.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts or starts[0] != 0:
        return [], "expected a Git 'diff --git' header at the start of the patch"
    if any(
        line.startswith(("diff --cc ", "diff --combined "))
        for line in lines
        if line.startswith("diff --") and not line.startswith("diff --git ")
    ):
        return [], "combined diffs are not supported"
    return [
        "".join(lines[start : starts[index + 1] if index + 1 < len(starts) else len(lines)])
        for index, start in enumerate(starts)
    ], None


def _decode_git_path(value: str, prefix: str) -> str:
    """Decode a Git C-quoted path and return its workspace-relative path."""

    value = value.strip()
    if value.startswith('"'):
        if not value.endswith('"') or len(value) < 2:
            raise ValueError("malformed quoted path")
        try:
            raw = codecs.escape_decode(value[1:-1].encode("utf-8"))[0]
            value = raw.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("quoted path is not valid UTF-8") from exc
    if not value.startswith(prefix):
        raise ValueError(f"expected a {prefix!r} workspace-relative path")
    path = value[len(prefix) :]
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("path must be a normalized workspace-relative file path")
    return path


def _diff_header_paths(section: str, section_index: int) -> tuple[str, str, str | None]:
    """Read two Git header paths, including C-quoted names with spaces."""

    header = section.splitlines()[0]
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[^ \t]+', header[len("diff --git ") :])
    if len(tokens) != 2:
        return "", "", f"file section {section_index + 1} has a malformed Git header"
    try:
        source = _decode_git_path(tokens[0], "a/")
        target = _decode_git_path(tokens[1], "b/")
    except ValueError as exc:
        return "", "", f"file section {section_index + 1}: {exc}"
    if source != target:
        return "", "", "each diff section must modify one existing file without renaming it"
    return source, target, None


def _validate_section_headers(section: str) -> str | None:
    """Validate the Git metadata and required unified-diff file headers."""

    lines = section.splitlines()
    if not lines or not lines[0].startswith("diff --git "):
        return "each file section must begin with 'diff --git'"
    if any(
        line.startswith(
            (
                "new file mode ",
                "deleted file mode ",
                "rename from ",
                "rename to ",
                "copy from ",
                "copy to ",
                "old mode ",
                "new mode ",
                "GIT binary patch",
                "Binary files ",
            )
        )
        for line in lines
    ):
        return (
            "file creation, deletion, move, copy, mode changes, and binary patches "
            "are not supported"
        )
    if any(line.startswith("diff --") and not line.startswith("diff --git ") for line in lines):
        return "only Git file diffs are supported; combined diffs are not supported"

    hunks = [index for index, line in enumerate(lines) if line.startswith("@@ ")]
    first_hunk = hunks[0] if hunks else len(lines)
    old_headers = [
        index for index, line in enumerate(lines[:first_hunk]) if line.startswith("--- ")
    ]
    new_headers = [
        index for index, line in enumerate(lines[:first_hunk]) if line.startswith("+++ ")
    ]
    if len(old_headers) != 1 or len(new_headers) != 1 or not hunks:
        return (
            "each file section must contain exactly one '---', one '+++', and at least one "
            "'@@' hunk"
        )
    if not (1 <= old_headers[0] < new_headers[0] < hunks[0]):
        return "unified-diff file headers must appear before the hunks"

    # Git's index line is the only extended metadata accepted. Reject unknown metadata
    # instead of allowing the parser to silently ignore file operations.
    for line in lines[1 : old_headers[0]]:
        if line and not line.startswith("index "):
            return f"unsupported Git diff metadata: {line}"
    for line in lines[hunks[0] + 1 :]:
        if line and line[0] not in {" ", "+", "-", "\\", "@"}:
            return "unexpected text outside a unified-diff hunk"
    return None


def _normalize_parser_paths(
    sections: list[str],
) -> tuple[list[str], list[str], str | None]:
    """Replace variable Git paths with safe parser labels after validating headers."""

    normalized: list[str] = []
    paths: list[str] = []
    for index, section in enumerate(sections):
        source, _, error = _diff_header_paths(section, index)
        if error:
            return [], [], error
        lines = section.splitlines(keepends=True)
        old_index = next(i for i, line in enumerate(lines) if line.startswith("--- "))
        new_index = next(i for i, line in enumerate(lines) if line.startswith("+++ "))
        old_value = lines[old_index][4:].rstrip("\r\n").split("\t", 1)[0]
        new_value = lines[new_index][4:].rstrip("\r\n").split("\t", 1)[0]
        try:
            old_path = _decode_git_path(old_value, "a/")
            new_path = _decode_git_path(new_value, "b/")
        except ValueError as exc:
            return [], [], f"file section {index + 1}: {exc}"
        if old_path != source or new_path != source:
            return [], [], "the Git, '---', and '+++' headers must name the same existing file"

        label = f"codex_patch_file_{index}"
        lines[0] = f"diff --git a/{label} b/{label}\n"
        lines[old_index] = f"--- a/{label}\n"
        lines[new_index] = f"+++ b/{label}\n"
        normalized.append("".join(lines))
        paths.append(source)
    return normalized, paths, None


def parse_git_unified_diff(patch: str) -> tuple[list[PatchOperation], str | None]:
    """Parse a Git unified diff without performing filesystem access.

    The parser validates Git file sections and hunk lengths. It returns only UPDATE
    operations; callers remain responsible for workspace containment and file checks.
    """

    if not isinstance(patch, str) or not patch.strip():
        return [], "patch must be a non-empty Git unified diff"
    normalized_input = patch if patch.endswith("\n") else patch + "\n"
    sections, error = _split_sections(normalized_input)
    if error:
        return [], error
    for section in sections:
        error = _validate_section_headers(section)
        if error:
            return [], error
    normalized_sections, paths, error = _normalize_parser_paths(sections)
    if error:
        return [], error
    try:
        parsed = PatchSet.from_string("".join(normalized_sections))
    except (UnidiffParseError, ValueError, IndexError) as exc:
        return [], f"invalid unified diff: {exc}"
    if len(parsed) != len(sections):
        return [], "unified diff contains an incomplete or ambiguous file section"

    operations: list[PatchOperation] = []
    seen_paths: set[str] = set()
    for file, path in zip(parsed, paths, strict=True):
        if file.is_binary_file or not file:
            return [], "binary patches and file sections without text hunks are not supported"
        if file.source_file == "/dev/null" or file.target_file == "/dev/null":
            return [], "file creation and deletion are not supported by apply_patch"
        if path in seen_paths:
            return [], f"duplicate diff section for {path!r}"
        seen_paths.add(path)

        converted_hunks: list[Hunk] = []
        for parsed_hunk in file:
            converted: list[HunkLine] = []
            new_no_newline = False
            previous_line_type: str | None = None
            for line in parsed_hunk:
                if line.line_type == "\\":
                    if previous_line_type in {" ", "+"}:
                        new_no_newline = True
                elif line.line_type in {" ", "+", "-"}:
                    value = line.value[:-1] if line.value.endswith("\n") else line.value
                    converted.append(HunkLine(line.line_type, value))
                    previous_line_type = line.line_type
            if not converted:
                return [], f"{path}: hunk has no context or changed lines"
            if not any(line.prefix in {"+", "-"} for line in converted):
                return [], f"{path}: hunk does not change file contents"
            converted_hunks.append(
                Hunk(
                    context_hint=parsed_hunk.section_header or None,
                    lines=converted,
                    source_start=parsed_hunk.source_start,
                    target_start=parsed_hunk.target_start,
                    source_length=parsed_hunk.source_length,
                    target_length=parsed_hunk.target_length,
                    new_no_newline_at_eof=new_no_newline,
                )
            )
        operations.append(PatchOperation(file_path=path, hunks=converted_hunks))

    if not operations:
        return [], "patch contains no file updates"
    return operations, None
