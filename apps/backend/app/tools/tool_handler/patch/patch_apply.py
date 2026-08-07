"""patch 两阶段校验与应用。

把 ``patch_parser`` 产出的 ``PatchOperation`` 列表先整体校验（路径合法、目标
存在/不存在符合预期、hunk 上下文可匹配），校验通过后再逐文件应用（Add/Update/
Delete/Move）。校验失败整体不落盘；但应用阶段为逐文件顺序落盘，中途异常不保证
已落盘文件自动回滚（已知限制，见风险清单）。

设计边界：
- 只做 patch 解析与应用，不关心工具权限。
- hunk 上下文匹配复用 ``fuzzy_match.fuzzy_find_and_replace``。
- 路径合法性委托 ``ProjectPathResolver``；落盘复用 ``file_io.atomic_write``。
"""

import os
from pathlib import Path

from app.tools.tool_handler.file_io.atomic_write import atomic_write_text
from app.tools.tool_handler.patch.fuzzy_match import format_no_match_hint, fuzzy_find_and_replace
from app.tools.tool_handler.patch.patch_diff import FileDiffResult
from app.tools.tool_handler.patch.patch_parser import (
    Hunk,
    OperationType,
    PatchOperation,
)
from app.tools.tool_handler.security.path_resolver import PathResolver


class PatchApplyError(RuntimeError):
    """表示 patch 应用阶段失败，并标记此前是否已有操作落盘。"""

    def __init__(self, message: str, *, partial_applied: bool) -> None:
        """初始化 patch 应用错误。

        参数:
            message: 原始失败说明。
            partial_applied: 失败前是否已有操作成功落盘。

        返回:
            无。

        异常:
            无。

        副作用:
            仅保存错误消息和部分应用标记。
        """

        super().__init__(message)
        self.partial_applied = partial_applied


def _resolve_patch_path(resolver: PathResolver, path: str) -> tuple[Path | None, str]:
    """解析并校验 patch header 中的单个路径。

    参数:
        resolver: workspace 路径解析器。
        path: patch header 中的原始路径。

    返回:
        ``(resolved, "")`` 表示成功；``(None, error)`` 表示设备路径或越界路径。

    异常:
        无。

    副作用:
        无。
    """

    device_error = resolver.blocked_device_reason(path)
    if device_error:
        return None, device_error
    resolved, error = resolver.resolve_within_workspace(path)
    if resolved is None:
        return None, error
    device_error = resolver.blocked_device_reason(path, resolved)
    if device_error:
        return None, device_error
    return resolved, ""


def _hunk_search(hunk: Hunk) -> str:
    """从 hunk 构造查找文本（上下文 + 删除行）。

    参数:
        hunk: 待处理的 hunk。

    返回:
        以换行连接的查找文本。

    异常:
        无。

    副作用:
        无。
    """

    return "\n".join(line.content for line in hunk.lines if line.prefix in {" ", "-"})


def _hunk_replace(hunk: Hunk) -> str:
    """从 hunk 构造替换文本（上下文 + 新增行）。

    参数:
        hunk: 待处理的 hunk。

    返回:
        以换行连接的替换文本。

    异常:
        无。

    副作用:
        无。
    """

    return "\n".join(line.content for line in hunk.lines if line.prefix in {" ", "+"})


