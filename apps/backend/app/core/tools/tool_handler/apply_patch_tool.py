"""apply_patch 工具实现（从原合并 patch_tool 的 patch 模式平移）。

本模块只承载 apply_patch（V4A 多文件补丁）这一个工具：解析并应用 V4A 补丁，复刻
原 apply_patch 逻辑。成功后返回 unified diff 回显（``content``）与结构化 diff 统计
（``data["diff_stats"]``），对齐 Hermes ``patch_tool`` 的 patch 分支。落盘后逐文件
经 ``guard.syntax_check`` 做多语言语法检查（error 驱动）：任一文件命中语法错误即
返回 error 观察（文件已写），聚合诊断经 ``reason`` 引导 Agent 逐文件二次编辑覆盖
自修复。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``。
- patch 的解析/应用复用 ``patch.patch_parser`` / ``patch.patch_apply``。
- 成功/失败观察统一经 ``tool_execute.tool_success`` / ``tool_error`` 工厂构造。
- 语法检查委托 ``guard.syntax_check``（多语言单一来源），不内联校验。
"""

import dataclasses

from app.core.tools.guard.syntax_check import (
    SyntaxDiagnostic,
    check_source_syntax,
)
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.file_io.atomic_write import looks_like_line_numbered
from app.core.tools.tool_handler.patch import (
    PatchApplyError,
    apply_all_with_diff,
    format_patch_diff,
    parse_v4a_patch,
    validate_all,
)
from app.core.tools.tool_handler.patch.file_change_display import (
    build_file_change_display_data,
)
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.apply_patch_args import ApplyPatchArgs

APPLY_PATCH_DESCRIPTION = (
    "Apply V4A multi-file patches for bulk changes. "
    "REQUIRED PARAMETER: patch (V4A patch content). "
    "Auto-runs syntax checks after editing.\n\n"
    "PATCH MODE: apply a V4A patch that can update, add, delete, or move multiple files "
    "in one call. Each operation references a file path and a diff/hunk block."
)


class ApplyPatchTool(HandlerBase):
    """解析并应用 V4A 多文件补丁的工具类（原 patch 工具的 patch 模式）。

    参数:
        无。

    返回:
        ``ApplyPatchTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存工具元数据；不读取、不写入文件。
    """

    name = "apply_patch"
    description = APPLY_PATCH_DESCRIPTION
    permission = "file_write"
    args_model = ApplyPatchArgs
    timeout_seconds = 30.0
    risk_level = "medium"

    def __init__(self) -> None:
        """初始化 apply_patch 工具实例。

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
        patch: str,
    ) -> ToolObservation:
        """解析并应用 V4A 补丁，统一收口成功/失败观察。

        参数:
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                由执行链在执行期强制注入，handler 契约必须接受此 kwarg。
                破坏性操作以其 ``workspace_root`` 作为路径 containment 的唯一事实源。
            patch: V4A 格式 patch 文本（必填，输入仅 V4A）。

        返回:
            ``ToolObservation``；成功经 :func:`tool_success` 返回 unified diff 回显
            与 diff 统计，失败经 :func:`tool_error` 返回（reason 见本方法内各分支）。

        异常:
            不主动向上抛出。

        副作用:
            校验通过后逐文件修改文件系统。
        """
        resolver = PathResolver(execution_context.workspace_root)
        if not patch:
            return tool_error(
                self.name,
                "apply_patch requires patch (V4A)",
                reason=(
                    "apply_patch needs the 'patch' argument containing a non-empty V4A "
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
            resolved_path, _ = resolver.resolve_within_workspace(r.path)
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
                reason=self._format_multi_file_syntax_reason(diagnostics_all),
                permission=self.permission,
                data={"syntax_errors": syntax_errors},
            )
        display_data = build_file_change_display_data(results)
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=format_patch_diff(results),
            data={"kind": "file-changes", **display_data},
            internal_data=display_data,
        )

    def _format_multi_file_syntax_reason(self, diagnostics: list[SyntaxDiagnostic]) -> str:
        """聚合多个文件的语法诊断为英文 reason（apply_patch 多文件自修复引导）。

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
                "(write_file or patch or apply_patch)."
            )
        parts = [
            f"line {d.row} col {d.column}"
            + (f" missing '{d.expected}'" if d.expected else " unexpected token")
            for d in diagnostics
        ]
        return (
            "the patched file(s) have syntax errors ("
            + "; ".join(parts)
            + "); the files have been written but are not valid. "
            "Fix each with a follow-up edit "
            "(write_file or patch or apply_patch) that corrects the "
            "syntax at the reported location."
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
                verb="应用补丁",
                icon="git-compare",
                surface="standalone",
                expandable=True,
                expand_layout="diff",
            ),
        )


def build_apply_patch_definition() -> ToolDefinition:
    """构造 apply_patch 工具定义。

    参数:
        无。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``ApplyPatchTool`` 实例和定义对象，不执行文件操作。
    """

    return ApplyPatchTool().to_definition()
