"""Validate and apply content-only unified-diff updates to existing files."""

from __future__ import annotations

from pathlib import Path

from app.tools.file_system.facts.file_mutation import FileMutation
from app.tools.file_system.facts.file_version import FileVersion
from app.tools.file_system.path_resolver import PathResolver
from app.tools.file_system.write_policy import WriteTarget
from app.tools.tool_handler.file.file_edit.patch_diff import FileDiffResult
from app.tools.tool_handler.file.file_edit.patch_parser import Hunk, PatchOperation
from app.tools.tool_handler.file.file_edit.text_match import TextMatchError, replace_exact
from app.utils.atomic_write import atomic_write_text


class PatchApplyError(RuntimeError):
    """应用中途失败；``applied`` 是失败前已经写入的文件（按顺序），空表示一个都没写。"""

    def __init__(self, message: str, *, applied: list[FileDiffResult]) -> None:
        super().__init__(message)
        self.applied = applied


def resolve_patch_path(resolver: PathResolver, path: str) -> tuple[Path | None, str]:
    """Resolve a parser-validated workspace-relative path and block devices."""

    device_error = resolver.blocked_device_reason(path)
    if device_error:
        return None, device_error
    resolved, error = resolver.resolve_for_write(path)
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
    """按序应用各块：有上下文的块按原文唯一命中定位（见 ``text_match``），不看块头行号。"""

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
            try:
                content, _ = replace_exact(content, search, replace)
            except TextMatchError as exc:
                raise RuntimeError(f"{operation.file_path}: hunk {index} not found — {exc}") from exc
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


def prepare_patch(
    operations: list[PatchOperation], resolver: PathResolver,
) -> tuple[dict[str, WriteTarget], list[str]]:
    """解析并校验补丁的各个目标，保留校验时的路径与版本供写入检查。"""

    errors: list[str] = []
    targets: dict[str, WriteTarget] = {}
    for operation in operations:
        resolved, error = resolve_patch_path(resolver, operation.file_path)
        if resolved is None:
            errors.append(f"{operation.file_path}: {error}")
            continue
        version = FileVersion.of(resolved)
        if version is None:
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
        targets[operation.file_path] = WriteTarget(resolved, "modify", version)
    return targets, errors


def apply_all_with_diff(
    operations: list[PatchOperation],
    resolver: PathResolver,
    targets: dict[str, WriteTarget],
) -> list[FileDiffResult]:
    """Apply validated updates in order and return the before/after text for display."""

    results: list[FileDiffResult] = []
    for operation in operations:
        try:
            resolved, error = resolve_patch_path(resolver, operation.file_path)
            if resolved is None:
                raise RuntimeError(f"{operation.file_path}: {error}")
            planned = targets[operation.file_path]
            version = FileVersion.of(resolved)
            if resolved != planned.path or version != planned.version:
                raise RuntimeError(f"{operation.file_path}: target changed since validation")
            before = _normalize_text(resolved.read_bytes().decode("utf-8"))
            after = _apply_hunks(before, operation)
            current, current_error = resolver.resolve_for_write(operation.file_path)
            if current is None or current_error or current != resolved or FileVersion.of(current) != version:
                raise RuntimeError(f"{operation.file_path}: target changed before mutation")
            atomic_write_text(
                resolved,
                after,
                containment_root=resolver.write_containment(resolved),
            )
            results.append(
                FileDiffResult(
                    path=operation.file_path,
                    status="modified",
                    before=before,
                    after=after,
                    mutation=FileMutation(
                        path=str(resolved), operation="modify", before=version, after=FileVersion.of(resolved),
                    ),
                )
            )
        except (OSError, RuntimeError, UnicodeDecodeError) as exc:
            raise PatchApplyError(str(exc), applied=results) from exc
    return results
