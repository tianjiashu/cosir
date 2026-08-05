"""patch 工具实现（合并 edit_file 与 apply_patch）。

本模块只承载 patch 这一个工具，按 ``mode`` 分流为 replace（单文件模糊替换，
原 edit_file）与 patch（V4A 多文件补丁，原 apply_patch）两种能力。成功后统一
返回 unified diff 回显（``content``）与结构化 diff 统计（``data["diff_stats"]``），
对齐 Hermes ``patch_tool``。落盘后经 ``guard.syntax_check`` 做多语言语法检查
（error 驱动）：命中语法错误返回 error 观察（文件已写），经 ``reason`` 引导
Agent 二次编辑覆盖自修复。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``。
- replace 的模糊匹配复用 ``patch.fuzzy_match``，patch 的解析/应用复用
  ``patch.patch_parser`` / ``patch.patch_apply``。
- 成功/失败观察统一经 ``tool_execute.tool_success`` / ``tool_error`` 工厂构造。
- 语法检查委托 ``guard.syntax_check``（多语言单一来源），不内联校验。
"""

import dataclasses
from pathlib import Path

from app.tools.guard.syntax_check import SyntaxDiagnostic, check_source_syntax, format_syntax_reason
from app.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_error import (
    blocked_device_reason,
    os_error_message,
    tool_error,
)
from app.tools.tool_execute.tool_success import tool_success
from app.tools.tool_handler.file_io.atomic_write import atomic_write_text, looks_like_line_numbered
from app.tools.tool_handler.patch import (
    PatchApplyError,
    apply_all_with_diff,
    format_no_match_hint,
    format_patch_diff,
    fuzzy_find_and_replace,
    parse_v4a_patch,
    validate_all,
)
from app.tools.tool_handler.patch.file_change_display import (
    build_file_change_display_data,
)
from app.tools.tool_handler.patch.patch_diff import FileDiffResult
from app.tools.tool_handler.security.project_path import ProjectPathResolver
from app.tools.tool_handler.tool_base import HandlerBase
from app.tools.tool_models.patch_args import PatchArgs


def _format_multi_file_syntax_reason(diagnostics: list[SyntaxDiagnostic]) -> str:
    """聚合多个文件的语法诊断为英文 reason（patch 模式多文件自修复引导）。

    参数:
        diagnostics: 各文件语法诊断的扁平集合（含 ``language`` / ``row`` /
            ``column`` / ``expected`` 字段）。

    返回:
        面向模型的英文 ``reason``：列出每个语法错误的位置与缺失 token，引导
        Agent 逐文件二次编辑覆盖修复。

    异常:
        无。

    副作用:
        无。
    """
    if not diagnostics:
        return (
            "the patched file(s) have syntax errors; fix them with follow-up edits "
            "(write_file or patch_tool)."
        )
    parts = [
        f"line {d.row} col {d.column}"
        + (f" missing '{d.expected}'" if d.expected else " unexpected token")
        for d in diagnostics
    ]
    return (
        "the patched file(s) have syntax errors ("
        + "; ".join(parts)
        + "); the files have been written but are not valid. Fix each with a follow-up "
        "edit (write_file or patch_tool) that corrects the syntax at the reported location."
    )


PATCH_DESCRIPTION = (
    "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
    "Uses fuzzy matching (9 strategies) so minor whitespace/indentation "
    "differences won't break it. "
    "Returns a unified diff. Auto-runs syntax checks after editing.\n\n"
    "REPLACE MODE (mode='replace', default): find a unique string and replace it. "
    "REQUIRED PARAMETERS: mode, path, old_string, new_string.\n"
    "PATCH MODE (mode='patch'): apply V4A multi-file patches for bulk changes. "
    "REQUIRED PARAMETERS: mode, patch."
)


