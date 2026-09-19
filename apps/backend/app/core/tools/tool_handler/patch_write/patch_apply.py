"""Validate and apply content-only unified-diff updates to existing files."""

from __future__ import annotations

from pathlib import Path

from app.core.tools.tool_handler.patch_write.atomic_write import atomic_write_text
from app.core.tools.tool_handler.patch_write.fuzzy_match import (
    format_no_match_hint,
    fuzzy_find_and_replace,
)
from app.core.tools.tool_handler.patch_write.patch_diff import FileDiffResult
from app.core.tools.tool_handler.patch_write.patch_parser import Hunk, PatchOperation
from app.core.tools.tool_handler.security.path_resolver import PathResolver


class PatchApplyError(RuntimeError):
    """Apply failure with a flag indicating whether earlier files were written."""

    def __init__(self, message: str, *, partial_applied: bool) -> None:
        super().__init__(message)
        self.partial_applied = partial_applied


def _resolve_patch_path(resolver: PathResolver, path: str) -> tuple[Path | None, str]:
    """Resolve a parser-validated workspace-relative path and block devices."""

    device_error = resolver.blocked_device_reason(path)
    if device_error:
        return None, device_error
    resolved, error = resolver.resolve_within_workspace(path)
    if resolved is None:
        return None, error
    device_error = resolver.blocked_device_reason(path, resolved)
    return (None, device_error) if device_error else (resolved, "")


def _hunk_search(hunk: Hunk) -> str:
    return "\n".join(line.content for line in hunk.lines if line.prefix in {" ", "-"})


def _hunk_replace(hunk: Hunk) -> str:
    return "\n".join(line.content for line in hunk.lines if line.prefix in {" ", "+"})


def _insert_addition(content: str, hunk: Hunk, line_offset: int, path: str) -> str:
    """Apply a context-free insertion using its source line coordinate."""

    start = hunk.source_start + line_offset
    lines = content.splitlines()
    if start < 0 or start > len(lines):
        raise RuntimeError(f"{path}: insertion hunk is outside the current file")
    added = [line.content for line in hunk.lines if line.prefix == "+"]
    if not added:
        raise RuntimeError(f"{path}: hunk has no source context or added lines")
    updated = lines[:start] + added + lines[start:]
    result = "\n".join(updated)
    if hunk.new_no_newline_at_eof and start == len(lines):
        result = result.removesuffix("\n")
    elif start == len(lines) or content.endswith("\n"):
        result += "\n"
    return result


def _normalize_text(content: str) -> str:
    """Normalize existing newline and BOM representation for diff matching."""

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.removeprefix("\ufeff")


def _apply_hunks(content: str, operation: PatchOperation) -> str:
    """Apply validated hunks in order, retaining fuzzy matching for context."""

    line_offset = 0
    for index, hunk in enumerate(operation.hunks, start=1):
        search = _hunk_search(hunk)
        replace = _hunk_replace(hunk)
        removed = sum(line.prefix == "-" for line in hunk.lines)
        added = sum(line.prefix == "+" for line in hunk.lines)
        if not search:
            content = _insert_addition(content, hunk, line_offset, operation.file_path)
        elif search != replace:
            original_content = content
            updated, count, _, error = fuzzy_find_and_replace(
                content,
                search,
                replace,
                replace_all=False,
            )
            if count == 0:
                hint = format_no_match_hint(error, count, search, content)
                raise RuntimeError(
                    f"{operation.file_path}: hunk {index} not found"
                    + (f" — {error}" if error else "")
                    + hint
                )
            content = updated
            target_end = (
                hunk.target_start + hunk.target_length - 1
                if hunk.target_length
                else hunk.target_start
            )
            is_last_target_line = index == len(operation.hunks) and target_end == len(
                content.splitlines()
            )
            if (
                is_last_target_line
                and not hunk.new_no_newline_at_eof
                and not content.endswith("\n")
            ):
                content += "\n"
            # If a replacement removes the last line entirely, preserve the source
            # file's final-newline state unless the patch's target marker says otherwise.
            if (
                original_content.endswith("\n")
                and not content.endswith("\n")
                and not hunk.new_no_newline_at_eof
            ):
                content += "\n"
        line_offset += added - removed
        if hunk.new_no_newline_at_eof and index == len(operation.hunks) and content.endswith("\n"):
            content = content[:-1]
    return content


def validate_all(operations: list[PatchOperation], resolver: PathResolver) -> list[str]:
    """Validate every target and simulate every hunk without changing files."""

    errors: list[str] = []
    for operation in operations:
        resolved, error = _resolve_patch_path(resolver, operation.file_path)
        if resolved is None:
            errors.append(f"{operation.file_path}: {error}")
            continue
        if not resolved.is_file():
            errors.append(f"{operation.file_path}: target is not an existing regular file")
            continue
        try:
            original = _normalize_text(resolved.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{operation.file_path}: target is not a readable UTF-8 text file: {exc}")
            continue
        if "\x00" in original:
            errors.append(f"{operation.file_path}: binary files are not supported")
            continue
        try:
            _apply_hunks(original, operation)
        except (RuntimeError, ValueError) as exc:
            errors.append(str(exc))
    return errors


def apply_all_with_diff(
    operations: list[PatchOperation],
    resolver: PathResolver,
) -> list[FileDiffResult]:
    """Apply validated updates in order and return the before/after text for display."""

    results: list[FileDiffResult] = []
    for operation in operations:
        try:
            resolved, error = _resolve_patch_path(resolver, operation.file_path)
            if resolved is None:
                raise RuntimeError(f"{operation.file_path}: {error}")
            before = _normalize_text(resolved.read_bytes().decode("utf-8"))
            after = _apply_hunks(before, operation)
            current, current_error = resolver.resolve_within_workspace(operation.file_path)
            if current is None or current_error or current != resolved:
                raise RuntimeError(f"{operation.file_path}: path changed before mutation")
            atomic_write_text(
                resolved,
                after,
                containment_root=resolver.workspace_root,
            )
            results.append(
                FileDiffResult(
                    path=operation.file_path,
                    status="modified",
                    before=before,
                    after=after,
                )
            )
        except (OSError, RuntimeError, UnicodeDecodeError) as exc:
            raise PatchApplyError(str(exc), partial_applied=bool(results)) from exc
    return results
