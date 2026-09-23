"""把 Git 风格 unified diff 应用到工作区内已有的 UTF-8 文本文件。

本模块只承载 ``apply_patch`` 工具：diff 解析、路径与内容校验、补丁落盘由 ``patch_write``
各协作者完成，本模块负责编排、取消检查、写后语法检查与观察归一化。
"""

import errno
import os

from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.display.file_change_display import build_file_change_display_data
from app.core.tools.guard.syntax_check import SyntaxDiagnostic, check_source_syntax
from app.core.tools.schemas import (
    TOOL_APPLY_PATCH,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.patch_write.patch_apply import (
    PatchApplyError,
    apply_all_with_diff,
    validate_all,
)
from app.core.tools.tool_handler.patch_write.patch_parser import (
    parse_git_unified_diff_detailed,
)
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.apply_patch_args import ApplyPatchArgs

APPLY_PATCH_DESCRIPTION = (
    "Apply a Git-style unified diff to modify the contents of existing UTF-8 text files inside "
    "the active workspace. It only changes existing files: it cannot create, delete, or move "
    "files, so use 'write_file' to create or replace a whole file, 'delete_file' to delete one, "
    "and 'move_file' to move or rename one. The 'patch' parameter holds the diff text; its "
    "description defines the accepted format."
)


def _is_patch_retryable_after_correction(error: PatchApplyError) -> bool:
    """判断「非部分写入」的文件系统失败在修正后是否可重试。

    参数:
        error: 应用补丁时抛出的 ``PatchApplyError``；真实底层异常挂在 ``__cause__`` 上。

    返回:
        已发生部分写入时恒为 ``False``（必须按文件当前内容重新生成补丁，不能重放原补丁）；
        ``RuntimeError`` 起因视为可修正后重试；Windows 的文件共享冲突（``winerror`` 为
        32/33）以及 ``EAGAIN`` / ``EBUSY`` / ``ETXTBSY`` 视为瞬时故障可重试；其余不可重试。

    异常:
        无。

    副作用:
        无（纯判断，只读 ``error`` 及其 ``__cause__``）。
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
    return cause.errno in {errno.EAGAIN, errno.EBUSY, getattr(errno, "ETXTBSY", -1)}


class ApplyPatchTool(HandlerBase):
    """校验、应用 Git unified diff，并产出文件变更展示载荷。

    单一职责：把一次 ``apply_patch`` 调用编排为「解析 → 路径与内容校验 → 取消检查 → 落盘应用
    → 写后语法检查 → 观察归一化」。

    职责边界：
    - 负责：编排上述步骤，并把每条失败路径归一化为 :func:`tool_error`。
    - 不负责：diff 语法（``patch_parser``）、补丁算法（``patch_apply``）、路径边界
      （``PathResolver``）、展示载荷构造（``build_file_change_display_data``）。
    """

    name = TOOL_APPLY_PATCH
    description = APPLY_PATCH_DESCRIPTION
    permission = "file_write"
    args_model = ApplyPatchArgs
    timeout_seconds = 30.0
    risk_level = "medium"

    def execute(
        self,
        execution_context: ToolExecutionContext,
        patch: str,
    ) -> ToolObservation:
        """完成格式、路径与内容校验后应用补丁 hunk。

        参数:
            execution_context: 本次执行的运行时边界；``workspace_root`` 决定全部目标的路径
                边界，``run_id`` 用于应用前的取消检查。
            patch: Git 风格 unified diff 文本；每个 section 只能修改一个已存在的文件。

        返回:
            成功为 ``status="success"``，携带 ``file-changes`` 展示载荷；写后语法检查发现问题
            时把诊断写入 ``content``。解析失败、校验失败、部分写入、应用失败均为
            ``status="error"``；应用前检出取消为 ``status="cancelled"``。

        异常:
            无：全部失败都归一化为 :func:`tool_error` 或 :func:`tool_cancelled`，不向上抛出。

        副作用:
            可能改写工作区内的目标文件；落盘前先查 ``cancellation_registry`` 的 run 级取消。
        """

        outcome = parse_git_unified_diff_detailed(patch)
        if outcome.count_repairs:
            log.warning(
                "apply_patch_hunk_counts_recomputed",
                extra={
                    "msg": "补丁 hunk 头计数与正文不符，已按正文重算",
                    "data": {"run_id": execution_context.run_id, "repairs": outcome.count_repairs},
                },
            )
        if outcome.error:
            return tool_error(
                tool_name=self.name,
                error=f"invalid Git unified diff: {outcome.error}",
                reason=(
                    "provide a valid Git unified diff with 'diff --git', '---', '+++', and '@@' "
                    "sections, and make every hunk header count exactly the context, '-' and '+' "
                    "lines its own body contains; remove any '*** Begin Patch'/'*** End Patch' "
                    "markers. Use write_file, delete_file, or move_file for whole-file operations."
                ),
                retryable=True,
                permission=self.permission,
            )
        operations = outcome.operations
        resolver = PathResolver(execution_context.workspace_root)
        validation_errors = validate_all(operations, resolver)
        if validation_errors:
            return tool_error(
                tool_name=self.name,
                error="unified diff validation failed (no files were modified):\n"
                + "\n".join(f"  - {error}" for error in validation_errors),
                reason=(
                    "fix every cause listed in 'error', then submit a new Git unified diff. Causes "
                    "that live in a target file itself (missing or irregular file, binary content, "
                    "non-UTF-8 bytes, reserved or blocked path) cannot be fixed by editing the "
                    "diff: pick another target, or use write_file/delete_file/move_file. This tool "
                    "cannot create, delete, or move files."
                ),
                retryable=True,
                permission=self.permission,
            )
        if cancellation_registry.is_cancelled(execution_context.run_id):
            return tool_cancelled(tool_name=self.name, permission=self.permission)
        try:
            results = apply_all_with_diff(operations, resolver)
        except PatchApplyError as exc:
            if exc.partial_applied:
                return tool_error(
                    tool_name=self.name,
                    error="unified diff application stopped after partial changes",
                    reason=(
                        "inspect the changed files and generate a new unified diff from their "
                        "current contents; do not replay this diff unchanged."
                    ),
                    retryable=False,
                    permission=self.permission,
                )
            return tool_error(
                tool_name=self.name,
                error=f"unified diff apply failed: {exc}",
                reason=(
                    "re-read the affected files and generate a new unified diff for their current "
                    "contents before retrying."
                ),
                retryable=_is_patch_retryable_after_correction(exc),
                permission=self.permission,
            )

        display_data = build_file_change_display_data(results)
        diagnostics: list[SyntaxDiagnostic] = []
        for result in results:
            resolved, error = resolver.resolve_within_workspace(result.path)
            if resolved is None or error:
                continue
            check = check_source_syntax(str(resolved), result.after)
            if check.has_error:
                diagnostics.extend(check.diagnostics)
        content = self._build_content(outcome.count_repairs, diagnostics)
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=content,
            display_data=display_data,
        )

    def _build_content(
        self,
        count_repairs: list[str],
        diagnostics: list[SyntaxDiagnostic],
    ) -> str | None:
        """拼装成功观察的模型可见 `content`。

        参数:
            count_repairs: 解析期为「头部计数与正文不符」的 hunk 产出的重算说明；空列表表示
                头部本来就自洽。它只是提示，不改变成功语义——补丁已按正文应用。
            diagnostics: 写后语法检查产出的诊断；空列表表示未发现问题。

        返回:
            两类信息都为空时返回 ``None``（成功且无需提示）；否则以 ``success`` 开头，按
            「写后语法问题 → 头部计数重算说明」顺序拼接，文本保持英文/ASCII（模型通道）。

        异常:
            无。

        副作用:
            无（纯字符串拼接，不读取文件）。
        """

        blocks: list[str] = []
        if diagnostics:
            blocks.append(
                "Post-write syntax check reported issues:\n"
                + self._format_syntax_reason(diagnostics)
            )
        if count_repairs:
            blocks.append(
                "note: hunk headers declared counts that did not match the hunk body; the counts "
                "were recomputed from the body and the patch was applied as written:\n"
                + "\n".join(f"  - {note}" for note in count_repairs)
            )
        if not blocks:
            return None
        return "success\n" + "\n".join(blocks)

    @staticmethod
    def _format_syntax_reason(diagnostics: list[SyntaxDiagnostic]) -> str:
        """把写后语法诊断的位置渲染成面向模型的后续建议。

        参数:
            diagnostics: 写后语法检查产出的诊断列表；由上游按语法树全量收集，本函数不设额外
                截断，`content` 的总长度由全局输出预算约束。

        返回:
            面向模型的**英文**建议句（模型通道，保持英文）：诊断为空时给出通用修正提示，
            否则逐条列出 ``line <行> col <列>`` 以及缺失或意外的 token。

        异常:
            无。

        副作用:
            无（纯字符串拼接，不读取文件）。
        """

        if not diagnostics:
            return "the patched files have syntax errors; fix them with write_file or apply_patch."
        locations = "; ".join(
            f"line {item.row} col {item.column}"
            + (f" missing '{item.expected}'" if item.expected else " unexpected token")
            for item in diagnostics
        )
        return (
            f"the patched files have syntax errors ({locations}); fix them with a new "
            "apply_patch or write_file call."
        )

    def to_definition(self) -> ToolDefinition:
        """返回该工具的注册表定义与静态 diff 展示声明。

        返回:
            以 ``self.execute`` 为 handler、携带 ``diff`` 展开布局展示声明的
            :class:`ToolDefinition`。

        异常:
            无。

        副作用:
            无（不访问文件系统）。
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
    """构造 ``apply_patch`` 的注册表定义（不访问文件系统）。

    返回:
        由 :class:`ApplyPatchTool` 产出的 :class:`ToolDefinition`。

    异常:
        无。

    副作用:
        无。
    """

    return ApplyPatchTool().to_definition_if_avaliable()