class PatchTool(HandlerBase):
    """在文件内做查找替换或应用 V4A 补丁的合并工具。

    参数:
        无。

    返回:
        ``PatchTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存项目根与解析器；不读取、不写入文件。
    """

    name = "patch"
    description = PATCH_DESCRIPTION
    permission = "file_write"
    args_model = PatchArgs
    timeout_seconds = 30.0
    risk_level = "medium"

    def __init__(self) -> None:
        """初始化 patch 工具实例。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            仅保存工具元数据，不执行文件系统操作。
        """

    def execute(
        self,
        execution_context: ToolExecutionContext,
        mode: str = "replace",
        path: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        patch: str | None = None,
    ) -> ToolObservation:
        """按 mode 分流执行 replace 或 patch，统一收口成功/失败观察。

        参数:
            mode: 编辑模式，``replace``（默认）或 ``patch``。
            path: replace 模式的目标文件相对路径。
            old_string: replace 模式的待查找文本。
            new_string: replace 模式的替换文本。
            replace_all: replace 模式是否替换所有命中。
            patch: patch 模式的 V4A 文本。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                由执行链在执行期强制注入，handler 契约必须接受此 kwarg。
                破坏性操作以其 ``workspace_root`` 作为路径 containment 的唯一事实源。

        返回:
            ``ToolObservation``：成功时 ``content`` 为 unified diff 回显、
            ``data["diff_stats"]`` 为结构化统计；失败时 ``status`` 为 error，
            ``error``/``reason`` 提供面向模型的
            富文本诊断（``error``=发生了什么、``reason``=为什么失败+如何修正+是否重试）。

        异常:
            不主动向上抛出；所有失败路径均归一化为错误观察。

        副作用:
            命中时原子回写目标文件；patch 模式按操作逐文件修改文件系统。
        """
        resolver = ProjectPathResolver(execution_context.workspace_root)
        if mode == "replace":
            return self._execute_replace(path, old_string, new_string, replace_all, resolver)
        if mode == "patch":
            return self._execute_patch(patch, resolver)
        return tool_error(
            self.name,
            f"unknown mode: {mode}",
            reason=(
                "the mode argument must be exactly 'replace' or 'patch'; any other value "
                "is rejected. Pass mode='replace' (with path/old_string/new_string) or "
                "mode='patch' (with patch); the same unknown mode will always fail."
            ),
            permission=self.permission,
        )

    def _execute_replace(
        self,
        path: str | None,
        old_string: str | None,
        new_string: str | None,
        replace_all: bool,
        resolver: ProjectPathResolver,
    ) -> ToolObservation:
        """replace 模式：单文件模糊查找替换，复刻原 edit_file 逻辑。

        参数:
            path: 目标文件相对路径（必填）。
            old_string: 待查找文本（必填）。
            new_string: 替换文本（必填）。
            replace_all: 是否替换所有命中。
            resolver: 路径解析与 containment 权威，执行期取自
                ``execution_context.workspace_root``。

        返回:
            ``ToolObservation``；成功经 :func:`tool_success` 返回 unified diff 回显，
            失败经 :func:`tool_error` 返回（reason 见 :meth:`execute`）。

        异常:
            不主动向上抛出。

        副作用:
            命中时原子回写目标文件。
        """

        if not path or old_string is None or new_string is None:
            return tool_error(
                self.name,
                "mode='replace' requires path, old_string, new_string",
                reason=(
                    "replace mode needs all of path, old_string and new_string; at least "
                    "one is missing. Provide a target file path, the exact text to find, "
                    "and its replacement; the same incomplete arguments will always fail."
                ),
                permission=self.permission,
            )
        if old_string == new_string:
            return tool_error(
                self.name,
                "old_string and new_string are identical, no changes would be made",
                reason=(
                    "old_string and new_string are exactly the same, so this edit is a "
                    "no-op and the file would not change. This is deterministic: provide "
                    "a new_string that differs from old_string, or skip the call if no "
                    "change is actually needed."
                ),
                permission=self.permission,
            )
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("patched"),
                permission=self.permission,
            )
        resolved, error = resolver.resolve(path)
        if resolved is None:
            return tool_error(
                self.name,
                f"could not patch the file: {error}",
                reason=(
                    "the path escapes the project workspace and cannot be patched. The "
                    "resolver rejects paths that point outside the workspace root for "
                    "safety. Pass a path inside the project (relative to the workspace "
                    "root, or an absolute path under it); the same out-of-bounds path will "
                    "always be rejected."
                ),
                permission=self.permission,
            )
        device_error = resolver.blocked_device_reason(path, resolved)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("patched"),
                permission=self.permission,
            )
        try:
            original = Path(resolved).read_text(encoding="utf-8")
        except OSError as exc:
            return tool_error(
                self.name,
                os_error_message(exc, "read the file"),
                reason=(
                    "the file could not be read before patching, usually because it is "
                    "locked by another process or the current user lacks read permission. "
                    "Close the program holding the file or adjust permissions, then retry "
                    "the same patch."
                ),
                retryable=True,
                permission=self.permission,
            )
        new_content, count, _, match_error = fuzzy_find_and_replace(
            original, old_string, new_string, replace_all
        )
        if count == 0:
            message = match_error or "Could not find a match for old_string in the file"
            message += format_no_match_hint(match_error, count, old_string, original)
            return tool_error(
                self.name,
                message,
                reason=(
                    "old_string was not found in the file (or occurred more than once "
                    "without replace_all). Show the exact current text via read_file and "
                    "retry with an old_string that matches uniquely; the same old_string "
                    "will always fail until the file content changes."
                ),
                permission=self.permission,
            )
        if looks_like_line_numbered(new_content):
            return tool_error(
                self.name,
                "resulting content appears line-numbered; remove 'N| ' prefixes first.",
                reason=(
                    "the patched content looks like line-numbered read_file output (most "
                    "lines start with a 'N| ' prefix). These prefixes are display metadata, "
                    "not file content. Strip the 'N| ' prefix from the new_string and retry; "
                    "the same content will always be rejected."
                ),
                permission=self.permission,
            )
        try:
            atomic_write_text(
                resolved,
                new_content,
                containment_root=resolver.workspace_root,
            )
        except OSError as exc:
            return tool_error(
                self.name,
                os_error_message(exc, "write the file"),
                reason=(
                    "the patched file could not be written, usually because it is locked "
                    "by another process or the current user lacks write permission. Close "
                    "the program holding the file or adjust permissions, then retry the "
                    "same patch."
                ),
                retryable=True,
                permission=self.permission,
            )
        # 落盘后语法检查（error 驱动）：命中语法错误返回 error 观察（文件已写），
        # 经 reason 引导 Agent 二次编辑覆盖自修复。
        result = check_source_syntax(str(resolved), new_content)
        if result.has_error:
            return tool_error(
                tool_name=self.name,
                error="syntax error detected after patch",
                reason=format_syntax_reason(result),
                permission=self.permission,
                display_data={"syntax_errors": [dataclasses.asdict(d) for d in result.diagnostics]},
            )
        snapshot = FileDiffResult(path=path, status="modified", before=original, after=new_content)
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=format_patch_diff([snapshot]),
            data=build_file_change_display_data([snapshot]),
        )

    def _execute_patch(self, patch: str | None, resolver: ProjectPathResolver) -> ToolObservation:
        """patch 模式：解析并应用 V4A 补丁，复刻原 apply_patch 逻辑。

        参数:
            patch: V4A 格式 patch 文本（必填，输入仅 V4A）。
            resolver: 路径解析与 containment 权威，执行期取自
                ``execution_context.workspace_root``。

        返回:
            ``ToolObservation``；成功经 :func:`tool_success` 返回 unified diff 回显
            与 diff 统计，失败经 :func:`tool_error` 返回（reason 见 :meth:`execute`）。

        异常:
            不主动向上抛出。

        副作用:
            校验通过后逐文件修改文件系统。
        """

        if not patch:
            return tool_error(
                self.name,
                "mode='patch' requires patch (V4A)",
                reason=(
                    "patch mode needs the 'patch' argument containing a non-empty V4A "
                    "patch. Provide the V4A patch text; the same empty patch will always "
                    "fail."
                ),
                permission=self.permission,
            )
        text = patch.strip()
        if looks_like_line_numbered(text):
            return tool_error(
                self.name,
                "patch appears to be line-numbered read_file output; remove the 'N| ' "
                "prefixes before applying.",
                reason=(
                    "the patch text looks like line-numbered read_file output (most lines "
                    "start with a 'N| ' prefix). These prefixes are display metadata, not "
                    "patch content. Strip the 'N| ' prefix from the patch and retry; the "
                    "same content will always be rejected."
                ),
                permission=self.permission,
            )
        operations, parse_error = parse_v4a_patch(patch)
        if parse_error:
            return tool_error(
                self.name,
                parse_error,
                reason=(
                    "the V4A patch could not be parsed (syntax error in the patch text). "
                    "Fix the patch format (correct headers, valid hunk ranges) and retry; "
                    "the same malformed patch will always fail."
                ),
                permission=self.permission,
            )
        if not operations:
            return tool_error(
                self.name,
                "patch contains no operations",
                reason=(
                    "the patch parsed successfully but contains no file operations. Add "
                    "at least one update/add/delete/move operation to the patch and retry; "
                    "the same empty patch will always fail."
                ),
                permission=self.permission,
            )
        validation_errors = validate_all(operations, resolver)
        if validation_errors:
            return tool_error(
                self.name,
                "Patch validation failed (no files were modified):\n"
                + "\n".join(f"  • {e}" for e in validation_errors),
                reason=(
                    "patch validation failed, so no files were modified. The error list "
                    "above shows which operations were rejected (e.g. paths outside the "
                    "workspace or unsupported operations). Fix the listed operations and "
                    "retry; the same invalid patch will always fail."
                ),
                permission=self.permission,
            )
        try:
            results = apply_all_with_diff(operations, resolver)
        except PatchApplyError as exc:
            return tool_error(
                self.name,
                f"patch apply failed: {exc}",
                reason=(
                    "the patch could not be fully applied"
                    + (
                        " (some operations were already applied before the failure)"
                        if exc.partial_applied
                        else ""
                    )
                    + ". This is usually a transient write/lock issue or a conflicting "
                    "concurrent edit. Resolve the conflict or free the file, then retry "
                    "the same patch."
                ),
                retryable=True,
                permission=self.permission,
            )
        # 落盘后逐文件语法检查（error 驱动）：任一文件命中语法错误即返回 error 观察，
        # 聚合所有错误诊断（带文件维度），经 reason 引导 Agent 逐文件二次编辑覆盖自修复。
        syntax_errors: list[dict[str, object]] = []
        diagnostics_all: list[SyntaxDiagnostic] = []
        # 仅对产生新内容的文件（modified/added）做语法检查；deleted/moved 无新内容可查。
        for r in results:
            if r.status not in ("modified", "added"):
                continue
            resolved_path, _ = resolver.resolve(r.path)
            if resolved_path is None:
                continue
            check = check_source_syntax(str(resolved_path), r.after)
            if check.has_error:
                syntax_errors.append(
                    {
                        "path": r.path,
                        "errors": [dataclasses.asdict(d) for d in check.diagnostics],
                    }
                )
                diagnostics_all.extend(check.diagnostics)
        if syntax_errors:
            return tool_error(
                tool_name=self.name,
                error="syntax error detected in patched file(s)",
                reason=_format_multi_file_syntax_reason(diagnostics_all),
                permission=self.permission,
                display_data={"syntax_errors": syntax_errors},
            )
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=format_patch_diff(results),
            data=build_file_change_display_data(results),
        )

    def to_definition(self) -> ToolDefinition:
        """把工具实例转换成 ``ToolDefinition``。

        参数:
            无。

        返回:
            可直接注册到 ``ToolRegistry`` 的工具定义（含 display 展示元数据）。

        异常:
            无。

        副作用:
            无。
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("filesystem",),
            display=ToolDisplayHints(
                verb="",
                icon="git-compare",
                expandable=True,
                expand_layout="diff",
            ),
        )


def build_patch_definition() -> ToolDefinition:
    """构造 patch 工具定义。

    参数:
        无。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``PatchTool`` 实例和定义对象，不执行文件操作。
    """

    return PatchTool().to_definition()
