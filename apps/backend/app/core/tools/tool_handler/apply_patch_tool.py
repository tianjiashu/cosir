"""apply_patch 工具实现（从原合并 patch_tool 的 patch_write 模式平移）。

本模块只承载 apply_patch（V4A 多文件补丁）这一个工具：解析并应用 V4A 补丁，复刻
原 apply_patch 逻辑。成功后返回 unified diff 回显（``content``）与结构化 diff 统计
（``display_data["diff_stats"]``），对齐 Hermes ``patch_tool`` 的 patch_write 分支。落盘后逐文件
经 ``guard.syntax_check`` 做多语言语法检查；语法诊断作为成功结果中的模型侧后续
修复提示，不改变已经落盘的文件变更展示状态。应用阶段的异常按瞬态文件系统错误、
确定性失败和部分落盘失败分类，分别填充 ``retryable``、``error`` 与 ``reason``。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``。
- patch_write 的解析/应用复用 ``patch_write.patch_parser`` / ``patch_write.patch_apply``。
- 成功/失败观察统一经 ``tool_execute.tool_success`` / ``tool_error`` 工厂构造。
- 语法检查委托 ``guard.syntax_check``（多语言单一来源），不内联校验。
"""

import errno
import os

from app.core.tools.display.file_change_display import (
    build_file_change_artifact_data,
    build_file_change_display_data,
)
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
from app.core.tools.tool_handler.patch_write.atomic_write import looks_like_line_numbered
from app.core.tools.tool_handler.patch_write import (
    PatchApplyError,
    apply_all_with_diff,
    parse_v4a_patch,
    validate_all,
)
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.apply_patch_args import ApplyPatchArgs

APPLY_PATCH_DESCRIPTION = (
    "Apply V4A multi-file patches for bulk changes. "
    "REQUIRED PARAMETER: patch_write (V4A patch_write content). "
    "Auto-runs syntax checks after editing.\n\n"
    "PATCH MODE: apply a V4A patch_write that can update, add, delete, or move multiple files "
    "in one call. Each operation references a file path and a diff/hunk block."
)


def _is_patch_retryable_after_correction(error: PatchApplyError) -> bool:
    """判断 patch_write 失败后是否允许模型修正或处理后再次调用。

    ``retryable`` 只是模型提示，不触发执行器自动重试，也不要求使用完全相同的
    patch_write。内容/路径竞态需要重新读取并生成新 patch_write，因此属于可修正后重试；已经
    部分落盘的失败始终不允许模型直接重放。

    参数:
        error: patch_write 应用阶段归一化后的异常。

    返回:
        未发生部分落盘且失败可以通过等待或修正当前状态后再次调用时返回 ``True``。

    异常:
        无。

    副作用:
        无。
    """

    if error.partial_applied:
        return False
    cause = error.__cause__
    if isinstance(cause, RuntimeError):
        return True
    if not isinstance(cause, OSError):
        return False
    if os.name == "nt" and getattr(cause, "winerror", None) in {32, 33}:
        return True
    return cause.errno in {
        errno.EAGAIN,
        errno.EBUSY,
        getattr(errno, "ETXTBSY", -1),
    }


class ApplyPatchTool(HandlerBase):
    """解析并应用 V4A 多文件补丁的工具类（原 patch_write 工具的 patch_write 模式）。

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
            patch_write: V4A 格式 patch_write 文本（必填，输入仅 V4A）。

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
                tool_name=self.name,
                error="missing patch_write input",
                reason="provide a non-empty V4A patch_write in the 'patch_write' argument.",
                retryable=True,
                permission=self.permission,
            )
        text = patch.strip()
        if looks_like_line_numbered(text):
            return tool_error(
                tool_name=self.name,
                error="patch_write contains line-number prefixes",
                reason="remove the 'N| ' display prefixes and provide the actual V4A patch_write.",
                retryable=True,
                permission=self.permission,
            )
        operations, parse_error = parse_v4a_patch(patch)
        if parse_error:
            return tool_error(
                tool_name=self.name,
                error=f"invalid V4A patch_write: {parse_error}",
                reason="fix the V4A headers and hunk ranges, then submit a new patch_write.",
                retryable=True,
                permission=self.permission,
            )
        if not operations:
            return tool_error(
                tool_name=self.name,
                error="patch_write contains no file operations",
                reason="add at least one update, add, delete, or move operation.",
                retryable=True,
                permission=self.permission,
            )
        validation_errors = validate_all(operations, resolver)
        if validation_errors:
            return tool_error(
                tool_name=self.name,
                error="patch_write validation failed (no files were modified):\n"
                + "\n".join(f"  • {e}" for e in validation_errors),
                reason="fix the listed operations before submitting a new patch_write.",
                retryable=True,
                permission=self.permission,
            )
        try:
            results = apply_all_with_diff(operations, resolver)
        except PatchApplyError as exc:
            return self._patch_apply_error_observation(exc)
        # 展示数据先基于文件变更事实构造；语法检查只作为模型侧诊断，不改变 UI 成功状态。
        display_data = build_file_change_display_data(results)
        artifact_data = build_file_change_artifact_data(results)
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
                diagnostics_all.extend(check.diagnostics)
        if diagnostics_all:
            return tool_success(
                tool_name=self.name,
                permission=self.permission,
                content=(
                    "success\nPost-write syntax check reported issues:\n"
                    + self._format_multi_file_syntax_reason(diagnostics_all)
                ),
                display_data=display_data,
                artifact_data=artifact_data,
            )
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=None,
            display_data=display_data,
            artifact_data=artifact_data,
        )

    def _patch_apply_error_observation(self, error: PatchApplyError) -> ToolObservation:
        """把 patch_write 应用异常转换为不重复诊断信息的工具错误观察。

        ``error`` 只描述已经发生的事实；``reason`` 只描述模型下一步应采取的动作。
        未落盘的文件锁/忙碌状态，以及可通过重新读取状态修正的内容竞态，允许模型
        处理后再次调用。部分落盘失败必须先检查当前文件状态，禁止模型盲目重放。

        参数:
            error: patch_write 应用阶段异常，可能携带底层异常 cause 和部分落盘标记。

        返回:
            ``status="error"`` 的 ``ToolObservation``，并正确填充 ``retryable``。

        异常:
            无。

        副作用:
            无。
        """

        if error.partial_applied:
            return tool_error(
                tool_name=self.name,
                error="patch_write application stopped after partial changes",
                reason=(
                    "inspect the changed files and create a new patch_write from the current "
                    "contents; do not replay this patch_write unchanged."
                ),
                retryable=False,
                permission=self.permission,
            )
        if _is_patch_retryable_after_correction(error):
            return tool_error(
                tool_name=self.name,
                error=f"patch_write apply failed: {error}",
                reason=(
                    "resolve the reported condition or regenerate the patch_write from current "
                    "file contents before retrying."
                ),
                retryable=True,
                permission=self.permission,
            )
        return tool_error(
            tool_name=self.name,
            error=f"patch_write apply failed: {error}",
            reason=(
                "re-read the affected files and create a new patch_write for the current contents; "
                "do not retry this patch_write unchanged."
            ),
            retryable=False,
            permission=self.permission,
        )

    def _format_multi_file_syntax_reason(self, diagnostics: list[SyntaxDiagnostic]) -> str:
        """聚合多个文件的语法诊断为英文后续修复提示。

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
                "(write_file or patch_write or apply_patch)."
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
            "(write_file or patch_write or apply_patch) that corrects the "
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
                show_result=False,
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