def validate_all(
    operations: list[PatchOperation],
    resolver: PathResolver,
) -> list[str]:
    """校验全部 patch 操作而不落盘。

    参数:
        operations: 待校验的 patch 操作列表。
        resolver: 项目路径解析器，提供路径合法性校验。

    返回:
        错误字符串列表；空列表表示全部合法，可进入应用阶段。

    异常:
        无。

    副作用:
        仅读取文件内容做模拟匹配，不修改文件系统。
    """

    errors: list[str] = []

    for op in operations:
        if op.operation == OperationType.ADD:
            resolved, err = _resolve_patch_path(resolver, op.file_path)
            if resolved is None:
                errors.append(f"{op.file_path}: {err}")
            elif os.path.lexists(resolved):
                errors.append(f"{op.file_path}: destination already exists")

        elif op.operation == OperationType.UPDATE:
            resolved, err = _resolve_patch_path(resolver, op.file_path)
            if resolved is None:
                errors.append(f"{op.file_path}: {err}")
                continue
            try:
                content = Path(resolved).read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"{op.file_path}: cannot read file: {exc}")
                continue

            simulated = content
            for index, hunk in enumerate(op.hunks, start=1):
                search = _hunk_search(hunk)
                replace = _hunk_replace(hunk)
                if not search and not replace:
                    errors.append(
                        f"{op.file_path}: hunk {index} is empty (no context, additions "
                        f"or deletions) — remove it or provide real hunk lines"
                    )
                    continue
                new_sim, count, _, match_err = fuzzy_find_and_replace(
                    simulated, search, replace, replace_all=False
                )
                if count == 0:
                    msg = f"{op.file_path}: hunk {index} not found"
                    if match_err:
                        msg += f" — {match_err}"
                    msg += format_no_match_hint(match_err, count, search, simulated)
                    errors.append(msg)
                else:
                    simulated = new_sim

        elif op.operation == OperationType.DELETE:
            resolved, err = _resolve_patch_path(resolver, op.file_path)
            if resolved is None:
                errors.append(f"{op.file_path}: {err}")
            elif not Path(resolved).exists():
                errors.append(f"{op.file_path}: file not found for deletion")

        elif op.operation == OperationType.MOVE:
            src, err_src = _resolve_patch_path(resolver, op.file_path)
            dst, err_dst = _resolve_patch_path(resolver, op.new_path or "")
            if src is None:
                errors.append(f"{op.file_path}: {err_src}")
            elif not Path(src).exists():
                errors.append(f"{op.file_path}: source not found for move")
            if dst is None:
                errors.append(f"{op.new_path}: {err_dst}")
            elif os.path.lexists(dst):
                errors.append(f"{op.new_path}: destination already exists — move would overwrite")

    return errors


def apply_all(
    operations: list[PatchOperation],
    resolver: PathResolver,
) -> None:
    """在校验通过后逐文件应用 patch 操作。

    参数:
        operations: 待应用的 patch 操作列表（应先经 ``validate_all`` 校验通过）。
        resolver: 项目路径解析器，提供路径合法性校验。

    返回:
        无。

    异常:
        RuntimeError: 当应用阶段 hunk 匹配意外失败时抛出（理论上校验已保证不失败，
            仅用于防御竞态；调用方应捕获并转为工具错误）。
        OSError: 当文件写入/删除/移动失败时向上抛出。

    副作用:
        按 Update/Add/Delete/Move 逐文件修改文件系统；中途失败不回滚已落盘文件。

    注意:
        本函数是 :func:`apply_all_with_diff` 的薄封装，仅丢弃 before/after 快照、
        保留 ``-> None`` 契约，便于不关心回显的调用方复用同一套应用逻辑。
    """

    apply_all_with_diff(operations, resolver)


def apply_all_with_diff(
    operations: list[PatchOperation],
    resolver: PathResolver,
) -> list[FileDiffResult]:
    """在校验通过后逐文件应用 patch 操作并捕获 before/after 快照。

    参数:
        operations: 待应用的 patch 操作列表（应先经 ``validate_all`` 校验通过）。
        resolver: 项目路径解析器，提供路径合法性校验。

    返回:
        ``FileDiffResult`` 列表：每个操作一条（Move 含 ``new_path``），供调用方
        投影 unified diff 回显与结构化统计。顺序与 ``operations`` 一致。

    异常:
        RuntimeError: 当应用阶段 hunk 匹配意外失败时抛出（理论上校验已保证不失败，
            仅用于防御竞态；调用方应捕获并转为工具错误）。
        OSError: 当文件写入/删除/移动失败时向上抛出。

    副作用:
        按 Update/Add/Delete/Move 逐文件修改文件系统；中途失败不回滚已落盘文件。
    """

    results: list[FileDiffResult] = []
    for op in operations:
        try:
            results.append(_apply_operation(op, resolver))
        except (OSError, RuntimeError) as exc:
            raise PatchApplyError(str(exc), partial_applied=bool(results)) from exc

    return results


def _apply_operation(operation: PatchOperation, resolver: PathResolver) -> FileDiffResult:
    """应用单个已校验的 patch 操作并返回差异快照。

    参数:
        operation: 已通过整体预校验的 patch 操作。
        resolver: workspace 路径解析器。

    返回:
        对应本次操作的 ``FileDiffResult``。

    异常:
        RuntimeError: 当竞态导致目标状态变化或 hunk 不再匹配时抛出。
        OSError: 当文件系统读写失败时抛出。

    副作用:
        修改、创建、删除或移动一个 workspace 内文件。
    """

    if operation.operation == OperationType.ADD:
        resolved, _ = resolver.resolve_within_workspace(operation.file_path)
        assert resolved is not None
        if os.path.lexists(resolved):
            raise RuntimeError(f"{operation.file_path}: destination already exists")
        # 优先使用显式完整内容（content，保留原始尾换行 / CRLF / BOM），
        # 缺失时回退到从 hunks 拼接的 '+' 行（不保留尾换行，仅防御路径）。
        if operation.content is not None:
            content = operation.content
        else:
            content = "\n".join(
                line.content
                for hunk in operation.hunks
                for line in hunk.lines
                if line.prefix == "+"
            )
        atomic_write_text(
            Path(resolved),
            content,
            containment_root=resolver.workspace_root,
        )
        return FileDiffResult(
            path=operation.file_path,
            status="added",
            before="",
            after=content,
        )

    if operation.operation == OperationType.DELETE:
        resolved, _ = resolver.resolve_within_workspace(operation.file_path)
        assert resolved is not None
        before = Path(resolved).read_text(encoding="utf-8")
        _require_same_resolution(resolver, operation.file_path, resolved)
        Path(resolved).unlink()
        return FileDiffResult(
            path=operation.file_path,
            status="deleted",
            before=before,
            after="",
        )

    if operation.operation == OperationType.MOVE:
        src, _ = resolver.resolve_within_workspace(operation.file_path)
        dst, _ = resolver.resolve_within_workspace(operation.new_path or "")
        assert src is not None and dst is not None
        if os.path.lexists(dst):
            raise RuntimeError(f"{operation.new_path}: destination already exists")
        before = Path(src).read_text(encoding="utf-8")
        _require_same_resolution(resolver, operation.file_path, src)
        _require_same_resolution(resolver, operation.new_path or "", dst)
        os.replace(src, dst)
        return FileDiffResult(
            path=operation.file_path,
            status="moved",
            before=before,
            after=before,
            new_path=operation.new_path or "",
        )

    resolved, _ = resolver.resolve_within_workspace(operation.file_path)
    assert resolved is not None
    # 显式完整内容（content，整文件目标态）优先：直接覆盖还原，绕开 fuzzy 行匹配，
    # 正确处理「after 为空（清空）/ before 为空（整文件新增）」等整文件变更场景，
    # 避免空 search 被跳过导致虚假成功。
    if operation.content is not None:
        before = Path(resolved).read_text(encoding="utf-8")
        atomic_write_text(
            Path(resolved),
            operation.content,
            containment_root=resolver.workspace_root,
        )
        return FileDiffResult(
            path=operation.file_path,
            status="modified",
            before=before,
            after=operation.content,
        )
    before = Path(resolved).read_text(encoding="utf-8")
    content = before
    for hunk in operation.hunks:
        search = _hunk_search(hunk)
        replace = _hunk_replace(hunk)
        if search == replace:
            continue
        if not search:
            # 空 search（hunk 仅含 '+' 行）表示整文件内容替换为 replace，
            # 用于「把文件清空后再还原为原内容」等整文件覆盖场景，不可跳过。
            content = replace
            continue
        new_content, count, _, error = fuzzy_find_and_replace(
            content, search, replace, replace_all=False
        )
        if count == 0:
            raise RuntimeError(
                f"{operation.file_path}: hunk apply failed after validation"
                + (f" — {error}" if error else "")
            )
        content = new_content
    atomic_write_text(
        Path(resolved),
        content,
        containment_root=resolver.workspace_root,
    )
    return FileDiffResult(
        path=operation.file_path,
        status="modified",
        before=before,
        after=content,
    )


def _require_same_resolution(
    resolver: PathResolver,
    path: str,
    expected: Path,
) -> None:
    """在实际文件变更前确认路径解析结果未发生变化。

    参数:
        resolver: workspace 路径解析器。
        path: patch 中的原始路径。
        expected: 预校验或读取阶段得到的解析路径。

    返回:
        无。

    异常:
        RuntimeError: 路径越界或解析结果发生变化时抛出。

    副作用:
        仅读取路径元数据。
    """

    current, error = resolver.resolve_within_workspace(path)
    if current is None or error or current != expected:
        raise RuntimeError(f"{path}: path changed before mutation")
